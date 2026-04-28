"""SoundMap FastAPI application — entry point."""

import json
import os
from pathlib import Path

import requests as _requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from .auth import router as auth_router
from .jobs import get_job, submit_job
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


@app.post("/analyze-moods")
async def analyze_moods(request: Request) -> JSONResponse:
    """Re-run mood grouping on existing map data using a user-supplied API key."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    api_key = ""
    try:
        body = await request.json()
        api_key = (body.get("api_key") or "").strip()
    except Exception:
        pass

    if not api_key:
        raise HTTPException(status_code=400, detail="api_key required")

    map_data = storage.load_map(user_id)
    if not map_data:
        raise HTTPException(status_code=404, detail="Map not found")

    points = map_data.get("points", [])
    if not points:
        raise HTTPException(status_code=400, detail="No tracks in map")

    # Reconstruct playlist metadata + track samples from stored points
    pl_tracks: dict[str, list[str]] = {}
    for p in points:
        for pl in p.get("playlists", []):
            pl_tracks.setdefault(pl, [])
            if len(pl_tracks[pl]) < 10:
                pl_tracks[pl].append(f"{p.get('name','')} — {p.get('artist','')}")

    playlist_meta = [{"name": pl} for pl in pl_tracks]
    if not playlist_meta:
        raise HTTPException(status_code=400, detail="No playlist data to analyze")

    from .pipeline import _llm_mood_groups
    pl_to_mood, persona = _llm_mood_groups(playlist_meta, pl_tracks, api_key=api_key)

    if not pl_to_mood:
        raise HTTPException(status_code=500, detail="Mood analysis returned no results — check your API key")

    # Apply mood assignments to points
    for p in points:
        pls = p.get("playlists", [])
        mood = next((pl_to_mood[pl] for pl in pls if pl in pl_to_mood), None)
        p["mood"] = mood or "Uncharted"

    map_data["points"] = points
    if persona:
        map_data["persona"] = persona
    storage.save_map(user_id, map_data)

    return JSONResponse({"points": points, "persona": persona})


@app.post("/start-job")
async def start_job(request: Request) -> JSONResponse:
    """Start pipeline job for the logged-in user. Accepts optional api_key in JSON body."""
    token = request.session.get("access_token")
    user_id = request.session.get("user_id")
    display_name = request.session.get("display_name", user_id)
    if not token or not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    api_key = ""
    try:
        body = await request.json()
        api_key = (body.get("api_key") or "").strip()
    except Exception:
        pass

    job_id = submit_job(token, user_id, display_name, api_key=api_key)
    return JSONResponse({"job_id": job_id, "user_id": user_id})


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


@app.post("/generate-remaining-playlists")
async def generate_remaining_playlists(request: Request) -> JSONResponse:
    """
    AI-groups a user's liked-but-unorganised tracks into new Spotify playlists.
    Body: {"api_key": "...", "provider": "anthropic"|"openai"}
    """
    token = request.session.get("access_token")
    user_id = request.session.get("user_id")
    if not token or not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        body = await request.json()
        user_api_key = (body.get("api_key") or "").strip()
        provider = (body.get("provider") or "anthropic").strip().lower()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if provider not in ("anthropic", "openai"):
        raise HTTPException(status_code=400, detail="provider must be 'anthropic' or 'openai'")

    api_key = user_api_key or os.environ.get(
        "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    )
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key required")

    map_data = storage.load_map(user_id)
    if not map_data:
        raise HTTPException(status_code=404, detail="Map not found — process your library first")

    remaining = map_data.get("remaining", [])
    if not remaining:
        raise HTTPException(status_code=400, detail="No unorganised tracks found — all your liked tracks are already in playlists")

    lines = [
        f"{t['id']} | {t.get('name','')} — {t.get('artist','')} | {t.get('release_year','?')}"
        for t in remaining
    ]
    manifest = "\n".join(lines)

    system_msg = (
        "You are a music curator. A user has liked these tracks but hasn't put them in any playlist. "
        "Group them into 2–6 coherent new playlists by genre, mood, or era. "
        "Each playlist must be sonically cohesive and have a short punchy name (2–4 words).\n\n"
        "Each input line is: TRACK_ID | Name — Artist | Release Year\n\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        '{"playlists": [{"name": "Playlist name", "track_ids": ["id1", ...]}, ...]}'
    )
    user_msg = f"Organise these {len(remaining)} unorganised liked tracks:\n\n{manifest}"

    raw = ""
    try:
        if provider == "anthropic":
            import anthropic as _anthropic
            resp = _anthropic.Anthropic(api_key=api_key).messages.create(
                model="claude-sonnet-4-6", max_tokens=4000,
                system=system_msg, messages=[{"role": "user", "content": user_msg}],
            )
            raw = resp.content[0].text.strip()
        else:
            from openai import OpenAI as _OpenAI
            resp = _OpenAI(api_key=api_key).chat.completions.create(
                model="gpt-4o", max_tokens=4000,
                messages=[{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}],
            )
            raw = resp.choices[0].message.content.strip()

        import re as _re
        m = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if not m:
            raise json.JSONDecodeError("no JSON", raw, 0)
        llm_data = json.loads(m.group(0))
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="AI returned malformed JSON — try again")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI error: {exc}")

    valid_ids = {t["id"] for t in remaining}
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    created = []

    for pl in llm_data.get("playlists", []):
        pl_name = (pl.get("name") or "").strip()
        track_ids = [tid for tid in pl.get("track_ids", []) if tid in valid_ids]
        if not pl_name or not track_ids:
            continue

        pl_resp = _requests.post(
            "https://api.spotify.com/v1/me/playlists", headers=h,
            json={"name": pl_name, "description": "SoundMap — auto-generated from unorganised liked tracks", "public": False},
        )
        if pl_resp.status_code not in (200, 201):
            print(f"[remaining] failed to create '{pl_name}': {pl_resp.status_code}")
            continue

        pl_id = pl_resp.json()["id"]
        pl_url = pl_resp.json()["external_urls"]["spotify"]
        uris = [f"spotify:track:{tid}" for tid in track_ids]
        for i in range(0, len(uris), 100):
            _requests.post(f"https://api.spotify.com/v1/playlists/{pl_id}/items", headers=h, json={"uris": uris[i:i+100]})

        created.append({"name": pl_name, "track_count": len(track_ids), "url": pl_url})
        print(f"[remaining] created '{pl_name}' ({len(track_ids)} tracks)")

    return JSONResponse({"created": created, "total_organised": sum(p["track_count"] for p in created)})


@app.get("/now-playing")
async def now_playing(request: Request) -> JSONResponse:
    """Return the user's currently playing track from Spotify."""
    token = request.session.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")

    resp = _requests.get(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers={"Authorization": f"Bearer {token}"},
        params={"additional_types": "track"},
    )

    if resp.status_code == 204 or not resp.content:
        return JSONResponse({"playing": False})

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Spotify error {resp.status_code}")

    data = resp.json()
    item = data.get("item")
    if not item or data.get("currently_playing_type") != "track":
        return JSONResponse({"playing": False})

    album = item.get("album", {})
    images = album.get("images", [])
    album_art = images[0]["url"] if images else None

    return JSONResponse({
        "playing": data.get("is_playing", False),
        "id": item.get("id"),
        "name": item.get("name"),
        "artist": ", ".join(a["name"] for a in item.get("artists", [])),
        "album": album.get("name"),
        "album_art": album_art,
        "external_url": (item.get("external_urls") or {}).get("spotify"),
        "progress_ms": data.get("progress_ms", 0),
        "duration_ms": item.get("duration_ms", 0),
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

    # Fetch all existing playlists owned by the user to avoid duplicates
    existing: dict[str, tuple[str, str]] = {}  # name → (id, url)
    offset = 0
    while True:
        r = _requests.get(f"https://api.spotify.com/v1/me/playlists?limit=50&offset={offset}", headers=h)
        if r.status_code != 200:
            break
        items = r.json().get("items", [])
        for pl in items:
            if pl and (pl.get("owner") or {}).get("id") == user_id:
                existing[pl["name"]] = (pl["id"], pl["external_urls"]["spotify"])
        if len(items) < 50:
            break
        offset += 50

    created = []

    for mood, track_ids in mood_tracks.items():
        if selected_moods is not None and mood not in selected_moods:
            continue

        pl_name = f"🗺 {mood}"

        # Skip if playlist already exists
        if pl_name in existing:
            print(f"[create-playlists] skipping '{pl_name}' — already exists")
            continue

        # Create the playlist
        pl_resp = _requests.post(
            f"https://api.spotify.com/v1/me/playlists",
            headers=h,
            json={
                "name": pl_name,
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


@app.post("/ai-playlist")
async def ai_playlist(request: Request) -> JSONResponse:
    """
    Curate a playlist using Claude or OpenAI.
    Body: {"prompt": "...", "name": "..." (optional), "api_key": "..." (optional), "provider": "anthropic"|"openai" (optional)}
    Returns: {name, url, track_count, duration_min, reasoning, track_ids}
    """
    token = request.session.get("access_token")
    user_id = request.session.get("user_id")
    if not token or not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        body = await request.json()
        prompt = (body.get("prompt") or "").strip()
        custom_name = (body.get("name") or "").strip()
        user_api_key = (body.get("api_key") or "").strip()
        provider = (body.get("provider") or "anthropic").strip().lower()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    if len(prompt) > 600:
        raise HTTPException(status_code=400, detail="Prompt too long (max 600 chars)")
    if provider not in ("anthropic", "openai"):
        raise HTTPException(status_code=400, detail="provider must be 'anthropic' or 'openai'")

    # Resolve API key: user-supplied key takes priority over server key
    if provider == "anthropic":
        api_key = user_api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise HTTPException(status_code=500, detail="No Anthropic API key — add yours in AI settings or ask the server admin to configure ANTHROPIC_API_KEY")
    else:
        api_key = user_api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise HTTPException(status_code=500, detail="No OpenAI API key — add yours in AI settings")

    map_data = storage.load_map(user_id)
    if not map_data:
        raise HTTPException(status_code=404, detail="Map not found — process your library first")

    pts = map_data.get("points", [])
    if not pts:
        raise HTTPException(status_code=400, detail="No tracks in your library map")

    # Build compact manifest: one line per track
    lines: list[str] = []
    for p in pts:
        tid = p.get("id")
        if not tid:
            continue
        name = p.get("name", "")
        artist = p.get("artist", "")
        pls = ", ".join((p.get("playlists") or [])[:3]) or "none"
        mood = p.get("mood", "Uncharted")
        dur_sec = round((p.get("duration_ms") or 0) / 1000)
        lines.append(f"{tid} | {name} — {artist} | pl: {pls} | mood: {mood} | {dur_sec}s")

    manifest = "\n".join(lines)

    system_msg = (
        "You are a music curator with deep knowledge of artists, genres, BPM, and energy levels. "
        "The user has given you their Spotify library and a request. "
        "Select tracks that genuinely fit — quality and cohesion matter more than quantity.\n\n"
        "Each library line is: TRACK_ID | Name — Artist | pl: playlist(s) | mood: mood | Xs\n\n"
        "Rules:\n"
        "- If a duration is mentioned (e.g. '1 hour', '45 min'), the SUM of selected track "
        "durations in seconds must land within ±15% of that target. Calculate carefully.\n"
        "- For high-BPM / high-energy requests (gym, workout, running, rave, techno): "
        "prioritise tracks from playlists with names like workout/gym/techno/dnb/electronic/rave, "
        "and artists/tracks you know to be high-energy from your training data.\n"
        "- For focus/study: instrumental, ambient, low-tempo, jazz, lo-fi.\n"
        "- For driving/road: anthemic, dynamic range, not too slow.\n"
        "- Order matters — build an arc appropriate to the request "
        "(warmup → peak → cooldown for gym; opener → energy → comedown for a party set, etc.).\n"
        "- Use your training knowledge of the artists and track names to infer BPM and energy. "
        "Playlist names and mood tags are strong hints.\n"
        "- NEVER invent or modify track IDs. Only return IDs that appear in the library.\n\n"
        "Respond ONLY with valid JSON, no markdown fences:\n"
        '{"name": "Playlist name (2-5 words)", "reasoning": "1-2 sentences", '
        '"track_ids": ["id1", "id2", ...]}'
    )

    user_msg = f"Library ({len(lines)} tracks):\n{manifest}\n\nRequest: {prompt}"

    print(f"[ai-playlist] {provider} for user {user_id} — {len(lines)} tracks, prompt: {prompt!r}")

    raw = ""
    try:
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                system=system_msg,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = resp.content[0].text.strip()
        else:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=4000,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
            )
            raw = resp.choices[0].message.content.strip()

        import re as _re
        match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if not match:
            raise json.JSONDecodeError("no JSON object found", raw, 0)
        llm_data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        print(f"[ai-playlist] JSON parse error: {exc}\nRaw: {raw[:500]}")
        raise HTTPException(status_code=502, detail="AI returned malformed JSON — try again")
    except Exception as exc:
        print(f"[ai-playlist] LLM error: {exc}")
        raise HTTPException(status_code=502, detail=f"AI error: {exc}")

    pl_name = custom_name or llm_data.get("name") or "AI Curated"
    reasoning = llm_data.get("reasoning", "")
    raw_ids: list[str] = llm_data.get("track_ids", [])

    # Validate — only accept IDs that exist in the user's library
    valid_ids = {p["id"] for p in pts if p.get("id")}
    track_ids = [tid for tid in raw_ids if tid in valid_ids]

    if not track_ids:
        raise HTTPException(status_code=502, detail="Claude returned no valid track IDs — try rephrasing")

    # Compute total duration
    dur_map = {p["id"]: (p.get("duration_ms") or 0) for p in pts}
    total_ms = sum(dur_map.get(tid, 0) for tid in track_ids)
    duration_min = round(total_ms / 60000, 1)

    print(f"[ai-playlist] '{pl_name}' — {len(track_ids)} tracks, {duration_min} min")
    print(f"[ai-playlist] reasoning: {reasoning}")

    # Create Spotify playlist
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    pl_resp = _requests.post(
        "https://api.spotify.com/v1/me/playlists",
        headers=h,
        json={
            "name": pl_name,
            "description": f"SoundMap AI · {prompt[:120]}",
            "public": False,
        },
    )
    if pl_resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=502,
            detail=f"Spotify error {pl_resp.status_code}: {pl_resp.text[:120]}",
        )

    pl_id = pl_resp.json()["id"]
    pl_url = pl_resp.json()["external_urls"]["spotify"]

    uris = [f"spotify:track:{tid}" for tid in track_ids]
    for i in range(0, len(uris), 100):
        _requests.post(
            f"https://api.spotify.com/v1/playlists/{pl_id}/items",
            headers=h,
            json={"uris": uris[i: i + 100]},
        )

    return JSONResponse({
        "name": pl_name,
        "url": pl_url,
        "track_count": len(track_ids),
        "duration_min": duration_min,
        "reasoning": reasoning,
        "track_ids": track_ids,
    })
