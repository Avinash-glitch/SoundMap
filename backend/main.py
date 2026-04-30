"""SoundMap FastAPI application — entry point."""

import asyncio
import json
import os
import re as _re
from pathlib import Path

import requests as _requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

from .auth import router as auth_router, refresh_access_token
from .jobs import get_job, submit_job, stop_job
from .models import JobStatus
from . import storage

load_dotenv()

_ENV_KEY_MAP = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "nvidia": "NVIDIA_API_KEY"}


def _default_provider() -> str:
    """Pick the first provider whose API key is configured in the environment."""
    for provider in ("nvidia", "anthropic", "openai"):
        if os.environ.get(_ENV_KEY_MAP[provider]):
            return provider
    return "anthropic"


def _parse_llm_json(raw: str) -> dict:
    """Robust JSON extraction from LLM output — handles markdown fences and stray text."""
    cleaned = _re.sub(r'```(?:json)?\s*', '', raw).strip().rstrip('`').strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    m = _re.search(r'\{.*\}', cleaned, _re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No parseable JSON in LLM response: {raw[:300]}")


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


async def _get_valid_token(request: Request) -> str | None:
    """Return a valid Spotify access token, auto-refreshing if the stored one is expired."""
    token = request.session.get("access_token")
    if not token:
        return None
    # Check if still valid with a lightweight call
    test = _requests.get("https://api.spotify.com/v1/me",
                         headers={"Authorization": f"Bearer {token}"}, timeout=5)
    if test.status_code != 401:
        return token
    return await refresh_access_token(request)


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/loading.html")
async def loading():
    return FileResponse(FRONTEND / "loading.html")


@app.get("/map.html")
async def map_page():
    return FileResponse(FRONTEND / "map.html")


@app.get("/compare.html")
async def compare_page():
    return FileResponse(FRONTEND / "compare.html")


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

    try:
        body = await request.json()
        user_api_key = (body.get("api_key") or "").strip()
        provider = (body.get("provider") or _default_provider()).strip().lower()
    except Exception:
        user_api_key, provider = "", "anthropic"

    if provider not in ("anthropic", "openai", "nvidia"):
        raise HTTPException(status_code=400, detail="provider must be 'anthropic', 'openai', or 'nvidia'")

    api_key = user_api_key or os.environ.get(_ENV_KEY_MAP[provider], "")
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
    pl_to_mood, persona = _llm_mood_groups(playlist_meta, pl_tracks, api_key=api_key, provider=provider)

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
    provider = ""
    try:
        body = await request.json()
        api_key = (body.get("api_key") or "").strip()
        provider = (body.get("provider") or "").strip().lower()
    except Exception:
        pass

    job_id = submit_job(token, user_id, display_name, api_key=api_key, provider=provider)
    return JSONResponse({"job_id": job_id, "user_id": user_id})


@app.post("/stop-job/{job_id}")
async def stop_job_endpoint(job_id: str, request: Request) -> JSONResponse:
    """Signal the pipeline to stop fetching early and build the map with what it has."""
    user_id = request.session.get("user_id")
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not your job")
    stopped = stop_job(job_id)
    return JSONResponse({"stopped": stopped})


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
async def generate_remaining_playlists(request: Request) -> StreamingResponse:
    """
    AI-groups liked-but-unorganised tracks into new Spotify playlists, streaming progress via SSE.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    token = await _get_valid_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Spotify session expired — please log in again")

    try:
        body = await request.json()
        user_api_key = (body.get("api_key") or "").strip()
        provider = (body.get("provider") or _default_provider()).strip().lower()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if provider not in ("anthropic", "openai", "nvidia"):
        raise HTTPException(status_code=400, detail="provider must be 'anthropic', 'openai', or 'nvidia'")

    api_key = user_api_key or os.environ.get(_ENV_KEY_MAP[provider], "")
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
    existing_pl_names = sorted({pl for p in map_data.get("points", []) for pl in p.get("playlists", [])})
    existing_note = (
        f"\nThe user already has these playlists: {', '.join(existing_pl_names[:20])}. "
        "Do NOT create playlists with the same name or theme — create genuinely new, distinct ones."
        if existing_pl_names else ""
    )
    system_msg = (
        "You are a music curator. A user has liked these tracks but hasn't put them in any playlist. "
        "Group them into 2–6 coherent NEW playlists by genre, mood, or era. "
        "Each playlist must be sonically cohesive and have a short punchy name (2–4 words)."
        + existing_note + "\n\n"
        "Each input line is: TRACK_ID | Name — Artist | Release Year\n\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        '{"playlists": [{"name": "Playlist name", "track_ids": ["id1", ...]}, ...]}'
    )
    user_msg = f"Organise these {len(remaining)} unorganised liked tracks into new playlists:\n\n" + "\n".join(lines)

    from .pipeline import _call_llm_chat

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    async def stream():
        loop = asyncio.get_running_loop()
        yield _sse({"type": "progress", "message": f"Sending {len(remaining)} tracks to AI…", "pct": 10})

        try:
            raw = await loop.run_in_executor(
                None,
                lambda: _call_llm_chat(system_msg, user_msg, api_key=api_key, provider=provider, max_tokens=4000),
            )
            llm_data = _parse_llm_json(raw)
        except Exception as exc:
            yield _sse({"type": "error", "message": f"AI error: {exc}"})
            return

        # Return suggestions — client reviews and calls /create-suggested-playlists
        valid_ids = {t["id"] for t in remaining}
        remaining_lookup = {t["id"]: {"name": t.get("name",""), "artist": t.get("artist",""), "album_art": t.get("album_art","")} for t in remaining}
        suggestions = []
        for pl in llm_data.get("playlists", []):
            pl_name = (pl.get("name") or "").strip()
            track_ids = [tid for tid in pl.get("track_ids", []) if tid in valid_ids]
            if pl_name and track_ids:
                suggestions.append({
                    "name": pl_name,
                    "track_ids": track_ids,
                    "tracks": {tid: remaining_lookup[tid] for tid in track_ids if tid in remaining_lookup},
                })

        yield _sse({"type": "done", "suggestions": suggestions})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/ai-sort-remaining")
async def ai_sort_remaining(request: Request) -> StreamingResponse:
    """
    AI assigns remaining liked tracks to existing playlists, streaming progress via SSE.
    Processes in batches of 60 so progress is visible and large libraries don't time out.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        body = await request.json()
        user_api_key = (body.get("api_key") or "").strip()
        provider = (body.get("provider") or _default_provider()).strip().lower()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if provider not in ("anthropic", "openai", "nvidia"):
        raise HTTPException(status_code=400, detail="provider must be 'anthropic', 'openai', or 'nvidia'")

    api_key = user_api_key or os.environ.get(_ENV_KEY_MAP[provider], "")
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key required")

    map_data = storage.load_map(user_id)
    if not map_data:
        raise HTTPException(status_code=404, detail="Map not found")

    remaining = map_data.get("remaining", [])
    if not remaining:
        raise HTTPException(status_code=400, detail="No remaining tracks found")

    playlist_names = sorted({pl for p in map_data.get("points", []) for pl in p.get("playlists", [])})
    if not playlist_names:
        raise HTTPException(status_code=400, detail="No existing playlists in your map — process your library first")

    pl_list = "\n".join(f"- {p}" for p in playlist_names)
    system_msg = (
        "You are a music curator. Assign each track to the single most fitting existing playlist. "
        "Use only the EXACT playlist names provided — do not invent new ones. "
        "Assign every track to its closest match even if the fit is imperfect.\n\n"
        "Each input line is: TRACK_ID | Name — Artist | Release Year\n\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        '{"assignments": {"track_id": "Exact Playlist Name", ...}}'
    )

    from .pipeline import _call_llm_chat

    track_info = {
        t["id"]: {"name": t.get("name", ""), "artist": t.get("artist", ""), "album_art": t.get("album_art", "")}
        for t in remaining
    }

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    async def stream():
        BATCH = 60
        n = len(remaining)
        n_batches = max(1, (n + BATCH - 1) // BATCH)
        valid_pls = set(playlist_names)
        loop = asyncio.get_running_loop()
        total_assigned = 0

        for bi, start in enumerate(range(0, n, BATCH)):
            batch = remaining[start:start + BATCH]
            end = min(start + BATCH, n)
            yield _sse({
                "type": "progress",
                "message": f"Reading through songs {start + 1}–{end} of {n}…",
                "processed": start, "total": n,
            })

            lines = [
                f"{t['id']} | {t.get('name','')} — {t.get('artist','')} | {t.get('release_year','?')}"
                for t in batch
            ]
            batch_user = f"Existing playlists:\n{pl_list}\n\nAssign these {len(batch)} tracks:\n\n" + "\n".join(lines)

            # Run LLM in executor; send keep-alive pings every 15s so Railway doesn't drop the connection
            fut = asyncio.ensure_future(loop.run_in_executor(
                None, lambda u=batch_user: _call_llm_chat(system_msg, u, api_key=api_key, provider=provider, max_tokens=2000)
            ))
            while not fut.done():
                try:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # SSE comment — keeps connection alive, browser ignores it

            try:
                raw = fut.result()
                batch_data = _parse_llm_json(raw)
                batch_assignments: dict[str, str] = {}
                for tid, pl in batch_data.get("assignments", {}).items():
                    if pl in valid_pls:
                        batch_assignments[tid] = pl
                total_assigned += len(batch_assignments)
                # Stream this batch's results immediately so the frontend can render them
                yield _sse({
                    "type": "batch_done",
                    "assignments": batch_assignments,
                    "tracks": {tid: track_info[tid] for tid in batch_assignments if tid in track_info},
                    "processed": end,
                    "total": n,
                    "batch": bi + 1,
                    "total_batches": n_batches,
                    "playlist_names": playlist_names,
                })
            except Exception as exc:
                yield _sse({"type": "error", "message": f"Batch {bi + 1} failed: {exc}"})
                return

        yield _sse({"type": "done", "total_assigned": total_assigned, "total": n})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/suggest-remaining")
async def suggest_remaining(request: Request) -> JSONResponse:
    """
    Match remaining liked tracks to existing playlists using MusicBrainz genre tags.
    Zero AI tokens — entirely free. Slow on first call (1 req/s), instant after caching.
    Returns {track_id: {playlist, genre, confidence}} for up to 60 remaining tracks.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    map_data = storage.load_map(user_id)
    if not map_data:
        raise HTTPException(status_code=404, detail="Map not found — process your library first")

    remaining: list[dict] = map_data.get("remaining", [])
    if not remaining:
        raise HTTPException(status_code=400, detail="No remaining tracks — all liked songs are already in playlists")

    points: list[dict] = map_data.get("points", [])

    # Build genre fingerprint for each playlist from the existing map tracks
    # genre field is one of our broad buckets (rock, pop, electronic, …)
    playlist_profile: dict[str, dict[str, int]] = {}
    for p in points:
        genre = p.get("genre") or "other"
        for pl in p.get("playlists") or []:
            playlist_profile.setdefault(pl, {})[genre] = playlist_profile[pl].get(genre, 0) + 1

    if not playlist_profile:
        raise HTTPException(status_code=400, detail="No playlist data in map — regenerate your map first")

    # Limit to 60 most-recently-liked tracks to bound API time on cold cache
    batch = remaining[:60]

    from .musicbrainz import get_tags_batch
    from .pipeline import _primary_genre

    total_fetched = 0
    def _prog(done, total):
        nonlocal total_fetched
        total_fetched = done
        print(f"[suggest-remaining] MusicBrainz: {done}/{total}")

    tag_map = get_tags_batch(batch, on_progress=_prog)

    suggestions: dict[str, dict] = {}
    for t in batch:
        tid = t["id"]
        tags = tag_map.get(tid, [])
        genre = _primary_genre(tags) if tags else "other"

        best_pl, best_score = None, 0.0
        for pl, counts in playlist_profile.items():
            total = sum(counts.values()) or 1
            score = counts.get(genre, 0) / total
            if score > best_score:
                best_score, best_pl = score, pl

        suggestions[tid] = {
            "playlist": best_pl,
            "genre": genre,
            "confidence": round(best_score, 3),
            "mb_tags": tags[:5],
        }

    return JSONResponse({
        "suggestions": suggestions,
        "tracks": {t["id"]: {"name": t["name"], "artist": t["artist"]} for t in batch},
        "playlists_profiled": len(playlist_profile),
        "cache_misses": total_fetched,
    })


@app.post("/add-to-playlists")
async def add_to_playlists(request: Request) -> JSONResponse:
    """
    Add tracks to existing Spotify playlists in batch.
    Body: {"assignments": {"PlaylistName": ["track_id1", "track_id2", ...]}}
    """
    token = request.session.get("access_token")
    user_id = request.session.get("user_id")
    if not token or not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        body = await request.json()
        assignments: dict[str, list[str]] = body.get("assignments", {})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not assignments:
        raise HTTPException(status_code=400, detail="No assignments provided")

    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Resolve playlist names → IDs
    pl_resp = _requests.get("https://api.spotify.com/v1/me/playlists", headers=h, params={"limit": 50})
    if pl_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not fetch your playlists")

    name_to_id: dict[str, str] = {}
    for pl in pl_resp.json().get("items", []):
        if pl and pl.get("id"):
            name_to_id[pl["name"]] = pl["id"]

    results = []
    for pl_name, track_ids in assignments.items():
        pl_id = name_to_id.get(pl_name)
        if not pl_id:
            results.append({"playlist": pl_name, "error": "playlist not found"})
            continue

        uris = [f"spotify:track:{tid}" for tid in track_ids if tid]
        added = 0
        for i in range(0, len(uris), 100):
            r = _requests.post(
                f"https://api.spotify.com/v1/playlists/{pl_id}/items",
                headers=h, json={"uris": uris[i:i+100]},
            )
            if r.status_code in (200, 201):
                added += min(100, len(uris) - i)

        results.append({"playlist": pl_name, "added": added})
        print(f"[add-to-playlists] '{pl_name}' ← {added} tracks")

    return JSONResponse({"results": results})


@app.get("/top-tracks")
async def top_tracks_endpoint(request: Request, time_range: str = "short_term") -> JSONResponse:
    """Return the user's top 7 tracks for a given time range."""
    if time_range not in ("short_term", "medium_term", "long_term"):
        raise HTTPException(status_code=400, detail="time_range must be short_term, medium_term, or long_term")
    token = await _get_valid_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")
    resp = _requests.get(
        "https://api.spotify.com/v1/me/top/tracks",
        headers={"Authorization": f"Bearer {token}"},
        params={"limit": 7, "time_range": time_range},
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Spotify error {resp.status_code}")
    return JSONResponse([{
        "id": t["id"],
        "name": t["name"],
        "artist": ", ".join(a["name"] for a in t.get("artists", [])),
        "album_art": (t.get("album", {}).get("images", [{}])[0].get("url", "")),
        "external_url": t.get("external_urls", {}).get("spotify", ""),
    } for t in resp.json().get("items", [])])


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


@app.get("/compare/{user_id_a}/{user_id_b}")
async def compare_users(user_id_a: str, user_id_b: str) -> JSONResponse:
    """Compare two users' music libraries — shared tracks and similar playlists."""
    import math

    map_a = storage.load_map(user_id_a)
    map_b = storage.load_map(user_id_b)
    if not map_a:
        raise HTTPException(status_code=404, detail=f"No map found for {user_id_a} — they need to process their library first")
    if not map_b:
        raise HTTPException(status_code=404, detail=f"No map found for {user_id_b} — they need to process their library first")

    pts_a = map_a.get("points", [])
    pts_b = map_b.get("points", [])

    ids_a = {p["id"]: p for p in pts_a if p.get("id")}
    ids_b = {p["id"]: p for p in pts_b if p.get("id")}
    shared_ids = set(ids_a) & set(ids_b)

    shared_tracks = sorted(
        [
            {
                "id": tid,
                "name": ids_a[tid].get("name", ""),
                "artist": ids_a[tid].get("artist", ""),
                "album_art": ids_a[tid].get("album_art", ""),
                "external_url": ids_a[tid].get("external_url", ""),
                "playlists_a": ids_a[tid].get("playlists") or [],
                "playlists_b": ids_b[tid].get("playlists") or [],
            }
            for tid in shared_ids
        ],
        key=lambda t: (ids_a[t["id"]].get("play_score", 0) + ids_b[t["id"]].get("play_score", 0)),
        reverse=True,
    )

    # Build per-playlist genre vectors (11-dim, one per genre bucket)
    GENRES = ["metal", "hip-hop", "electronic", "rock", "pop", "jazz", "classical", "folk", "latin", "world", "other"]

    def build_vectors(pts: list[dict]) -> dict[str, list[float]]:
        pl_counts: dict[str, dict[str, int]] = {}
        for p in pts:
            genre = p.get("genre") or "other"
            for pl in (p.get("playlists") or []):
                pl_counts.setdefault(pl, {})
                pl_counts[pl][genre] = pl_counts[pl].get(genre, 0) + 1
        vecs = {}
        for pl, counts in pl_counts.items():
            total = sum(counts.values()) or 1
            if total < 3:
                continue
            vecs[pl] = [counts.get(g, 0) / total for g in GENRES]
        return vecs

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
        return dot / mag if mag else 0.0

    vecs_a = build_vectors(pts_a)
    vecs_b = build_vectors(pts_b)

    pairs = []
    for pl_a, vec_a in vecs_a.items():
        for pl_b, vec_b in vecs_b.items():
            score = cosine(vec_a, vec_b)
            if score >= 0.25:
                pairs.append({"playlist_a": pl_a, "playlist_b": pl_b, "score": round(score, 3)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    # Compatibility score: genre similarity + shared track bonus
    if vecs_a and vecs_b:
        best_per_a = [max((cosine(va, vb) for vb in vecs_b.values()), default=0.0) for va in vecs_a.values()]
        genre_compat = sum(best_per_a) / len(best_per_a)
        track_bonus = len(shared_ids) / max(len(ids_a), len(ids_b), 1) * 0.3
        compatibility = round(min(genre_compat * 0.7 + track_bonus, 1.0), 3)
    else:
        compatibility = round(len(shared_ids) / max(len(ids_a), len(ids_b), 1), 3)

    return JSONResponse({
        "user_a": {
            "id": user_id_a,
            "display_name": map_a.get("display_name") or user_id_a,
            "track_count": len(pts_a),
        },
        "user_b": {
            "id": user_id_b,
            "display_name": map_b.get("display_name") or user_id_b,
            "track_count": len(pts_b),
        },
        "shared_tracks": shared_tracks[:50],
        "shared_count": len(shared_ids),
        "similar_playlists": pairs[:20],
        "compatibility": compatibility,
    })


@app.get("/me")
async def me(request: Request) -> JSONResponse:
    """Return the current session's user identity, or 401 if not logged in."""
    user_id = request.session.get("user_id")
    display_name = request.session.get("display_name", user_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    return JSONResponse({"user_id": user_id, "display_name": display_name})


@app.get("/apple/token")
async def apple_developer_token() -> JSONResponse:
    """Return a short-lived Apple Music developer token for MusicKit JS."""
    try:
        from .apple_auth import get_developer_token
        token = get_developer_token()
        return JSONResponse({"developer_token": token})
    except EnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Token generation failed: {exc}")


@app.post("/apple/start-job")
async def apple_start_job(request: Request) -> JSONResponse:
    """Start an Apple Music pipeline job. Body: {music_user_token, storefront}."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in — connect Spotify first")

    try:
        body = await request.json()
        music_user_token = (body.get("music_user_token") or "").strip()
        storefront = (body.get("storefront") or "us").strip().lower()
        api_key = (body.get("api_key") or "").strip()
        provider = (body.get("provider") or "").strip().lower()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not music_user_token:
        raise HTTPException(status_code=400, detail="music_user_token required")

    from .jobs import submit_apple_job
    apple_user_id = f"{user_id}_apple"
    job_id = submit_apple_job(music_user_token, user_id, storefront, api_key=api_key, provider=provider)
    return JSONResponse({"job_id": job_id, "user_id": apple_user_id})


@app.get("/apple/test")
async def apple_test() -> JSONResponse:
    """Verify the Apple Music developer token by hitting the catalog API."""
    try:
        from .apple_auth import get_developer_token
        token = get_developer_token()
    except Exception as exc:
        return JSONResponse({"step": "token_generation", "ok": False, "error": str(exc)})

    resp = _requests.get(
        "https://api.music.apple.com/v1/catalog/us/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"term": "test", "types": "songs", "limit": 1},
        timeout=10,
    )
    return JSONResponse({
        "step": "catalog_request",
        "ok": resp.status_code == 200,
        "http_status": resp.status_code,
        "token_prefix": token[:60] + "…",
        "error": resp.text[:300] if resp.status_code != 200 else None,
    })


@app.get("/apple/configured")
async def apple_configured() -> JSONResponse:
    """Check whether Apple Music credentials are configured."""
    from .apple_auth import is_configured
    return JSONResponse({"configured": is_configured()})


@app.get("/map/{user_id}")
async def get_map(user_id: str) -> JSONResponse:
    data = storage.load_map(user_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Map not found — processing may still be running")
    return JSONResponse(data)


@app.post("/import-friend-playlist")
async def import_friend_playlist(request: Request) -> JSONResponse:
    """
    Copy a friend's playlist into the logged-in user's Spotify library.

    Body: {
      "playlist_name": str,          # original playlist name
      "track_ids": [str, ...],       # Spotify track IDs from friend's map
      "friend_display_name": str     # used to label the new playlist
    }
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    token = await _get_valid_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Spotify session expired — please log in again")

    try:
        body = await request.json()
        playlist_name: str = (body.get("playlist_name") or "").strip()
        track_ids: list[str] = body.get("track_ids") or []
        friend_name: str = (body.get("friend_display_name") or "Friend").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not playlist_name:
        raise HTTPException(status_code=400, detail="playlist_name required")
    if not track_ids:
        raise HTTPException(status_code=400, detail="track_ids required")

    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    new_name = f"{playlist_name} (from {friend_name})"
    print(f"[import-friend-playlist] user={user_id} | playlist='{new_name}' | tracks={len(track_ids)}")

    create_resp = _requests.post(
        "https://api.spotify.com/v1/me/playlists",
        headers=h,
        json={"name": new_name, "public": False, "description": f"Imported from {friend_name}'s SoundMap"},
    )
    print(f"[import-friend-playlist] create status={create_resp.status_code} body={create_resp.text[:300]}")
    if create_resp.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail="Spotify permissions missing — please reconnect your Spotify account to grant playlist access",
        )
    if create_resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Could not create playlist: {create_resp.text[:300]}")

    pl_data = create_resp.json()
    pl_id = pl_data["id"]
    pl_url = pl_data["external_urls"]["spotify"]

    uris = [f"spotify:track:{tid}" for tid in track_ids if tid]
    added = 0
    for i in range(0, len(uris), 100):
        r = _requests.post(
            f"https://api.spotify.com/v1/playlists/{pl_id}/items",
            headers=h,
            json={"uris": uris[i:i + 100]},
        )
        if r.status_code in (200, 201):
            added += min(100, len(uris) - i)

    print(f"[import-friend-playlist] created '{new_name}' ({added}/{len(uris)} tracks) for {user_id}")
    return JSONResponse({"name": new_name, "track_count": added, "url": pl_url})


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
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    token = await _get_valid_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Spotify session expired — please log in again")

    try:
        body = await request.json()
        prompt = (body.get("prompt") or "").strip()
        custom_name = (body.get("name") or "").strip()
        user_api_key = (body.get("api_key") or "").strip()
        provider = (body.get("provider") or _default_provider()).strip().lower()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    if len(prompt) > 600:
        raise HTTPException(status_code=400, detail="Prompt too long (max 600 chars)")
    if provider not in ("anthropic", "openai", "nvidia"):
        raise HTTPException(status_code=400, detail="provider must be 'anthropic', 'openai', or 'nvidia'")

    # Resolve API key: user-supplied key takes priority over server key
    api_key = user_api_key or os.environ.get(_ENV_KEY_MAP[provider], "")
    if not api_key:
        _labels = {"anthropic": "Anthropic", "openai": "OpenAI", "nvidia": "NVIDIA"}
        raise HTTPException(status_code=500, detail=f"No {_labels[provider]} API key — add yours in AI settings or configure {_ENV_KEY_MAP[provider]}")

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
        elif provider == "nvidia":
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url="https://integrate.api.nvidia.com/v1")
            resp = client.chat.completions.create(
                model="z-ai/glm-5.1",
                max_tokens=4000,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
            )
            raw = resp.choices[0].message.content.strip()
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

    valid_ids = {p["id"] for p in pts if p.get("id")}
    track_ids = [tid for tid in raw_ids if tid in valid_ids]

    if not track_ids:
        raise HTTPException(status_code=502, detail="AI returned no valid track IDs — try rephrasing")

    dur_map = {p["id"]: (p.get("duration_ms") or 0) for p in pts}
    total_ms = sum(dur_map.get(tid, 0) for tid in track_ids)
    duration_min = round(total_ms / 60000, 1)

    # Return suggestions — client reviews and calls /create-ai-playlist to save
    track_lookup = {p["id"]: {"name": p.get("name",""), "artist": p.get("artist",""), "album_art": p.get("album_art","")} for p in pts if p.get("id")}
    print(f"[ai-playlist] suggestion '{pl_name}' — {len(track_ids)} tracks, {duration_min} min")
    return JSONResponse({
        "name": pl_name,
        "reasoning": reasoning,
        "track_ids": track_ids,
        "track_count": len(track_ids),
        "duration_min": duration_min,
        "tracks": {tid: track_lookup[tid] for tid in track_ids if tid in track_lookup},
        "prompt": prompt,
    })


@app.post("/create-ai-playlist")
async def create_ai_playlist(request: Request) -> JSONResponse:
    """Create a Spotify playlist from a curated track list. Body: {name, track_ids, prompt}."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    token = await _get_valid_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Spotify session expired — please log in again")

    try:
        body = await request.json()
        pl_name = (body.get("name") or "AI Curated").strip()
        track_ids: list[str] = body.get("track_ids", [])
        prompt = (body.get("prompt") or "")[:120]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not track_ids:
        raise HTTPException(status_code=400, detail="No tracks provided")

    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    pl_resp = _requests.post(
        "https://api.spotify.com/v1/me/playlists", headers=h,
        json={"name": pl_name, "description": f"SoundMap AI · {prompt}", "public": False},
    )
    if pl_resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Spotify error {pl_resp.status_code}: {pl_resp.text[:120]}")

    pl_id = pl_resp.json()["id"]
    pl_url = pl_resp.json()["external_urls"]["spotify"]
    uris = [f"spotify:track:{tid}" for tid in track_ids]
    for i in range(0, len(uris), 100):
        _requests.post(f"https://api.spotify.com/v1/playlists/{pl_id}/items", headers=h, json={"uris": uris[i:i+100]})

    print(f"[create-ai-playlist] '{pl_name}' — {len(track_ids)} tracks")
    return JSONResponse({"name": pl_name, "url": pl_url, "track_count": len(track_ids)})


@app.post("/create-suggested-playlists")
async def create_suggested_playlists(request: Request) -> StreamingResponse:
    """
    Create Spotify playlists from AI-suggested groupings. Streams progress per playlist.
    Body: {"playlists": [{"name": "...", "track_ids": [...]}]}
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    token = await _get_valid_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Spotify session expired — please log in again")

    try:
        body = await request.json()
        playlists: list[dict] = body.get("playlists", [])
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not playlists:
        raise HTTPException(status_code=400, detail="No playlists provided")

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    async def stream():
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        created = []
        n = len(playlists)

        for i, pl in enumerate(playlists):
            pl_name = (pl.get("name") or "").strip()
            track_ids = pl.get("track_ids", [])
            if not pl_name or not track_ids:
                continue

            yield _sse({"type": "progress", "message": f"Creating '{pl_name}'… ({i+1}/{n})", "pct": int(i / n * 90)})

            pl_resp = _requests.post(
                "https://api.spotify.com/v1/me/playlists", headers=h,
                json={"name": pl_name, "description": "SoundMap — organised from liked tracks", "public": False},
            )
            if pl_resp.status_code not in (200, 201):
                print(f"[create-suggested] failed '{pl_name}': {pl_resp.status_code}")
                continue

            pl_id = pl_resp.json()["id"]
            pl_url = pl_resp.json()["external_urls"]["spotify"]
            uris = [f"spotify:track:{tid}" for tid in track_ids]
            for j in range(0, len(uris), 100):
                _requests.post(f"https://api.spotify.com/v1/playlists/{pl_id}/items", headers=h, json={"uris": uris[j:j+100]})

            created.append({"name": pl_name, "track_count": len(track_ids), "url": pl_url})
            print(f"[create-suggested] created '{pl_name}' ({len(track_ids)} tracks)")

        yield _sse({"type": "done", "created": created, "total_organised": sum(p["track_count"] for p in created)})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
