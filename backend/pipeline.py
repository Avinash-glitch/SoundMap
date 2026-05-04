"""
Spotify library processing pipeline — adapted for per-user programmatic use.
Fetches tracks, computes genre embeddings, runs UMAP, saves result via storage.py.
"""

import time
import numpy as np
from typing import Callable

import requests

from . import storage

MAX_TRACKS = 1000
ARTIST_BATCH = 50

_ENV_KEY_MAP = {"nvidia": "NVIDIA_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def _default_provider() -> str:
    import os
    for p in ("nvidia", "anthropic", "openai"):
        if os.environ.get(_ENV_KEY_MAP[p]):
            return p
    return "anthropic"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_user(
    spotify_token: str,
    user_id: str,
    on_progress: Callable[[int, str], None] | None = None,
    display_name: str = "",
    api_key: str = "",
    provider: str = "",
    stop_event=None,
    share_for_comparison: bool = True,
) -> dict:
    """
    Full pipeline for one user.
    Returns the map_data dict and saves it to storage.
    Serves cached result if map is < 24 hours old.
    """
    def progress(pct: int, msg: str) -> None:
        if on_progress:
            on_progress(pct, msg)
        print(f"[pipeline] {pct:3d}% — {msg}")

    existing = storage.load_map(user_id) if storage.map_exists(user_id) else None
    existing_share = bool(existing.get("share_for_comparison", True)) if existing else None
    if existing and existing_share == bool(share_for_comparison) and storage.map_age_hours(user_id) < 24:
        progress(100, "Loaded from cache.")
        return existing

    headers = {"Authorization": f"Bearer {spotify_token}"}

    # ---- 1. Collect tracks ------------------------------------------------
    progress(5, "Fetching your library…")
    tracks, playlist_meta, pl_track_samples, remaining = _collect_tracks(headers, progress, user_id=user_id, stop_event=stop_event)

    if not tracks:
        raise RuntimeError("No tracks found in your Spotify library.")

    progress(28, "Grouping playlists into moods…")
    pl_to_mood, persona = _llm_mood_groups(playlist_meta, pl_track_samples, api_key=api_key, provider=provider or _default_provider())

    progress(30, f"Processing {len(tracks)} tracks across {len(playlist_meta)} playlists…")

    # ---- 3. Embeddings ----------------------------------------------------
    progress(35, "Analysing your music…")
    embeddings, track_genres = _genre_embeddings(tracks, headers, on_progress=progress)

    # ---- 3b. AI genre detection (enriches map labels) --------------------
    if api_key:
        progress(71, "Detecting genres with AI…")
        try:
            track_genres = _llm_genre_detect(tracks, api_key, provider or _default_provider())
        except Exception as _ge:
            print(f"[pipeline] LLM genre detection failed ({_ge}) — keeping keyword genres")

    # ---- 4. UMAP ----------------------------------------------------------
    progress(75, "Running UMAP dimensionality reduction…")
    coords = _run_umap(embeddings)

    # ---- 5. Build output --------------------------------------------------
    progress(90, "Building map…")
    map_data = _build_map_data(tracks, coords, track_genres, display_name=display_name, playlist_meta=playlist_meta, pl_to_mood=pl_to_mood, persona=persona, remaining=remaining)
    map_data["share_for_comparison"] = bool(share_for_comparison)

    storage.save_map(user_id, map_data)
    progress(100, "Done!")
    return map_data


# ---------------------------------------------------------------------------
# Track collection
# ---------------------------------------------------------------------------

def _collect_tracks(headers: dict, progress: Callable, user_id: str = "", stop_event=None) -> tuple[list[dict], list[dict], dict[str, list[str]]]:
    """
    Gather all unique tracks — no cap. Returns (tracks, playlist_meta).
    Each track gets 'playlists' (list of playlist names) and 'play_score' (int).
    stop_event: threading.Event — if set mid-fetch, skips remaining sources and
    builds the map with whatever has been collected so far.
    """
    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    # Step 1: fetch playlists once — builds pl_lookup and collects track objects
    pl_lookup: dict[str, list[str]] = {}
    pl_trks, playlist_meta, pl_track_samples = _fetch_playlists_data(headers, pl_lookup, user_id=user_id)

    # Step 2: gather from each source separately so we can score per-source
    scored: dict[str, int] = {}
    recency_scored: dict[str, int] = {}   # short-term top tracks + recently played
    depth_scored: dict[str, int] = {}     # long-term/medium top tracks + liked songs
    all_sources: list[list[dict]] = []

    def _score(tracks_list: list[dict], weight: int, cap: int | None = None,
               bucket: dict | None = None) -> None:
        target = bucket if bucket is not None else scored
        for t in tracks_list:
            tid = t.get("id")
            if not tid:
                continue
            current = target.get(tid, 0)
            add = weight if cap is None else min(weight, cap - current)
            target[tid] = current + max(add, 0)
            if bucket is not None:
                scored[tid] = scored.get(tid, 0) + max(add, 0)

    _score(pl_trks, 1)
    all_sources.append(pl_trks)

    saved = _fetch_saved_tracks(headers)
    _score(saved, 2, bucket=depth_scored)
    all_sources.append(saved)

    if not _stopped():
        top_s = _fetch_top_tracks_range(headers, "short_term")
        _score(top_s, 3, bucket=recency_scored)
        all_sources.append(top_s)
    else:
        top_s = []
    top_track_ids: set[str] = {t["id"] for t in top_s if t.get("id")}
    current_taste_scores: dict[str, int] = {
        t["id"]: max(1, 100 - i * 2)
        for i, t in enumerate(top_s[:50])
        if t.get("id")
    }

    if not _stopped():
        top_m = _fetch_top_tracks_range(headers, "medium_term")
        _score(top_m, 2, bucket=depth_scored)
        all_sources.append(top_m)

    if not _stopped():
        top_l = _fetch_top_tracks_range(headers, "long_term")
        _score(top_l, 1, bucket=depth_scored)
        all_sources.append(top_l)

    if not _stopped():
        recent = _fetch_recent_tracks(headers)
        _score(recent, 1, cap=3, bucket=recency_scored)
        all_sources.append(recent)
    elif stop_event and stop_event.is_set():
        progress(20, "Stopped early — building map with tracks collected so far…")

    # Step 3: deduplicate by track id (preserve first occurrence for track metadata)
    seen: dict[str, dict] = {}
    for t in (t for source in all_sources for t in source):
        tid = t.get("id")
        if tid and tid not in seen:
            seen[tid] = t

    # Step 4: assign play_score + per-dimension scores — no hard cap, include everything
    all_candidates = list(seen.values())
    for t in all_candidates:
        t["playlists"] = pl_lookup.get(t["id"], [])
        t["play_score"] = max(scored.get(t["id"], 1), 1)
        t["recency_score"] = recency_scored.get(t["id"], 0)
        t["depth_score"] = depth_scored.get(t["id"], 0)
        t["is_top_track"] = t["id"] in top_track_ids
        t["current_taste_score"] = current_taste_scores.get(t["id"], 0)

    tracks = sorted(all_candidates, key=lambda t: t["play_score"], reverse=True)

    # Step 5: compute remaining = liked tracks not in any user-owned playlist
    liked_by_id: dict[str, dict] = {t["id"]: t for t in saved if t.get("id")}
    playlist_ids: set[str] = set(pl_lookup.keys())
    remaining: list[dict] = [
        t for tid, t in liked_by_id.items()
        if tid not in playlist_ids
    ]
    remaining.sort(key=lambda t: t.get("liked_at") or "", reverse=True)
    remaining_slim = [
        {k: t[k] for k in ("id", "name", "artist", "album_art", "liked_at", "release_year", "isrc") if k in t}
        for t in remaining
    ]

    tagged = sum(1 for t in tracks if t["playlists"])
    with_preview = sum(1 for t in tracks if t.get("preview_url"))
    max_score = max((t["play_score"] for t in tracks), default=1)
    min_score = min((t["play_score"] for t in tracks), default=1)
    print(f"[pipeline] {len(all_candidates)} total tracks (range {min_score}–{max_score}), {tagged} in playlists, {with_preview} previews, {len(remaining_slim)} remaining")
    return tracks, playlist_meta, pl_track_samples, remaining_slim


def _spotify_get(url: str, headers: dict, params: dict | None = None, retries: int = 3) -> requests.Response:
    """GET with automatic retry on 429 (respects Retry-After header)."""
    for attempt in range(retries):
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"[pipeline] 429 rate limit — waiting {wait}s…")
            time.sleep(wait)
            continue
        return resp
    return resp  # return last response after exhausting retries


def _fetch_playlists_data(
    headers: dict,
    pl_lookup: dict[str, list[str]],
    user_id: str = "",
) -> tuple[list[dict], list[dict], dict[str, list[str]]]:
    """
    Single pass over the user's playlists.
    Only reads tracks from playlists OWNED by the user (followed/label playlists return 403).
    Uses /playlists/{id}/items (newer endpoint).
    Returns (track_objects, playlist_meta, pl_track_samples) and builds pl_lookup in-place.
    pl_track_samples: {playlist_name: ["Track — Artist", ...]} up to 10 per playlist.
    """
    playlists: list[dict] = []
    pl_list_url: str | None = "https://api.spotify.com/v1/me/playlists"
    base_pl_url = pl_list_url
    while pl_list_url:
        pl_resp = _spotify_get(pl_list_url, headers, params={"limit": 50} if pl_list_url == base_pl_url else None)
        if pl_resp.status_code != 200:
            print(f"[pipeline] /me/playlists failed: HTTP {pl_resp.status_code}")
            return [], [], {}
        body = pl_resp.json()
        playlists.extend(body.get("items", []))
        pl_list_url = body.get("next")

    # Only include playlists this user owns or collaborates on
    owned = [
        p for p in playlists
        if p and p.get("id") and (
            (p.get("owner") or {}).get("id") == user_id
            or p.get("collaborative", False)
        )
    ]
    skipped = len(playlists) - len(owned)
    if skipped:
        print(f"[pipeline] skipping {skipped} followed/external playlists (not owned)")

    playlist_meta = [
        {"id": p["id"], "name": p.get("name", p["id"])}
        for p in owned
    ]

    track_objects: list[dict] = []
    pl_track_samples: dict[str, list[str]] = {}

    for pl in owned:
        pl_id = pl["id"]
        pl_name = pl.get("name", pl_id)
        pl_track_samples[pl_name] = []

        base_url = f"https://api.spotify.com/v1/playlists/{pl_id}/items"
        url: str | None = base_url
        while url:
            # Only pass params on the first request — pagination URLs already
            # contain limit/offset and adding params again causes duplicates.
            resp = _spotify_get(url, headers, params={"limit": 50} if url == base_url else None)
            if resp.status_code != 200:
                print(f"[pipeline] playlist '{pl_name}': HTTP {resp.status_code}")
                break
            data = resp.json()

            for item in data.get("items", []):
                # Spotify returns "track" key for tracks (both old and new endpoint)
                t = (item or {}).get("track") or (item or {}).get("item")
                if not t or not t.get("id") or t.get("type") == "episode":
                    continue
                tid = t["id"]
                # Always register in pl_lookup so playlist labels are correct
                # even if this track ends up below the play_score cut-off
                if tid not in pl_lookup:
                    pl_lookup[tid] = []
                if pl_name not in pl_lookup[tid]:
                    pl_lookup[tid].append(pl_name)
                track_objects.append(_normalise_track(t))
                if len(pl_track_samples[pl_name]) < 15:
                    artists = t.get("artists", [])
                    artist_name = artists[0]["name"] if artists else ""
                    pl_track_samples[pl_name].append(f'{t.get("name", "")} — {artist_name}')

            url = data.get("next")
        time.sleep(0.05)

    print(f"[pipeline] playlists: {len(track_objects)} tracks across {len(owned)} owned playlists, lookup={len(pl_lookup)} IDs")
    return track_objects, playlist_meta, pl_track_samples


def _fetch_saved_tracks(headers: dict) -> list[dict]:
    """Fetch all of the user's liked tracks. Attaches liked_at from the item wrapper."""
    out: list[dict] = []
    url = "https://api.spotify.com/v1/me/tracks"
    while url:
        resp = _spotify_get(url, headers, params={"limit": 50})
        if resp.status_code != 200:
            break
        data = resp.json()
        for item in data.get("items", []):
            t = item.get("track")
            if t and t.get("id"):
                track = _normalise_track(t)
                track["liked_at"] = item.get("added_at")  # ISO 8601 e.g. "2023-04-15T12:30:00Z"
                out.append(track)
        url = data.get("next")
        if len(out) % 200 == 0 and len(out) > 0:
            print(f"[pipeline] liked tracks fetched so far: {len(out)}")
    print(f"[pipeline] liked tracks total: {len(out)}")
    return out




def _fetch_top_tracks_range(headers: dict, time_range: str) -> list[dict]:
    resp = requests.get(
        f"https://api.spotify.com/v1/me/top/tracks?limit=50&time_range={time_range}",
        headers=headers, timeout=15,
    )
    if resp.status_code != 200:
        return []
    return [
        _normalise_track(t)
        for t in resp.json().get("items", [])
        if t and t.get("id")
    ]


def _fetch_recent_tracks(headers: dict) -> list[dict]:
    resp = requests.get(
        "https://api.spotify.com/v1/me/player/recently-played?limit=50",
        headers=headers, timeout=15,
    )
    if resp.status_code != 200:
        return []
    out: list[dict] = []
    for item in resp.json().get("items", []):
        t = item.get("track")
        if t and t.get("id"):
            out.append(_normalise_track(t))
    return out


def _normalise_track(t: dict) -> dict:
    artists = t.get("artists", [])
    album = t.get("album", {}) or {}
    release_date = album.get("release_date", "") or ""
    release_year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None
    return {
        "id": t["id"],
        "name": t.get("name", ""),
        "artist": artists[0]["name"] if artists else "",
        "artists": [a["name"] for a in artists],
        "artist_ids": [a["id"] for a in artists if a.get("id")],
        "album": album.get("name", ""),
        "album_art": (
            album.get("images", [{}])[0].get("url", "")
            if album.get("images") else ""
        ),
        "preview_url": t.get("preview_url"),
        "external_url": t.get("external_urls", {}).get("spotify", ""),
        "duration_ms": t.get("duration_ms", 0),
        "popularity": t.get("popularity", 0),
        "release_year": release_year,
        "isrc": (t.get("external_ids") or {}).get("isrc"),
    }


# ---------------------------------------------------------------------------
# Genre-based embeddings
# ---------------------------------------------------------------------------

def _genre_embeddings(
    tracks: list[dict],
    headers: dict,
    on_progress: Callable | None = None,
) -> np.ndarray:
    """
    Build embeddings from artist genres + track popularity.
    Uses artist_ids stored on each track during normalisation.
    """
    # Collect unique artist IDs from already-normalised tracks
    seen_artists: set[str] = set()
    unique_artist_ids: list[str] = []
    for t in tracks:
        for aid in t.get("artist_ids", []):
            if aid not in seen_artists:
                seen_artists.add(aid)
                unique_artist_ids.append(aid)

    # Fetch artist objects in batches to get genre tags
    genre_map: dict[str, list[str]] = {}  # artist_id → genres
    for i in range(0, len(unique_artist_ids), ARTIST_BATCH):
        batch = unique_artist_ids[i: i + ARTIST_BATCH]
        resp = requests.get(
            "https://api.spotify.com/v1/artists",
            params={"ids": ",".join(batch)},
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            for artist in resp.json().get("artists") or []:
                if artist and artist.get("id"):
                    genres = artist.get("genres", [])
                    genre_map[artist["id"]] = genres
                    if genres:
                        print(f"[pipeline] {artist['name']}: {genres}")

        pct = 35 + int((i / max(len(unique_artist_ids), 1)) * 35)
        if on_progress:
            on_progress(pct, f"Fetched genres for {min(i+ARTIST_BATCH, len(unique_artist_ids))}/{len(unique_artist_ids)} artists…")
        time.sleep(0.05)

    # Build genre vocabulary
    all_genres: set[str] = set()
    for genres in genre_map.values():
        all_genres.update(genres)
    genre_vocab = sorted(all_genres)
    genre_index = {g: i for i, g in enumerate(genre_vocab)}
    n_genres = max(len(genre_vocab), 1)

    # Build playlist vocabulary for one-hot features
    all_playlists: list[str] = []
    seen_pl: set[str] = set()
    for t in tracks:
        for pl in t.get("playlists", []):
            if pl not in seen_pl:
                seen_pl.add(pl)
                all_playlists.append(pl)
    pl_index = {pl: i for i, pl in enumerate(all_playlists)}
    n_pl = max(len(all_playlists), 1)

    # Feature matrix: genre one-hot + playlist one-hot (4x weight) + popularity
    # 4x playlist weight means UMAP's KNN graph treats same-playlist tracks as close neighbours
    n_cols = n_genres + n_pl * 4 + 1
    matrix = np.zeros((len(tracks), n_cols), dtype=float)
    track_genres: list[str] = []

    for ti, track in enumerate(tracks):
        genres_for_track: list[str] = []
        for aid in track.get("artist_ids", []):
            genres_for_track.extend(genre_map.get(aid, []))

        for genre in genres_for_track:
            if genre in genre_index:
                matrix[ti, genre_index[genre]] = 1.0

        # 4x weight: fills 4 columns per playlist so playlist co-membership
        # dominates the KNN neighbourhood in UMAP
        for pl in track.get("playlists", []):
            if pl in pl_index:
                base = n_genres + pl_index[pl] * 4
                matrix[ti, base]     = 1.0
                matrix[ti, base + 1] = 1.0
                matrix[ti, base + 2] = 1.0
                matrix[ti, base + 3] = 1.0

        matrix[ti, n_cols - 1] = (track.get("popularity", 50) or 50) / 100.0
        track_genres.append(_primary_genre(genres_for_track))

    return matrix, track_genres


# Broad genre buckets — order matters: first match wins
_GENRE_BUCKETS: list[tuple[str, list[str]]] = [
    ("metal",       ["metal", "heavy", "death", "black metal", "doom", "thrash", "hardcore"]),
    ("hip-hop",     ["hip hop", "hip-hop", "rap", "trap", "drill", "grime", "r&b", "rnb",
                     "soul", "funk", "rhythm and blues"]),
    ("electronic",  ["electronic", "edm", "house", "techno", "trance", "dubstep", "dnb",
                     "drum and bass", "ambient", "synth", "electro", "dance", "idm",
                     "breakbeat", "garage", "footwork", "club"]),
    ("rock",        ["rock", "punk", "grunge", "alternative", "indie", "post-rock",
                     "shoegaze", "emo", "noise", "stoner", "psych"]),
    ("pop",         ["pop", "bedroom pop", "bubblegum", "dream pop"]),
    ("jazz",        ["jazz", "blues", "swing", "bebop", "bossa", "fusion", "big band",
                     "soul jazz", "afrobeat"]),
    ("classical",   ["classical", "orchestr", "chamber", "opera", "baroque", "contemporary classical",
                     "modern composition", "minimalism"]),
    ("folk",        ["folk", "singer-songwriter", "acoustic", "country", "bluegrass",
                     "americana", "roots", "celtic", "traditional"]),
    ("latin",       ["latin", "reggaeton", "salsa", "cumbia", "flamenco", "tango",
                     "samba", "bossa nova", "reggae", "afropop", "afrobeats", "dancehall"]),
    ("world",       ["world", "k-pop", "j-pop", "anime", "bollywood", "afro", "global"]),
]


def _primary_genre(genres: list[str]) -> str:
    """Map a list of raw Spotify genre tags to one broad bucket."""
    if not genres:
        return "other"
    combined = " ".join(genres).lower()
    for bucket, keywords in _GENRE_BUCKETS:
        if any(kw in combined for kw in keywords):
            return bucket
    # Last resort: return the first raw genre tag shortened, so at least it's labelled
    first = genres[0].lower()
    return first if len(first) <= 20 else "other"


# ---------------------------------------------------------------------------
# UMAP
# ---------------------------------------------------------------------------

def _run_umap(embeddings: np.ndarray) -> np.ndarray:
    from umap import UMAP

    n = len(embeddings)
    n_neighbors = min(15, n - 1)
    reducer = UMAP(n_components=2, n_neighbors=n_neighbors, min_dist=0.1, random_state=42)
    return reducer.fit_transform(embeddings)


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _call_llm_chat(system: str, user: str, api_key: str, provider: str = "anthropic", max_tokens: int = 1200) -> str:
    """Route a chat completion to Anthropic, OpenAI, or NVIDIA NIM (GLM-5.1)."""
    if provider == "nvidia":
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://integrate.api.nvidia.com/v1")
        resp = client.chat.completions.create(
            model="z-ai/glm-5.1",
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content.strip()
    elif provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content.strip()
    else:  # anthropic default
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text.strip()


_GENRE_BUCKETS_SET = {"metal", "hip-hop", "electronic", "rock", "pop", "jazz", "classical", "folk", "latin", "world", "other"}


def _llm_genre_detect(
    tracks: list[dict],
    api_key: str,
    provider: str = "anthropic",
) -> list[str]:
    """
    Batch-classify each track's genre using an LLM.
    Falls back to keyword bucketing on failure.
    """
    import json
    import re as _re

    fallback = ["other"] * len(tracks)

    system_msg = (
        "You are a music genre classifier. For each numbered track, assign exactly one genre "
        "from this fixed list: metal, hip-hop, electronic, rock, pop, jazz, classical, folk, latin, world, other.\n"
        "Use Spotify genre tags when provided, otherwise use your knowledge of the artist.\n"
        'Respond ONLY with valid JSON: {"genres": ["genre1", "genre2", ...]}'
    )

    results = list(fallback)
    BATCH = 100  # larger batches = fewer API calls, stays under rate limit

    for batch_idx, batch_start in enumerate(range(0, len(tracks), BATCH)):
        batch = tracks[batch_start: batch_start + BATCH]
        lines = []
        for i, t in enumerate(batch):
            lines.append(f'{batch_start + i}. "{t["name"]}" by {t["artist"]}')
        user_msg = "\n".join(lines)

        try:
            raw = _call_llm_chat(system_msg, user_msg, api_key, provider, max_tokens=1200)
            m = _re.search(r'\{.*\}', raw, _re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group(0))
            for i, genre in enumerate(data.get("genres", [])):
                idx = batch_start + i
                if idx < len(results) and genre in _GENRE_BUCKETS_SET:
                    results[idx] = genre
        except Exception as exc:
            print(f"[pipeline] LLM genre batch {batch_idx + 1} failed ({exc})")

        # Pace requests to stay under 40 RPM (1.5s gap = safe for concurrent users)
        if batch_start + BATCH < len(tracks):
            time.sleep(1.5)

    detected = sum(1 for g in results if g != "other")
    print(f"[pipeline] LLM genre detection: {detected}/{len(tracks)} tracks classified")
    return results


# ---------------------------------------------------------------------------
# LLM mood grouping
# ---------------------------------------------------------------------------

def _llm_mood_groups(
    playlist_meta: list[dict],
    pl_track_samples: dict[str, list[str]],
    api_key: str = "",
    provider: str = "anthropic",
) -> tuple[dict[str, str], str]:
    """
    Group playlists into persona-aware mood categories using Claude.
    Returns ({playlist_name: mood_name}, persona_string).
    Falls back to ({}, "") if no API key or call fails.
    """
    import json

    if not api_key or not playlist_meta:
        return {}, ""

    try:
        # Build the user message with playlist names + track samples
        lines = ["Playlists and sample tracks:"]
        for p in playlist_meta:
            name = p["name"]
            samples = pl_track_samples.get(name, [])
            lines.append(f'\n{name}:')
            for s in samples:
                lines.append(f'  - {s}')
        user_msg = "\n".join(lines)

        system_msg = (
            "You are a music curator building mood zones for a Spotify listening map. "
            "You will receive playlist names and up to 10 sample tracks per playlist. "
            "\n\n"
            "CRITICAL RULE — sonic coherence: tracks in the same mood category must "
            "actually sound good played back-to-back. NEVER group playlists whose sample "
            "tracks come from clearly different sonic worlds into the same category. "
            "For example: techno and jazz must be in separate categories; "
            "metal and pop must be separate; classical and hip-hop must be separate; "
            "ambient and drum-and-bass must be separate. When in doubt, split into more "
            "categories rather than merging incompatible sounds. "
            "\n\n"
            "First, infer the user's listener persona in one short sentence. "
            "Then assign each playlist to exactly one mood category. "
            "Use between 3 and 6 categories maximum — merge similar sounds rather than splitting. "
            "Name each category evocatively in 2–3 words that match the sonic character "
            "of the tracks — NOT just the time-of-day or mood adjective. "
            "\n\n"
            'Respond ONLY with valid JSON, no markdown:\n'
            '{"persona": "...", "categories": [{"mood": "...", "playlists": ["...", "..."]}, ...]}'
        )

        print(f"[pipeline] sending {len(playlist_meta)} playlists to LLM ({provider}) for mood grouping")
        import re as _re
        raw = _call_llm_chat(system_msg, user_msg, api_key, provider, max_tokens=1200)
        print(f"[pipeline] LLM raw response: {raw[:500]}")
        match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if not match:
            raise ValueError(f"No JSON object in LLM response: {raw[:200]}")
        data = json.loads(match.group(0))

        persona = data.get("persona", "")
        pl_to_mood: dict[str, str] = {}
        for cat in data.get("categories", []):
            mood = cat.get("mood", "Uncharted")
            for pl_name in cat.get("playlists", []):
                pl_to_mood[pl_name] = mood

        moods = sorted(set(pl_to_mood.values()))
        print(f"[pipeline] LLM persona: \"{persona}\" — {len(moods)} moods: {', '.join(moods)}")
        return pl_to_mood, persona

    except Exception as exc:
        print(f"[pipeline] LLM mood grouping failed ({exc}) — using Uncharted fallback")
        return {}, ""


# ---------------------------------------------------------------------------
# Build output
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Apple Music pipeline
# ---------------------------------------------------------------------------

def _apple_genre_embeddings(tracks: list[dict]) -> tuple[np.ndarray, list[str]]:
    """Build feature matrix for Apple Music tracks using Apple-provided genre tags + playlists."""
    BUCKETS = ["metal", "hip-hop", "electronic", "rock", "pop", "jazz", "classical", "folk", "latin", "world", "other"]
    bucket_index = {b: i for i, b in enumerate(BUCKETS)}
    n_buckets = len(BUCKETS)

    all_playlists: list[str] = []
    seen_pl: set[str] = set()
    for t in tracks:
        for pl in t.get("playlists", []):
            if pl not in seen_pl:
                seen_pl.add(pl)
                all_playlists.append(pl)
    pl_index = {pl: i for i, pl in enumerate(all_playlists)}
    n_pl = max(len(all_playlists), 1)

    n_cols = n_buckets + n_pl * 4
    matrix = np.zeros((len(tracks), n_cols), dtype=float)
    track_genres: list[str] = []

    for ti, t in enumerate(tracks):
        bucket = _primary_genre(t.get("genre_tags") or [])
        track_genres.append(bucket)
        if bucket in bucket_index:
            matrix[ti, bucket_index[bucket]] = 1.0
        for pl in t.get("playlists", []):
            if pl in pl_index:
                base = n_buckets + pl_index[pl] * 4
                for offset in range(4):
                    matrix[ti, base + offset] = 1.0

    return matrix, track_genres


def _fetch_apple_library(
    dev_token: str,
    music_user_token: str,
    storefront: str = "us",
    on_progress=None,
) -> list[dict]:
    """Fetch all tracks from an Apple Music library via the Apple Music API."""
    headers = {
        "Authorization": f"Bearer {dev_token}",
        "Music-User-Token": music_user_token,
    }
    base_url = "https://api.music.apple.com"

    def _get_all(path: str) -> list[dict]:
        items: list[dict] = []
        url = f"{base_url}{path}?limit=100"
        while url:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code != 200:
                print(f"[apple] {path}: HTTP {r.status_code} — {r.text[:200]}")
                break
            data = r.json()
            items.extend(data.get("data", []))
            nxt = data.get("next")
            url = f"{base_url}{nxt}" if nxt else None
        return items

    if on_progress:
        on_progress(5, "Fetching Apple Music playlists…")

    playlists = _get_all("/v1/me/library/playlists")
    n_pl = max(len(playlists), 1)
    track_to_pls: dict[str, list[str]] = {}
    all_track_attrs: dict[str, dict] = {}

    def _merge_attrs(tid: str, attrs: dict) -> None:
        if not tid:
            return
        current = all_track_attrs.setdefault(tid, {})
        for key, value in (attrs or {}).items():
            if value and not current.get(key):
                current[key] = value

    for i, pl in enumerate(playlists):
        pl_name = (pl.get("attributes") or {}).get("name") or f"Playlist {i + 1}"
        pl_id = pl["id"]
        if on_progress:
            on_progress(5 + int(i / n_pl * 40), f"Reading '{pl_name}'…")
        for t in _get_all(f"/v1/me/library/playlists/{pl_id}/tracks"):
            tid = t.get("id")
            if not tid:
                continue
            attrs = t.get("attributes") or {}
            _merge_attrs(tid, attrs)
            track_to_pls.setdefault(tid, []).append(pl_name)

    if on_progress:
        on_progress(50, "Fetching library songs…")

    for t in _get_all("/v1/me/library/songs"):
        tid = t.get("id")
        _merge_attrs(tid, t.get("attributes") or {})

    if on_progress:
        on_progress(53, "Checking recent Apple Music plays…")

    heavy_items = _get_all("/v1/me/history/heavy-rotation")
    recent_items = _get_all("/v1/me/recent/played/tracks")
    recent_rank: dict[str, int] = {}
    current_taste_rank: dict[str, int] = {}
    for rank, item in enumerate([*heavy_items[:50], *recent_items[:50]]):
        attrs = item.get("attributes") or {}
        keys = {
            item.get("id") or "",
            attrs.get("isrc") or "",
            f"{(attrs.get('name') or '').strip().lower()}|{(attrs.get('artistName') or '').strip().lower()}",
        }
        play_params = attrs.get("playParams") or {}
        keys.add(play_params.get("id") or "")
        keys.add(play_params.get("catalogId") or "")
        for key in keys:
            if key and key not in recent_rank:
                recent_rank[key] = rank
            if key and key not in current_taste_rank:
                current_taste_rank[key] = rank

    if on_progress:
        on_progress(55, f"Processing {len(all_track_attrs)} tracks…")

    tracks_out: list[dict] = []
    for tid, attrs in all_track_attrs.items():
        genre_names: list[str] = attrs.get("genreNames") or []
        artwork = attrs.get("artwork") or {}
        art_url = artwork.get("url", "").replace("{w}", "60").replace("{h}", "60")
        rd = attrs.get("releaseDate") or ""
        release_year = int(rd[:4]) if len(rd) >= 4 and rd[:4].isdigit() else None
        date_added = attrs.get("dateAdded") or attrs.get("addedDate") or ""
        track_key = f"{(attrs.get('name') or '').strip().lower()}|{(attrs.get('artistName') or '').strip().lower()}"
        recent_pos = min(
            (
                recent_rank[key]
                for key in (tid, attrs.get("isrc") or "", track_key)
                if key in recent_rank
            ),
            default=None,
        )
        current_pos = min(
            (
                current_taste_rank[key]
                for key in (tid, attrs.get("isrc") or "", track_key)
                if key in current_taste_rank
            ),
            default=None,
        )

        tracks_out.append({
            "id": tid,
            "name": attrs.get("name") or "",
            "artist": attrs.get("artistName") or "",
            "artists": [attrs.get("artistName")] if attrs.get("artistName") else [],
            "artist_ids": [],
            "album": attrs.get("albumName") or "",
            "album_art": art_url,
            "preview_url": None,
            "external_url": "",
            "duration_ms": attrs.get("durationInMillis") or 0,
            "popularity": 50,
            "release_year": release_year,
            "isrc": attrs.get("isrc"),
            "playlists": track_to_pls.get(tid, []),
            "genre_tags": [g.lower() for g in genre_names[:5]],
            "play_score": 100 - recent_pos if recent_pos is not None else 1,
            "liked_at": date_added,
            "is_top_track": recent_pos is not None,
            "current_taste_score": 100 - current_pos if current_pos is not None else 0,
        })

    if not any(t.get("is_top_track") for t in tracks_out):
        dated = sorted(
            [t for t in tracks_out if t.get("liked_at")],
            key=lambda t: t.get("liked_at") or "",
            reverse=True,
        )
        for rank, track in enumerate(dated[:50]):
            track["is_top_track"] = True
            track["play_score"] = max(track.get("play_score", 1), 100 - rank)
            track["current_taste_score"] = max(track.get("current_taste_score", 0), 100 - rank)

    print(f"[apple] library: {len(tracks_out)} tracks across {len(playlists)} playlists")
    return tracks_out


def process_apple_user(
    music_user_token: str,
    user_id: str,
    storefront: str = "us",
    on_progress=None,
    api_key: str = "",
    provider: str = "",
    force: bool = False,
    share_for_comparison: bool = True,
) -> dict:
    """
    Full pipeline for an Apple Music library.
    Saves map as '{user_id}_apple' in storage.
    """
    from .apple_auth import get_developer_token

    def progress(pct: int, msg: str) -> None:
        if on_progress:
            on_progress(pct, msg)
        print(f"[apple-pipeline] {pct:3d}% — {msg}")

    apple_id = f"{user_id}_apple"

    if not force and storage.map_exists(apple_id) and storage.map_age_hours(apple_id) < 24:
        progress(100, "Loaded from cache.")
        cached = storage.load_map(apple_id)
        if cached is not None and cached.get("share_for_comparison", True) != bool(share_for_comparison):
            cached["share_for_comparison"] = bool(share_for_comparison)
            storage.save_map(apple_id, cached)
        return cached

    progress(3, "Connecting to Apple Music…")
    dev_token = get_developer_token()

    tracks = _fetch_apple_library(
        dev_token, music_user_token, storefront,
        on_progress=lambda p, m: progress(p, m),
    )
    if not tracks:
        raise RuntimeError("No tracks found in your Apple Music library.")

    progress(58, f"Analysing {len(tracks)} tracks…")
    embeddings, track_genres = _apple_genre_embeddings(tracks)

    if api_key:
        progress(65, "Detecting genres with AI…")
        try:
            track_genres = _llm_genre_detect(tracks, api_key, provider or _default_provider())
        except Exception as exc:
            print(f"[apple-pipeline] LLM genre detection failed ({exc}) — keeping keyword genres")

    progress(75, "Running UMAP…")
    coords = _run_umap(embeddings)

    progress(90, "Building map…")
    playlist_track_ids = {t["id"] for t in tracks if t.get("playlists")}
    remaining_slim = [
        {"id": t["id"], "name": t["name"], "artist": t["artist"],
         "album_art": t["album_art"], "liked_at": t.get("liked_at") or "", "release_year": t.get("release_year")}
        for t in tracks if t["id"] not in playlist_track_ids
    ]

    playlist_names = sorted({pl for t in tracks for pl in t.get("playlists", [])})
    map_data = _build_map_data(
        tracks, coords, track_genres,
        display_name="Apple Music Library",
        playlist_meta=[{"id": pl, "name": pl} for pl in playlist_names],
        remaining=remaining_slim,
    )
    map_data["source"] = "apple"
    map_data["share_for_comparison"] = bool(share_for_comparison)

    storage.save_map(apple_id, map_data)
    progress(100, "Done!")
    return map_data


def _build_map_data(
    tracks: list[dict],
    coords: np.ndarray,
    track_genres: list[str],
    display_name: str = "",
    playlist_meta: list[dict] | None = None,
    pl_to_mood: dict[str, str] | None = None,
    persona: str = "",
    remaining: list[dict] | None = None,
) -> dict:
    pl_to_mood = pl_to_mood or {}
    points = []
    for track, (x, y), genre in zip(tracks, coords, track_genres):
        pl = (track.get("playlists") or [None])[0]
        mood = pl_to_mood.get(pl, "Uncharted") if pl else "Uncharted"
        points.append({
            **track,
            "x": float(x),
            "y": float(y),
            "genre": genre,
            "mood": mood,
            "is_top_track": bool(track.get("is_top_track")),
        })

    return {
        "points": points,
        "generated_at": int(time.time()),
        "track_count": len(points),
        "embedding_method": "genres",
        "display_name": display_name,
        "playlists": playlist_meta or [],
        "moods": sorted(set(p["mood"] for p in points)),
        "persona": persona,
        "remaining": remaining or [],
    }
