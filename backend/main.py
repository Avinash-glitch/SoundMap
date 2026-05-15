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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

from .auth import forget_user_credentials, router as auth_router, refresh_access_token
from .jobs import get_job, submit_job, stop_job, submit_youtube_job
from .models import JobStatus
from .notifications import send_notification
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

CANONICAL_HOST = os.environ.get("CANONICAL_HOST", "soundmap.avibuildsstuff.com")
LEGACY_HOSTS = {"web-production-7b33.up.railway.app"}


@app.middleware("http")
async def redirect_legacy_hosts(request: Request, call_next):
    """Send old Railway URLs to the public SoundMap domain."""
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(":")[0]
    if CANONICAL_HOST and host in LEGACY_HOSTS:
        target = f"https://{CANONICAL_HOST}{request.url.path}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=308)
    return await call_next(request)

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
async def index(request: Request):
    send_notification(
        "Homepage visit",
        path=str(request.url.path),
        query=request.url.query or None,
        client_ip=getattr(request.client, "host", None),
        user_agent=request.headers.get("user-agent"),
        referer=request.headers.get("referer"),
    )
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


@app.get("/apple-login.html")
async def apple_login_page():
    return FileResponse(FRONTEND / "apple-login.html")


@app.get("/demo.html")
@app.get("/soundmap-demo.html")
async def demo_page():
    return FileResponse(FRONTEND / "demo.html")


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


@app.post("/playlist-insight")
async def playlist_insight(request: Request) -> JSONResponse:
    """Infer opt-in playlist profile values from song metadata for the 3D lobe panel."""
    try:
        body = await request.json()
        user_api_key = (body.get("api_key") or "").strip()
        provider = (body.get("provider") or _default_provider()).strip().lower()
        playlist_name = (body.get("playlist_name") or "Playlist").strip()[:120]
        tracks = body.get("tracks") or []
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if provider not in ("anthropic", "openai", "nvidia"):
        raise HTTPException(status_code=400, detail="provider must be 'anthropic', 'openai', or 'nvidia'")

    api_key = user_api_key or os.environ.get(_ENV_KEY_MAP[provider], "")
    if not api_key:
        labels = {"anthropic": "Anthropic", "openai": "OpenAI", "nvidia": "NVIDIA"}
        raise HTTPException(status_code=400, detail=f"No {labels[provider]} API key configured")

    if not isinstance(tracks, list) or not tracks:
        raise HTTPException(status_code=400, detail="tracks required")

    rows: list[str] = []
    for i, track in enumerate(tracks[:120], start=1):
        if not isinstance(track, dict):
            continue
        name = str(track.get("name") or "Untitled")[:120]
        artist = str(track.get("artist") or "Unknown Artist")[:160]
        duration_ms = int(track.get("duration_ms") or 0)
        duration = f"{round(duration_ms / 1000)}s" if duration_ms > 0 else "unknown duration"
        year = str(track.get("release_year") or "").strip()
        album = str(track.get("album") or "").strip()[:100]
        genre = str(track.get("genre") or "").strip()[:80]
        details = ", ".join(part for part in (year, album, genre, duration) if part)
        rows.append(f"{i}. {name} — {artist}" + (f" ({details})" if details else ""))

    if not rows:
        raise HTTPException(status_code=400, detail="No valid tracks supplied")

    system_msg = (
        "You are a concise music analyst. Infer playlist-level listening characteristics "
        "from song titles, artist names, albums, years, and durations. Do not claim exact "
        "audio analysis or play counts. Return strict JSON only."
    )
    user_msg = (
        f"Playlist: {playlist_name}\n"
        f"Tracks:\n" + "\n".join(rows) + "\n\n"
        "Return JSON with exactly these keys:\n"
        "{"
        "\"energy\": integer 0-100, "
        "\"dance\": integer 0-100, "
        "\"tempo_bpm\": integer estimated average BPM, "
        "\"mood\": short 1-3 word feeling, "
        "\"rationale\": one short sentence"
        "}\n"
        "Mood must be a feeling phrase, not a percentage."
    )

    from .pipeline import _call_llm_chat

    try:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            None,
            lambda: _call_llm_chat(system_msg, user_msg, api_key=api_key, provider=provider, max_tokens=700),
        )
        data = _parse_llm_json(raw)
    except Exception as exc:
        print(f"[playlist-insight] LLM error: {exc}")
        raise HTTPException(status_code=502, detail=f"AI profile failed: {exc}")

    def _clamp_int(value: object, lo: int, hi: int, fallback: int) -> int:
        try:
            n = int(round(float(value)))
        except Exception:
            n = fallback
        return max(lo, min(hi, n))

    mood = str(data.get("mood") or "Textured")[:40].strip() or "Textured"
    rationale = str(data.get("rationale") or "")[:220].strip()
    return JSONResponse({
        "energy": _clamp_int(data.get("energy"), 0, 100, 50),
        "dance": _clamp_int(data.get("dance"), 0, 100, 50),
        "tempo_bpm": _clamp_int(data.get("tempo_bpm"), 40, 220, 110),
        "mood": mood,
        "rationale": rationale,
    })


@app.post("/start-job")
async def start_job(request: Request) -> JSONResponse:
    """Start pipeline job for the logged-in user."""
    token = request.session.get("access_token")
    user_id = request.session.get("user_id")
    display_name = request.session.get("display_name", user_id)
    if not token or not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    force = False
    share_for_comparison = bool(request.session.get("share_for_comparison", True))
    try:
        body = await request.json()
        force = bool(body.get("force") or request.session.pop("force_rebuild", False))
        if body.get("share_for_comparison") is not None:
            share_for_comparison = bool(body.get("share_for_comparison"))
    except Exception:
        force = bool(request.session.pop("force_rebuild", False))

    request.session["share_for_comparison"] = share_for_comparison
    job_id = submit_job(
        token,
        user_id,
        display_name,
        share_for_comparison=share_for_comparison,
        force=force,
    )
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
    map_a = storage.load_map(user_id_a)
    map_b = storage.load_map(user_id_b)
    if not map_a:
        raise HTTPException(status_code=404, detail=f"No map found for {user_id_a} — they need to process their library first")
    if not map_b:
        raise HTTPException(status_code=404, detail=f"No map found for {user_id_b} — they need to process their library first")
    if not map_a.get("share_for_comparison", True):
        raise HTTPException(status_code=403, detail=f"{map_a.get('display_name') or user_id_a} has not opted in to comparison")
    if not map_b.get("share_for_comparison", True):
        raise HTTPException(status_code=403, detail=f"{map_b.get('display_name') or user_id_b} has not opted in to comparison")

    pts_a = map_a.get("points", [])
    pts_b = map_b.get("points", [])

    def clean_key(value: str) -> str:
        return _re.sub(r"\s+", " ", (value or "").strip().lower())

    def track_key(track: dict) -> str:
        isrc = clean_key(track.get("isrc") or "")
        if isrc:
            return f"isrc:{isrc}"
        return f"name:{clean_key(track.get('name') or '')}|artist:{clean_key(track.get('artist') or '')}"

    def track_keys(track: dict) -> set[str]:
        keys: set[str] = set()
        isrc = clean_key(track.get("isrc") or "")
        name = clean_key(track.get("name") or "")
        artist = clean_key(track.get("artist") or "")
        if isrc:
            keys.add(f"isrc:{isrc}")
        if name and artist:
            keys.add(f"name:{name}|artist:{artist}")
        return keys

    def track_score(track: dict) -> float:
        return float(track.get("current_taste_score") or track.get("play_score") or 1)

    def track_payload(track: dict, score: float | None = None) -> dict:
        return {
            "id": track.get("id", ""),
            "name": track.get("name", ""),
            "artist": track.get("artist", ""),
            "album": track.get("album", ""),
            "album_art": track.get("album_art", ""),
            "external_url": track.get("external_url", ""),
            "isrc": track.get("isrc"),
            "score": round(score if score is not None else track_score(track), 3),
        }

    def keyed_tracks(pts: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for p in pts:
            for key in track_keys(p):
                current = out.get(key)
                if current is None or track_score(p) > track_score(current):
                    out[key] = p
        return out

    tracks_a = keyed_tracks(pts_a)
    tracks_b = keyed_tracks(pts_b)
    shared_track_keys = set(tracks_a) & set(tracks_b)
    shared_track_pairs: dict[str, tuple[dict, dict]] = {}
    for key in shared_track_keys:
        a_track = tracks_a[key]
        b_track = tracks_b[key]
        primary_key = track_key(a_track)
        if primary_key not in shared_track_pairs:
            shared_track_pairs[primary_key] = (a_track, b_track)

    shared_tracks = sorted(
        [
            {
                "id": a_track.get("id", ""),
                "name": a_track.get("name", ""),
                "artist": a_track.get("artist", ""),
                "album_art": a_track.get("album_art", ""),
                "external_url": a_track.get("external_url", ""),
                "isrc": a_track.get("isrc") or b_track.get("isrc"),
                "playlists_a": a_track.get("playlists") or [],
                "playlists_b": b_track.get("playlists") or [],
                "_score": track_score(a_track) + track_score(b_track),
            }
            for a_track, b_track in shared_track_pairs.values()
        ],
        key=lambda t: t["_score"],
        reverse=True,
    )
    for track in shared_tracks:
        track.pop("_score", None)

    def build_artist_profile(pts: list[dict]) -> tuple[dict[str, dict], set[str]]:
        owned_keys = {key for p in pts for key in track_keys(p)}
        artists: dict[str, dict] = {}
        for p in pts:
            names = p.get("artists") or [p.get("artist")]
            for raw_name in names:
                name = (raw_name or "").strip()
                key = clean_key(name)
                if not key:
                    continue
                entry = artists.setdefault(key, {
                    "name": name,
                    "track_count": 0,
                    "score": 0.0,
                    "tracks": {},
                })
                score = track_score(p)
                entry["track_count"] += 1
                entry["score"] += score
                tk = track_key(p)
                current = entry["tracks"].get(tk)
                if current is None or score > current["score"]:
                    entry["tracks"][tk] = {"track": p, "score": score}
        return artists, owned_keys

    def artist_recommendations(source_profile: dict[str, dict], target_keys: set[str], limit: int = 24) -> list[dict]:
        recs: list[dict] = []
        for artist_key, source_artist in source_profile.items():
            candidates = [
                item
                for tk, item in source_artist["tracks"].items()
                if tk not in target_keys
            ]
            if not candidates:
                continue
            candidates.sort(key=lambda item: item["score"], reverse=True)
            for item in candidates[:3]:
                recs.append({
                    "artist": source_artist["name"],
                    "artist_score": round(source_artist["score"], 3),
                    "source_track_count": source_artist["track_count"],
                    "track": track_payload(item["track"], item["score"]),
                })
        recs.sort(key=lambda r: (r["artist_score"], r["track"]["score"]), reverse=True)
        return recs[:limit]

    artists_a, owned_keys_a = build_artist_profile(pts_a)
    artists_b, owned_keys_b = build_artist_profile(pts_b)
    shared_artist_keys = set(artists_a) & set(artists_b)
    shared_artists = sorted(
        [
            {
                "artist": artists_a[key]["name"],
                "score_a": round(artists_a[key]["score"], 3),
                "score_b": round(artists_b[key]["score"], 3),
                "track_count_a": artists_a[key]["track_count"],
                "track_count_b": artists_b[key]["track_count"],
            }
            for key in shared_artist_keys
        ],
        key=lambda a: (a["score_a"] + a["score_b"], a["track_count_a"] + a["track_count_b"]),
        reverse=True,
    )
    recommendations_a_from_b = artist_recommendations(
        {key: artists_b[key] for key in shared_artist_keys},
        owned_keys_a,
    )
    recommendations_b_from_a = artist_recommendations(
        {key: artists_a[key] for key in shared_artist_keys},
        owned_keys_b,
    )

    def playlist_refs_for(track: dict) -> list[tuple[str, str]]:
        refs = track.get("playlist_refs") or []
        out: list[tuple[str, str]] = []
        for ref in refs:
            key = str(ref.get("id") or ref.get("name") or "").strip()
            name = str(ref.get("name") or key).strip()
            if key:
                out.append((key, name))
        if out:
            return out
        return [
            (str(name).strip(), str(name).strip())
            for name in (track.get("playlists") or [])
            if str(name).strip()
        ]

    def build_playlist_profiles(pts: list[dict]) -> dict[str, dict]:
        profiles: dict[str, dict] = {}
        for p in pts:
            tk = track_key(p)
            if not tk:
                continue
            keys = track_keys(p)
            if not keys:
                continue
            artist_keys = {clean_key(a) for a in (p.get("artists") or [p.get("artist")]) if clean_key(a)}
            for pl_key, pl_name in playlist_refs_for(p):
                entry = profiles.setdefault(pl_key, {
                    "key": pl_key,
                    "name": pl_name,
                    "track_keys": set(),
                    "artist_keys": set(),
                })
                entry["track_keys"].update(keys)
                entry["artist_keys"].update(artist_keys)
        return profiles

    def overlap_score(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / max(min(len(a), len(b)), 1)

    playlists_a = build_playlist_profiles(pts_a)
    playlists_b = build_playlist_profiles(pts_b)
    raw_pairs = []
    for pl_a in playlists_a.values():
        for pl_b in playlists_b.values():
            shared_pl_tracks = pl_a["track_keys"] & pl_b["track_keys"]
            shared_pl_artists = pl_a["artist_keys"] & pl_b["artist_keys"]
            if not shared_pl_tracks and not shared_pl_artists:
                continue
            track_overlap = overlap_score(pl_a["track_keys"], pl_b["track_keys"])
            artist_overlap = overlap_score(pl_a["artist_keys"], pl_b["artist_keys"])
            score = (track_overlap * 0.7) + (artist_overlap * 0.3)
            if score < 0.08:
                continue
            raw_pairs.append({
                "playlist_a": pl_a["name"],
                "playlist_a_key": pl_a["key"],
                "playlist_b": pl_b["name"],
                "playlist_b_key": pl_b["key"],
                "score": round(score, 3),
                "track_overlap": round(track_overlap, 3),
                "artist_overlap": round(artist_overlap, 3),
                "shared_track_count": len(shared_pl_tracks),
                "shared_artist_count": len(shared_pl_artists),
            })

    raw_pairs.sort(
        key=lambda x: (x["score"], x["shared_track_count"], x["shared_artist_count"]),
        reverse=True,
    )
    pairs = []
    used_a: set[str] = set()
    used_b: set[str] = set()
    for pair in raw_pairs:
        if pair["playlist_a_key"] in used_a or pair["playlist_b_key"] in used_b:
            continue
        used_a.add(pair["playlist_a_key"])
        used_b.add(pair["playlist_b_key"])
        pairs.append(pair)
        if len(pairs) >= 20:
            break

    library_track_overlap = overlap_score(owned_keys_a, owned_keys_b)
    library_artist_overlap = overlap_score(set(artists_a), set(artists_b))
    compatibility = round(min((library_track_overlap * 0.65) + (library_artist_overlap * 0.35), 1.0), 3)

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
        "shared_count": len(shared_track_pairs),
        "similar_playlists": pairs[:20],
        "artist_similarity": {
            "shared_artists": shared_artists[:30],
            "shared_artist_count": len(shared_artist_keys),
            "recommendations_a_from_b": recommendations_a_from_b,
            "recommendations_b_from_a": recommendations_b_from_a,
        },
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


@app.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Clear the current browser session without deleting stored map data."""
    user_id = request.session.get("user_id")
    if user_id:
        forget_user_credentials(user_id)
    request.session.clear()
    return JSONResponse({"ok": True})


@app.delete("/me/data")
async def delete_my_data(request: Request) -> JSONResponse:
    """Delete the logged-in user's stored map data and clear their session."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")

    deleted_ids = []
    ids_to_delete = {user_id}
    if user_id.endswith("_apple"):
        ids_to_delete.add(user_id[:-6])
    elif user_id.endswith("_youtube"):
        ids_to_delete.add(user_id[:-8])
    else:
        ids_to_delete.add(f"{user_id}_apple")
        ids_to_delete.add(f"{user_id}_youtube")

    for uid in ids_to_delete:
        if uid and storage.delete_map(uid):
            deleted_ids.append(uid)
        if uid:
            forget_user_credentials(uid)
            storage.delete_youtube_auth(uid[:-8] if uid.endswith("_youtube") else uid)

    request.session.clear()
    return JSONResponse({"ok": True, "deleted_user_ids": sorted(deleted_ids)})


def _youtube_base_user_id(request: Request, explicit_user_id: str = "") -> str:
    """Pick the server-side YouTube Music auth namespace for this browser/session."""
    user_id = (
        explicit_user_id
        or request.session.get("user_id")
        or os.environ.get("YTMUSIC_USER_ID")
        or "youtube"
    )
    user_id = str(user_id).strip()
    return user_id[:-8] if user_id.endswith("_youtube") else user_id


@app.get("/auth/youtube/login")
async def youtube_login(request: Request, user_id: str = "", force: int = 0) -> RedirectResponse | JSONResponse:
    """
    Start a YouTube Music map job when server-side ytmusicapi auth is configured.

    ytmusicapi is not a normal web OAuth flow. For this MVP, auth is expected at:
    user_data/ytmusic_auth/{user_id}.json or YTMUSIC_AUTH_FILE.
    """
    from .youtube_music import youtube_auth_configured

    base_user_id = _youtube_base_user_id(request, user_id)
    if not youtube_auth_configured(base_user_id):
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({
                "configured": False,
                "detail": "YouTube Music connection is not configured yet.",
                "expected_auth_file": f"user_data/ytmusic_auth/{base_user_id}.json",
            }, status_code=503)
        return RedirectResponse("/?error=youtube_not_configured")

    request.session["user_id"] = base_user_id
    request.session["display_name"] = "YouTube Music Library"
    youtube_user_id = f"{base_user_id}_youtube"
    job_id = submit_youtube_job(base_user_id, force=bool(force), share_for_comparison=True)
    return RedirectResponse(f"/loading.html?user={youtube_user_id}&job={job_id}")


@app.get("/auth/youtube/callback")
async def youtube_callback(request: Request, user_id: str = "") -> JSONResponse:
    """Document the current server-side YouTube Music auth state for the MVP flow."""
    from .youtube_music import youtube_auth_configured

    base_user_id = _youtube_base_user_id(request, user_id)
    return JSONResponse({
        "configured": youtube_auth_configured(base_user_id),
        "user_id": f"{base_user_id}_youtube",
        "message": "YouTube Music uses server-side ytmusicapi auth for this MVP.",
        "expected_auth_file": f"user_data/ytmusic_auth/{base_user_id}.json",
    })


@app.post("/youtube/start-job")
async def youtube_start_job(request: Request) -> JSONResponse:
    """Start a YouTube Music pipeline job. Body: {user_id?, force?, share_for_comparison?}."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    from .youtube_music import youtube_auth_configured

    base_user_id = _youtube_base_user_id(request, str(body.get("user_id") or ""))
    if not youtube_auth_configured(base_user_id):
        raise HTTPException(
            status_code=503,
            detail=f"YouTube Music auth missing. Add user_data/ytmusic_auth/{base_user_id}.json or set YTMUSIC_AUTH_FILE.",
        )

    force = bool(body.get("force"))
    share_for_comparison = bool(body.get("share_for_comparison", request.session.get("share_for_comparison", True)))
    request.session["user_id"] = base_user_id
    request.session["display_name"] = "YouTube Music Library"

    job_id = submit_youtube_job(base_user_id, force=force, share_for_comparison=share_for_comparison)
    return JSONResponse({"job_id": job_id, "user_id": f"{base_user_id}_youtube"})


@app.post("/youtube/search")
async def youtube_search(request: Request) -> JSONResponse:
    """Search YouTube Music for a track candidate."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    name = str(body.get("name") or body.get("track_name") or "").strip()
    artist = str(body.get("artist") or "").strip()
    album = str(body.get("album") or "").strip()
    if not name and not artist:
        raise HTTPException(status_code=400, detail="name or artist required")

    from .youtube_music import search_youtube_music_track

    try:
        track = search_youtube_music_track(name, artist, album)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"YouTube Music search failed: {exc}")
    if not track:
        raise HTTPException(status_code=404, detail="No confident YouTube Music match found")
    return JSONResponse(track)


@app.post("/youtube/playlist/create")
async def youtube_playlist_create(request: Request) -> JSONResponse:
    """Create a YouTube Music playlist and optionally add video IDs."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    base_user_id = _youtube_base_user_id(request, str(body.get("user_id") or ""))
    name = str(body.get("name") or "SoundMap Transfer").strip()
    description = str(body.get("description") or "Created by SoundMap").strip()
    video_ids = body.get("video_ids") or body.get("youtube_music_ids") or []
    if isinstance(video_ids, str):
        video_ids = [video_ids]
    if not isinstance(video_ids, list):
        raise HTTPException(status_code=400, detail="video_ids must be a list")

    from .youtube_music import add_youtube_music_playlist_items, create_youtube_music_playlist

    try:
        playlist_id = create_youtube_music_playlist(base_user_id, name, description)
        added = 0
        response = None
        if video_ids:
            result = add_youtube_music_playlist_items(base_user_id, playlist_id, [str(v) for v in video_ids])
            added = int(result.get("added") or 0)
            response = result.get("response")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"YouTube Music playlist creation failed: {exc}")

    return JSONResponse({
        "playlist_id": playlist_id,
        "name": name,
        "track_count": added,
        "url": f"https://music.youtube.com/playlist?list={playlist_id}",
        "response": response,
    })


@app.post("/youtube/playlist/add")
async def youtube_playlist_add(request: Request) -> JSONResponse:
    """Add video IDs to an existing YouTube Music playlist."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    base_user_id = _youtube_base_user_id(request, str(body.get("user_id") or ""))
    playlist_id = str(body.get("playlist_id") or "").strip()
    video_ids = body.get("video_ids") or body.get("youtube_music_ids") or []
    if isinstance(video_ids, str):
        video_ids = [video_ids]
    if not playlist_id:
        raise HTTPException(status_code=400, detail="playlist_id required")
    if not isinstance(video_ids, list) or not video_ids:
        raise HTTPException(status_code=400, detail="video_ids required")

    from .youtube_music import add_youtube_music_playlist_items

    try:
        result = add_youtube_music_playlist_items(base_user_id, playlist_id, [str(v) for v in video_ids])
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"YouTube Music playlist add failed: {exc}")
    return JSONResponse(result)


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

    try:
        body = await request.json()
        music_user_token = (body.get("music_user_token") or "").strip()
        storefront = (body.get("storefront") or "us").strip().lower()
        force = bool(body.get("force"))
        share_for_comparison = bool(body.get("share_for_comparison", request.session.get("share_for_comparison", True)))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not music_user_token:
        raise HTTPException(status_code=400, detail="music_user_token required")

    if not user_id:
        import hashlib
        user_id = "apple_" + hashlib.sha256(music_user_token.encode()).hexdigest()[:16]
        request.session["user_id"] = user_id
        request.session["display_name"] = "Apple Music Library"

    if user_id.endswith("_apple"):
        user_id = user_id[:-6]
        request.session["user_id"] = user_id

    request.session["music_user_token"] = music_user_token
    request.session["storefront"] = storefront
    request.session["share_for_comparison"] = share_for_comparison

    from .jobs import submit_apple_job
    apple_user_id = f"{user_id}_apple"
    job_id = submit_apple_job(
        music_user_token,
        user_id,
        storefront,
        force=force,
        share_for_comparison=share_for_comparison,
    )
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
async def get_map(user_id: str, request: Request) -> JSONResponse:
    data = storage.load_map(user_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Map not found — processing may still be running")
    session_user = request.session.get("user_id")
    allowed_ids = {
        session_user,
        f"{session_user}_apple" if session_user else None,
        f"{session_user}_youtube" if session_user else None,
    }
    if data.get("share_for_comparison") is False and user_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="This user has made their mind map private")
    return JSONResponse(data)


def _can_read_map(map_data: dict, requested_user_id: str, session_user_id: str | None) -> bool:
    """Apply the same privacy check used by /map/{user_id}."""
    allowed_ids = {session_user_id, f"{session_user_id}_apple" if session_user_id else None}
    allowed_ids.add(f"{session_user_id}_youtube" if session_user_id else None)
    return map_data.get("share_for_comparison") is not False or requested_user_id in allowed_ids


@app.post("/audio-features/reccobeats")
async def reccobeats_audio_features(request: Request) -> JSONResponse:
    """
    Fetch or extract ReccoBeats audio features.

    Body can be either:
    - {"track_id": "..."} or {"track_ids": ["..."]} for Spotify/ReccoBeats stored features
    - {"tracks": [{id, name, artist, isrc}, ...]} for Apple/other catalog resolution
    - {"track_id": "...", "user_id": "..."} to include track metadata from stored map data
    - {"preview_url": "https://p.scdn.co/..."} to run file-based extraction
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    preview_url = str(body.get("preview_url") or "").strip()
    track_id = str(body.get("track_id") or "").strip()
    track_ids = body.get("track_ids") or ([track_id] if track_id else [])
    if isinstance(track_ids, str):
        track_ids = [track_ids]
    if not isinstance(track_ids, list):
        raise HTTPException(status_code=400, detail="track_ids must be a list")
    user_id = str(body.get("user_id") or "").strip()
    track_meta: dict = {}
    track_objects = body.get("tracks") or []
    if track_objects and not isinstance(track_objects, list):
        raise HTTPException(status_code=400, detail="tracks must be a list")

    if track_id and user_id:
        requested_user_id = user_id or str(request.session.get("user_id") or "")
        map_data = storage.load_map(requested_user_id)
        if not map_data:
            raise HTTPException(status_code=404, detail="Map not found")
        if not _can_read_map(map_data, requested_user_id, request.session.get("user_id")):
            raise HTTPException(status_code=403, detail="This user has made their mind map private")

        point = next((p for p in map_data.get("points", []) if p.get("id") == track_id), None)
        if not point:
            raise HTTPException(status_code=404, detail="Track not found in map")
        preview_url = str(point.get("preview_url") or "").strip()
        track_meta = {
            "id": point.get("id"),
            "name": point.get("name"),
            "artist": point.get("artist"),
            "isrc": point.get("isrc"),
            "spotify_id": point.get("spotify_id"),
            "preview_url": preview_url,
        }
        track_objects = [track_meta]

    from .reccobeats import FEATURE_KEYS, extract_audio_features_from_url, get_audio_features_for_ids, get_audio_features_for_tracks

    if track_objects:
        try:
            spotify_token = request.session.get("access_token")
            loop = asyncio.get_running_loop()
            feature_map = await loop.run_in_executor(None, get_audio_features_for_tracks, track_objects, spotify_token)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        if not feature_map:
            raise HTTPException(status_code=404, detail="Audio features not found for supplied track(s)")
        storage.save_track_audio_features([
            {
                "spotify_id": data.get("spotify_id"),
                "reccobeats_id": data.get("reccobeats_id"),
                "href": data.get("href"),
                "isrc": data.get("isrc"),
                "musical_key": data.get("key"),
                "musical_mode": data.get("mode"),
                "raw": data,
                **(data.get("features") or {}),
            }
            for data in feature_map.values()
        ])
        return JSONResponse({
            "source": "reccobeats",
            "method": "spotify_search" if any(str(t.get("id") or "") != v.get("spotify_id") for t in track_objects for v in [feature_map.get(str(t.get("id") or ""), {})]) else "lookup",
            "features": feature_map,
            "track": track_meta,
        })

    if track_ids:
        try:
            loop = asyncio.get_running_loop()
            features = await loop.run_in_executor(None, get_audio_features_for_ids, track_ids)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        if not features:
            raise HTTPException(status_code=404, detail="Audio features not found for supplied track id(s)")
        storage.save_track_audio_features([
            {
                "spotify_id": row.get("spotify_id"),
                "reccobeats_id": row.get("id"),
                "href": row.get("href"),
                "isrc": row.get("isrc"),
                "musical_key": row.get("key"),
                "musical_mode": row.get("mode"),
                "raw": row,
                **{key: row.get(key) for key in FEATURE_KEYS if key in row},
            }
            for row in features
        ])
        return JSONResponse({
            "source": "reccobeats",
            "method": "lookup",
            "features": features[0] if len(features) == 1 else features,
            "track": track_meta or ({"id": track_id} if track_id else {}),
        })

    if not preview_url:
        raise HTTPException(status_code=400, detail="preview_url or track_id required")

    try:
        loop = asyncio.get_running_loop()
        features = await loop.run_in_executor(None, extract_audio_features_from_url, preview_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    return JSONResponse({
        "source": "reccobeats",
        "method": "extract",
        "features": features,
        "track": track_meta or {"preview_url": preview_url},
    })


def _resolve_to_spotify_uris(tracks: list[dict], h: dict) -> list[str]:
    """
    Convert a list of track dicts to spotify:track:ID URIs.
    Handles both Spotify tracks (direct) and Apple Music tracks (resolved via ISRC or name search).
    Each track dict: {id, name, artist, isrc}
    """
    import re
    spotify_id_re = re.compile(r'^[A-Za-z0-9]{22}$')
    uris: list[str] = []
    for t in tracks:
        tid = t.get("id") or ""
        if spotify_id_re.match(tid):
            uris.append(f"spotify:track:{tid}")
            continue
        # Apple Music ID — resolve via ISRC first, then name+artist fallback
        spotify_id = None
        isrc = t.get("isrc")
        if isrc:
            r = _requests.get(
                "https://api.spotify.com/v1/search",
                headers=h,
                params={"q": f"isrc:{isrc}", "type": "track", "limit": 1},
                timeout=10,
            )
            if r.status_code == 200:
                items = (r.json().get("tracks") or {}).get("items") or []
                if items:
                    spotify_id = items[0]["id"]
        if not spotify_id:
            name = t.get("name") or ""
            artist = t.get("artist") or ""
            if name:
                q = f"track:{name}"
                if artist:
                    q += f" artist:{artist}"
                r = _requests.get(
                    "https://api.spotify.com/v1/search",
                    headers=h,
                    params={"q": q, "type": "track", "limit": 1},
                    timeout=10,
                )
                if r.status_code == 200:
                    items = (r.json().get("tracks") or {}).get("items") or []
                    if items:
                        spotify_id = items[0]["id"]
        if spotify_id:
            uris.append(f"spotify:track:{spotify_id}")
    return uris


@app.post("/import-friend-playlist")
async def import_friend_playlist(request: Request) -> JSONResponse:
    """
    Copy a friend's playlist into the logged-in user's Spotify library.

    Body: {
      "playlist_name": str,
      "tracks": [{id, name, artist, isrc}, ...],  # full track objects for Apple Music resolution
      "friend_display_name": str
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
        tracks: list[dict] = body.get("tracks") or []
        friend_name: str = (body.get("friend_display_name") or "Friend").strip()
        target_user_id: str = (body.get("target_user_id") or "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not target_user_id:
        raise HTTPException(status_code=400, detail="target_user_id required — refresh the page and try again")
    if target_user_id != user_id:
        raise HTTPException(
            status_code=409,
            detail=f"Spotify session mismatch — this browser is logged in as {user_id}, not {target_user_id}",
        )
    if not playlist_name:
        raise HTTPException(status_code=400, detail="playlist_name required")
    if not tracks:
        raise HTTPException(status_code=400, detail="tracks required")

    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    new_name = f"{playlist_name} (from {friend_name})"
    print(f"[import-friend-playlist] user={user_id} | playlist='{new_name}' | tracks={len(tracks)}")

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

    uris = _resolve_to_spotify_uris(tracks, h)
    print(f"[import-friend-playlist] resolved {len(uris)}/{len(tracks)} tracks to Spotify URIs")

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


@app.post("/merge-friend-playlist")
async def merge_friend_playlist(request: Request) -> JSONResponse:
    """Create a new Spotify playlist from one of yours plus a friend's playlist."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    token = await _get_valid_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Spotify session expired — please log in again")

    try:
        body = await request.json()
        my_playlist_name: str = (body.get("my_playlist_name") or "").strip()
        my_playlist_label: str = (body.get("my_playlist_label") or my_playlist_name).strip()
        friend_playlist_name: str = (body.get("friend_playlist_name") or "").strip()
        friend_playlist_label: str = (body.get("friend_playlist_label") or friend_playlist_name).strip()
        friend_name: str = (body.get("friend_display_name") or "Friend").strip()
        friend_tracks: list[dict] = body.get("friend_tracks") or []
        target_user_id: str = (body.get("target_user_id") or "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not target_user_id:
        raise HTTPException(status_code=400, detail="target_user_id required — refresh the page and try again")
    if target_user_id != user_id:
        raise HTTPException(
            status_code=409,
            detail=f"Spotify session mismatch — this browser is logged in as {user_id}, not {target_user_id}",
        )
    if not my_playlist_name or not friend_playlist_name:
        raise HTTPException(status_code=400, detail="my_playlist_name and friend_playlist_name required")
    if not friend_tracks:
        raise HTTPException(status_code=400, detail="friend_tracks required")

    map_data = storage.load_map(user_id)
    if not map_data:
        raise HTTPException(status_code=404, detail="Your map was not found — rebuild your map first")

    own_ids: list[str] = []
    seen_ids: set[str] = set()
    for p in map_data.get("points", []):
        tid = p.get("id")
        p_keys = {str(ref.get("id") or ref.get("name")) for ref in (p.get("playlist_refs") or []) if ref.get("id") or ref.get("name")}
        p_keys.update(str(pl) for pl in (p.get("playlists") or []))
        if tid and tid not in seen_ids and my_playlist_name in p_keys:
            seen_ids.add(tid)
            own_ids.append(tid)
    if not own_ids:
        raise HTTPException(status_code=400, detail=f"No tracks found in your playlist: {my_playlist_label}")

    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    friend_uris = _resolve_to_spotify_uris(friend_tracks, h)
    uris: list[str] = []
    seen_uris: set[str] = set()
    for uri in [*(f"spotify:track:{tid}" for tid in own_ids), *friend_uris]:
        if uri not in seen_uris:
            seen_uris.add(uri)
            uris.append(uri)

    new_name = f"{my_playlist_label} + {friend_playlist_label}"
    create_resp = _requests.post(
        "https://api.spotify.com/v1/me/playlists",
        headers=h,
        json={
            "name": new_name,
            "public": False,
            "description": f"Merged from your playlist and {friend_name}'s SoundMap playlist",
        },
    )
    if create_resp.status_code == 403:
        raise HTTPException(status_code=403, detail="Spotify permissions missing — please reconnect Spotify")
    if create_resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Could not create playlist: {create_resp.text[:300]}")

    pl_data = create_resp.json()
    pl_id = pl_data["id"]
    pl_url = pl_data["external_urls"]["spotify"]

    added = 0
    for i in range(0, len(uris), 100):
        r = _requests.post(
            f"https://api.spotify.com/v1/playlists/{pl_id}/items",
            headers=h,
            json={"uris": uris[i:i + 100]},
        )
        if r.status_code in (200, 201):
            added += min(100, len(uris) - i)

    print(f"[merge-friend-playlist] created '{new_name}' ({added}/{len(uris)} tracks) for {user_id}")
    return JSONResponse({"name": new_name, "track_count": added, "url": pl_url})


@app.post("/import-to-apple")
async def import_to_apple(request: Request) -> JSONResponse:
    """
    Copy a friend's playlist into the Apple Music user's library.

    Body: {
      "playlist_name": str,
      "tracks": [{id, name, artist, isrc}, ...],
      "friend_display_name": str
    }
    Uses ISRC to match tracks in the Apple Music catalog, falls back to name+artist search.
    """
    try:
        body = await request.json()
        playlist_name: str = (body.get("playlist_name") or "").strip()
        tracks: list[dict] = body.get("tracks") or []
        friend_name: str = (body.get("friend_display_name") or "Friend").strip()
        music_user_token = (body.get("music_user_token") or request.session.get("music_user_token") or "").strip()
        storefront = (body.get("storefront") or request.session.get("storefront") or "us").strip().lower()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not music_user_token:
        raise HTTPException(status_code=401, detail="Apple Music session not found — reconnect Apple Music")
    if not playlist_name:
        raise HTTPException(status_code=400, detail="playlist_name required")
    if not tracks:
        raise HTTPException(status_code=400, detail="tracks required")

    request.session["music_user_token"] = music_user_token
    request.session["storefront"] = storefront

    from .apple_auth import get_developer_token
    try:
        dev_token = get_developer_token()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Apple Music not configured: {e}")

    h = {
        "Authorization": f"Bearer {dev_token}",
        "Music-User-Token": music_user_token,
    }
    base = "https://api.music.apple.com"

    print(f"[import-to-apple] storefront={storefront} playlist='{playlist_name}' tracks={len(tracks)}")

    # Resolve each track to an Apple Music catalog ID
    catalog_ids: list[str] = []
    seen_catalog_ids: set[str] = set()
    for t in tracks:
        isrc = t.get("isrc")
        apple_id = None
        if isrc:
            r = _requests.get(
                f"{base}/v1/catalog/{storefront}/songs",
                headers=h,
                params={"filter[isrc]": isrc},
                timeout=10,
            )
            if r.status_code == 200:
                data = (r.json().get("data") or [])
                if data:
                    apple_id = data[0]["id"]
        if not apple_id:
            name = t.get("name") or ""
            artist = t.get("artist") or ""
            if name:
                r = _requests.get(
                    f"{base}/v1/catalog/{storefront}/search",
                    headers=h,
                    params={"term": f"{name} {artist}".strip(), "types": "songs", "limit": 1},
                    timeout=10,
                )
                if r.status_code == 200:
                    results = ((r.json().get("results") or {}).get("songs") or {}).get("data") or []
                    if results:
                        apple_id = results[0]["id"]
        if apple_id and apple_id not in seen_catalog_ids:
            seen_catalog_ids.add(apple_id)
            catalog_ids.append(apple_id)

    print(f"[import-to-apple] resolved {len(catalog_ids)}/{len(tracks)} tracks")

    if not catalog_ids:
        raise HTTPException(status_code=422, detail="Could not match any tracks in Apple Music catalog")

    # Add the catalog songs to the user's library first. Apple may ignore IDs
    # it cannot add, but this makes subsequent playlist insertion more reliable.
    for i in range(0, len(catalog_ids), 50):
        chunk = catalog_ids[i:i + 50]
        lib_resp = _requests.post(
            f"{base}/v1/me/library",
            headers=h,
            params={"ids[songs]": ",".join(chunk)},
            timeout=20,
        )
        print(f"[import-to-apple] add-to-library batch={i // 50 + 1} status={lib_resp.status_code}")
        if lib_resp.status_code not in (202, 200, 201, 204):
            raise HTTPException(status_code=502, detail=f"Could not add songs to Apple Music library: {lib_resp.text[:200]}")

    # Create a named playlist, then add tracks through the playlist tracks endpoint
    # so failures are visible instead of reporting success for an empty playlist.
    new_name = f"{playlist_name} (from {friend_name})"
    playlist_body = {
        "attributes": {
            "name": new_name,
            "description": f"Imported from {friend_name}'s SoundMap",
            "isPublic": False,
        }
    }
    r = _requests.post(
        f"{base}/v1/me/library/playlists",
        headers={**h, "Content-Type": "application/json"},
        json=playlist_body,
        timeout=20,
    )
    print(f"[import-to-apple] create playlist status={r.status_code} body={r.text[:300]}")
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Could not create Apple Music playlist: {r.text[:200]}")

    playlist_data = r.json()
    playlist_id = ((playlist_data.get("data") or [{}])[0] or {}).get("id")
    if not playlist_id:
        raise HTTPException(status_code=502, detail="Apple Music created a playlist but did not return its ID")

    added = 0
    for i in range(0, len(catalog_ids), 100):
        chunk = catalog_ids[i:i + 100]
        add_resp = _requests.post(
            f"{base}/v1/me/library/playlists/{playlist_id}/tracks",
            headers={**h, "Content-Type": "application/json"},
            json={"data": [{"id": cid, "type": "songs"} for cid in chunk]},
            timeout=20,
        )
        print(f"[import-to-apple] add tracks batch={i // 100 + 1} status={add_resp.status_code} body={add_resp.text[:200]}")
        if add_resp.status_code not in (200, 201, 202, 204):
            raise HTTPException(status_code=502, detail=f"Could not add tracks to Apple Music playlist: {add_resp.text[:200]}")
        added += len(chunk)

    print(f"[import-to-apple] created '{new_name}' ({added}/{len(catalog_ids)} tracks) id={playlist_id}")
    return JSONResponse({"name": new_name, "track_count": added, "matched_count": len(catalog_ids), "playlist_id": playlist_id})


@app.post("/import-to-youtube")
async def import_to_youtube(request: Request) -> JSONResponse:
    """Create a YouTube Music playlist by matching supplied SoundMap tracks."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    base_user_id = _youtube_base_user_id(request, str(body.get("target_user_id") or body.get("user_id") or ""))
    playlist_name = str(body.get("playlist_display_name") or body.get("playlist_name") or "SoundMap Playlist").strip()
    friend_name = str(body.get("friend_display_name") or "friend").strip()
    tracks = body.get("tracks") or []
    if not isinstance(tracks, list) or not tracks:
        raise HTTPException(status_code=400, detail="tracks required")

    from .youtube_music import add_youtube_music_playlist_items, create_youtube_music_playlist, search_youtube_music_track

    video_ids: list[str] = []
    skipped: list[dict] = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        video_id = str(track.get("youtube_music_id") or track.get("videoId") or track.get("video_id") or "").strip()
        if not video_id:
            match = search_youtube_music_track(
                str(track.get("name") or ""),
                str(track.get("artist") or ""),
                str(track.get("album") or ""),
            )
            video_id = str((match or {}).get("youtube_music_id") or (match or {}).get("id") or "").strip()
        if video_id and video_id not in video_ids:
            video_ids.append(video_id)
        else:
            skipped.append({"name": track.get("name"), "artist": track.get("artist")})

    if not video_ids:
        raise HTTPException(status_code=422, detail="Could not match any tracks in YouTube Music")

    new_name = f"{playlist_name} (from {friend_name})"
    try:
        playlist_id = create_youtube_music_playlist(
            base_user_id,
            new_name,
            f"Imported from {friend_name}'s SoundMap",
        )
        result = add_youtube_music_playlist_items(base_user_id, playlist_id, video_ids)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"YouTube Music import failed: {exc}")

    added = int(result.get("added") or len(video_ids))
    return JSONResponse({
        "name": new_name,
        "track_count": added,
        "matched_count": len(video_ids),
        "skipped_count": len(skipped),
        "skipped": skipped[:25],
        "playlist_id": playlist_id,
        "url": f"https://music.youtube.com/playlist?list={playlist_id}",
    })


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
    Body: {"source_playlists": ["name-or-id"], "new_name": "My Merged Playlist"}
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
        p_keys = {str(ref.get("id")) for ref in (p.get("playlist_refs") or []) if ref.get("id")}
        p_keys.update(str(pl) for pl in (p.get("playlists") or []))
        if tid and tid not in seen and p_keys & source_set:
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
