"""SoundMap FastAPI application — entry point."""

import os
from pathlib import Path

import requests as _requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from .auth import router as auth_router
from .jobs import get_job
from .models import JobStatus
from . import storage

load_dotenv()

app = FastAPI(title="SoundMap")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-secret-change-in-production"),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)

FRONTEND = Path(__file__).parent.parent / "frontend"


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/loading.html")
async def loading():
    return FileResponse(FRONTEND / "loading.html")


@app.get("/map.html")
async def map_page():
    return FileResponse(FRONTEND / "map.html")


@app.get("/status/{job_id}")
async def job_status(job_id: str) -> JSONResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(
        JobStatus(
            job_id=job_id,
            status=job["status"],
            progress=job["progress"],
            message=job["message"],
            user_id=job["user_id"],
            display_name=job["display_name"],
            error=job.get("error"),
        ).model_dump()
    )


@app.get("/debug/token")
async def debug_token(request: Request) -> JSONResponse:
    """Return the current session's Spotify token for testing."""
    token = request.session.get("access_token")
    user_id = request.session.get("user_id")
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in — visit / and log in first")
    return JSONResponse({"access_token": token, "user_id": user_id})


@app.get("/debug/playlists")
async def debug_playlists(request: Request) -> JSONResponse:
    """Test endpoint — lists owned playlists and fetches items from the first one."""
    token = request.session.get("access_token")
    user_id = request.session.get("user_id")
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in — visit / and log in first")

    h = {"Authorization": f"Bearer {token}"}

    # 1. Get playlists
    pl_resp = _requests.get("https://api.spotify.com/v1/me/playlists", headers=h, params={"limit": 50})
    if pl_resp.status_code != 200:
        return JSONResponse({"error": f"/me/playlists returned {pl_resp.status_code}", "body": pl_resp.text})

    all_playlists = pl_resp.json().get("items", [])
    owned = [p for p in all_playlists if p and (p.get("owner") or {}).get("id") == user_id]

    results = []
    # Test /items on first owned playlist — no fields filter, show raw item keys
    for pl in owned[:2]:
        pl_id = pl["id"]
        pl_name = pl.get("name", pl_id)
        resp = _requests.get(
            f"https://api.spotify.com/v1/playlists/{pl_id}/items",
            headers=h,
            params={"limit": 3},
        )
        tracks = []
        raw_keys = []
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            for item in items:
                if item:
                    raw_keys = list(item.keys())  # show what keys each item has
                    # try both 'track' (old) and 'item' (new)
                    t = item.get("track") or item.get("item")
                    if t and t.get("id"):
                        tracks.append(f'{t["name"]} — {t.get("artists", [{}])[0].get("name", "?")}')
        results.append({
            "playlist": pl_name,
            "status": resp.status_code,
            "item_keys": raw_keys,
            "tracks": tracks,
            "raw_error": resp.text[:300] if resp.status_code != 200 else None,
        })

    return JSONResponse({
        "user_id": user_id,
        "total_playlists": len(all_playlists),
        "owned_playlists": len(owned),
        "sample": results,
    })


@app.get("/map/{user_id}")
async def get_map(user_id: str) -> JSONResponse:
    data = storage.load_map(user_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Map not found — processing may still be running")
    return JSONResponse(data)


@app.post("/create-mood-playlists")
async def create_mood_playlists(request: Request) -> JSONResponse:
    """
    Create one Spotify playlist per mood and populate with tracks.
    Accepts optional JSON body: {"moods": ["Mood A", "Mood B"]} to only create
    specific moods. Omit or pass null to create all.
    Uses the session token — user must be logged in.
    """
    token = request.session.get("access_token")
    user_id = request.session.get("user_id")
    if not token or not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    # Parse optional mood filter from request body
    selected_moods: list[str] | None = None
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("moods"):
            selected_moods = body["moods"]
    except Exception:
        pass  # no body or invalid JSON — create all moods

    map_data = storage.load_map(user_id)
    if not map_data:
        raise HTTPException(status_code=404, detail="Map not found — process your library first")

    points = map_data.get("points", [])
    if not points:
        raise HTTPException(status_code=400, detail="No tracks in map data")

    # Group track IDs by mood
    mood_tracks: dict[str, list[str]] = {}
    for p in points:
        mood = p.get("mood", "Uncharted")
        tid = p.get("id")
        if tid:
            mood_tracks.setdefault(mood, []).append(tid)

    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    created = []

    for mood, track_ids in mood_tracks.items():
        if selected_moods is not None and mood not in selected_moods:
            continue
        # Create the playlist
        pl_resp = _requests.post(
            f"https://api.spotify.com/v1/me/playlists",
            headers=h,
            json={
                "name": f"🗺 {mood}",
                "description": f"SoundMap mood zone — {mood}. Auto-generated from your library.",
                "public": False,
            },
        )
        if pl_resp.status_code not in (200, 201):
            print(f"[create-playlists] failed to create '{mood}': {pl_resp.status_code} {pl_resp.text[:100]}")
            continue

        pl_id = pl_resp.json()["id"]
        pl_url = pl_resp.json()["external_urls"]["spotify"]

        # Add tracks in batches of 100
        uris = [f"spotify:track:{tid}" for tid in track_ids]
        for i in range(0, len(uris), 100):
            batch = uris[i: i + 100]
            _requests.post(
                f"https://api.spotify.com/v1/playlists/{pl_id}/items",
                headers=h,
                json={"uris": batch},
            )

        created.append({"mood": mood, "track_count": len(track_ids), "url": pl_url})
        print(f"[create-playlists] created '🗺 {mood}' ({len(track_ids)} tracks)")

    return JSONResponse({"created": created})


@app.post("/merge-playlists")
async def merge_playlists(request: Request) -> JSONResponse:
    """
    Merge tracks from selected playlists into a new Spotify playlist.
    Body: {"source_playlists": ["name1", "name2"], "new_name": "My Merged Playlist"}
    Duplicate tracks are removed; order is preserved by play_score (highest first).
    """
    token = request.session.get("access_token")
    user_id = request.session.get("user_id")
    if not token or not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        body = await request.json()
        source = body.get("source_playlists", [])
        new_name = body.get("new_name", "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not source:
        raise HTTPException(status_code=400, detail="No playlists selected")
    if not new_name:
        raise HTTPException(status_code=400, detail="Playlist name required")

    map_data = storage.load_map(user_id)
    if not map_data:
        raise HTTPException(status_code=404, detail="Map not found — process your library first")

    source_set = set(source)
    seen: set[str] = set()
    scored: list[tuple[int, str]] = []  # (play_score, track_id)

    for p in map_data.get("points", []):
        tid = p.get("id")
        if tid and tid not in seen and any(pl in source_set for pl in p.get("playlists", [])):
            seen.add(tid)
            scored.append((p.get("play_score", 1), tid))

    # Sort by play_score descending so most-listened tracks appear first
    scored.sort(key=lambda x: x[0], reverse=True)
    track_ids = [tid for _, tid in scored]

    if not track_ids:
        raise HTTPException(status_code=400, detail="No tracks found in selected playlists")

    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    desc = f"SoundMap merge of: {', '.join(source[:3])}{'…' if len(source) > 3 else ''}"
    pl_resp = _requests.post(
        "https://api.spotify.com/v1/me/playlists",
        headers=h,
        json={"name": new_name, "description": desc, "public": False},
    )
    if pl_resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Spotify error {pl_resp.status_code}: {pl_resp.text[:120]}")

    pl_id = pl_resp.json()["id"]
    pl_url = pl_resp.json()["external_urls"]["spotify"]

    uris = [f"spotify:track:{tid}" for tid in track_ids]
    for i in range(0, len(uris), 100):
        _requests.post(
            f"https://api.spotify.com/v1/playlists/{pl_id}/items",
            headers=h,
            json={"uris": uris[i: i + 100]},
        )

    print(f"[merge-playlists] '{new_name}' — {len(track_ids)} tracks from {source}")
    return JSONResponse({"name": new_name, "track_count": len(track_ids), "url": pl_url})
