"""
Spotify library processing pipeline — adapted for per-user programmatic use.
Fetches tracks, computes embeddings (CLAP or audio features), runs UMAP,
saves result via storage.py.
"""

import time
import numpy as np
from typing import Callable

import requests

from . import storage

try:
    from msclap import CLAP
    CLAP_AVAILABLE = True
except ImportError:
    CLAP_AVAILABLE = False

MAX_TRACKS = 500
ARTIST_BATCH = 50
PREVIEW_CONCURRENCY = 5


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_user(
    spotify_token: str,
    user_id: str,
    on_progress: Callable[[int, str], None] | None = None,
    display_name: str = "",
    api_key: str = "",
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

    if storage.map_exists(user_id) and storage.map_age_hours(user_id) < 24:
        progress(100, "Loaded from cache.")
        return storage.load_map(user_id)

    headers = {"Authorization": f"Bearer {spotify_token}"}

    # ---- 1. Collect tracks ------------------------------------------------
    progress(5, "Fetching your library…")
    tracks, playlist_meta, pl_track_samples, remaining = _collect_tracks(headers, progress, user_id=user_id)

    if not tracks:
        raise RuntimeError("No tracks found in your Spotify library.")

    progress(28, "Grouping playlists into moods…")
    pl_to_mood, persona = _llm_mood_groups(playlist_meta, pl_track_samples, api_key=api_key)

    progress(30, f"Processing {len(tracks)} tracks across {len(playlist_meta)} playlists…")

    # ---- 3. Embeddings ----------------------------------------------------
    track_genres: list[str] = ["other"] * len(tracks)

    if CLAP_AVAILABLE:
        progress(35, "Computing CLAP audio embeddings (this takes a while)…")
        embeddings = _clap_embeddings(tracks, headers, on_progress=progress)
    else:
        progress(35, "Fetching audio features…")
        embeddings, track_genres = _audio_feature_embeddings(tracks, headers, on_progress=progress)

    # ---- 4. UMAP ----------------------------------------------------------
    progress(75, "Running UMAP dimensionality reduction…")
    coords = _run_umap(embeddings)

    # ---- 5. Build output --------------------------------------------------
    progress(90, "Building map…")
    map_data = _build_map_data(tracks, coords, track_genres, display_name=display_name, playlist_meta=playlist_meta, pl_to_mood=pl_to_mood, persona=persona, remaining=remaining)

    storage.save_map(user_id, map_data)
    progress(100, "Done!")
    return map_data


# ---------------------------------------------------------------------------
# Track collection
# ---------------------------------------------------------------------------

def _collect_tracks(headers: dict, progress: Callable, user_id: str = "") -> tuple[list[dict], list[dict], dict[str, list[str]]]:
    """
    Gather up to MAX_TRACKS unique tracks. Returns (tracks, playlist_meta).
    Each track gets 'playlists' (list of playlist names) and 'play_score' (int).
    play_score is a personal listening-frequency proxy scored across data sources.
    """
    # Step 1: fetch playlists once — builds pl_lookup and collects track objects
    pl_lookup: dict[str, list[str]] = {}
    pl_trks, playlist_meta, pl_track_samples = _fetch_playlists_data(headers, pl_lookup, user_id=user_id)

    # Step 2: gather from each source separately so we can score per-source
    scored: dict[str, int] = {}

    def _score(tracks_list: list[dict], weight: int, cap: int | None = None) -> list[dict]:
        for t in tracks_list:
            tid = t.get("id")
            if not tid:
                continue
            current = scored.get(tid, 0)
            add = weight if cap is None else min(weight, cap - current)
            scored[tid] = current + max(add, 0)
        return tracks_list

    saved   = _fetch_saved_tracks(headers)
    _score(saved, 2)  # liked = stronger signal than a playlist entry

    _score(pl_trks, 1)

    top_s   = _fetch_top_tracks_range(headers, "short_term")
    _score(top_s, 3)

    top_m   = _fetch_top_tracks_range(headers, "medium_term")
    _score(top_m, 2)

    top_l   = _fetch_top_tracks_range(headers, "long_term")
    _score(top_l, 1)

    recent  = _fetch_recent_tracks(headers)  # may contain duplicates = more plays
    _score(recent, 1, cap=3)  # cap recent bonus at +3 per track

    # Step 3: deduplicate by track id (preserve first occurrence for track metadata)
    seen: dict[str, dict] = {}
    for t in saved + pl_trks + top_s + top_m + top_l + recent:
        tid = t.get("id")
        if tid and tid not in seen:
            seen[tid] = t

    # Step 4: assign play_score to all candidates, then take top MAX_TRACKS by score
    all_candidates = list(seen.values())
    for t in all_candidates:
        t["playlists"] = pl_lookup.get(t["id"], [])
        t["play_score"] = max(scored.get(t["id"], 1), 1)

    tracks = sorted(all_candidates, key=lambda t: t["play_score"], reverse=True)[:MAX_TRACKS]

    # Step 5: compute remaining = liked tracks not in any user-owned playlist
    liked_by_id: dict[str, dict] = {t["id"]: t for t in saved if t.get("id")}
    playlist_ids: set[str] = set(pl_lookup.keys())
    remaining: list[dict] = [
        t for tid, t in liked_by_id.items()
        if tid not in playlist_ids
    ]
    # Sort most-recently liked first
    remaining.sort(key=lambda t: t.get("liked_at") or "", reverse=True)
    # Strip heavy fields not needed for the remaining panel
    remaining_slim = [
        {k: t[k] for k in ("id", "name", "artist", "album_art", "liked_at", "release_year", "isrc") if k in t}
        for t in remaining
    ]

    tagged = sum(1 for t in tracks if t["playlists"])
    with_preview = sum(1 for t in tracks if t.get("preview_url"))
    max_score = max((t["play_score"] for t in tracks), default=1)
    min_score = min((t["play_score"] for t in tracks), default=1)
    print(f"[pipeline] {len(all_candidates)} candidates → top {len(tracks)} by play_score (range {min_score}–{max_score}), {tagged} in playlists, {with_preview} previews, {len(remaining_slim)} remaining (liked but not in any playlist)")
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
    pl_resp = _spotify_get("https://api.spotify.com/v1/me/playlists", headers, params={"limit": 50})
    if pl_resp.status_code != 200:
        print(f"[pipeline] /me/playlists failed: HTTP {pl_resp.status_code}")
        return [], []

    playlists = pl_resp.json().get("items", [])

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

        url = f"https://api.spotify.com/v1/playlists/{pl_id}/items"
        while url:
            resp = _spotify_get(url, headers, params={"limit": 50})
            if resp.status_code != 200:
                print(f"[pipeline] playlist '{pl_name}': HTTP {resp.status_code}")
                break
            data = resp.json()

            for item in data.get("items", []):
                t = (item or {}).get("item")
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
                if len(pl_track_samples[pl_name]) < 10:
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
# Client credentials token (for public endpoints like audio-features)
# ---------------------------------------------------------------------------

def _get_app_token() -> str:
    """Client credentials token — no user context needed, used for audio features."""
    import base64
    import os
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {creds}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Audio feature embeddings (primary path)
# ---------------------------------------------------------------------------

_AF_DIRECT = ["danceability", "energy", "speechiness", "acousticness",
               "instrumentalness", "liveness", "valence"]
_AF_NORM   = {"key": (0, 11), "loudness": (-60, 0), "tempo": (50, 220),
               "mode": (0, 1), "time_signature": (3, 7)}
_AF_KEYS   = _AF_DIRECT + list(_AF_NORM.keys())  # 12 features total


def _audio_feature_embeddings(
    tracks: list[dict],
    user_headers: dict,
    on_progress: Callable | None = None,
) -> tuple[np.ndarray, list[str]]:
    """
    Fetch audio features in batches of 100.
    Tries client-credentials token first, falls back to user token, then genre embeddings.
    """
    try:
        app_token = _get_app_token()
        af_headers = {"Authorization": f"Bearer {app_token}"}
    except Exception as e:
        print(f"[pipeline] could not get app token ({e}), using user token for audio features")
        af_headers = user_headers

    ids = [t["id"] for t in tracks]
    feat_map: dict[str, dict] = {}
    use_genre_fallback = False
    n_batches = max(1, (len(ids) + 99) // 100)

    for batch_i, i in enumerate(range(0, len(ids), 100)):
        batch = ids[i: i + 100]
        resp = requests.get(
            "https://api.spotify.com/v1/audio-features",
            params={"ids": ",".join(batch)},
            headers=af_headers,
            timeout=15,
        )
        if resp.status_code == 403 and af_headers is not user_headers:
            print("[pipeline] audio features 403 with app token — retrying with user token")
            af_headers = user_headers
            resp = requests.get(
                "https://api.spotify.com/v1/audio-features",
                params={"ids": ",".join(batch)},
                headers=af_headers,
                timeout=15,
            )
        if resp.status_code == 403:
            print("[pipeline] audio features 403 — falling back to genre embeddings")
            use_genre_fallback = True
            break
        if resp.status_code == 200:
            for af in resp.json().get("audio_features") or []:
                if af and af.get("id"):
                    feat_map[af["id"]] = af

        pct = 35 + int(((batch_i + 1) / n_batches) * 35)
        if on_progress:
            on_progress(pct, f"Audio features: batch {batch_i + 1}/{n_batches}…")
        time.sleep(0.05)

    if use_genre_fallback:
        if on_progress:
            on_progress(40, "Using genre embeddings instead…")
        return _genre_embeddings(tracks, user_headers, on_progress=on_progress)

    # Build normalised feature matrix
    n_cols = len(_AF_KEYS)
    matrix = np.zeros((len(tracks), n_cols), dtype=float)

    for ti, track in enumerate(tracks):
        af = feat_map.get(track["id"])
        if not af:
            continue
        for ci, key in enumerate(_AF_DIRECT):
            matrix[ti, ci] = float(af.get(key) or 0)
        for ci, (key, (lo, hi)) in enumerate(zip(_AF_NORM.keys(), _AF_NORM.values())):
            val = float(af.get(key) or lo)
            matrix[ti, len(_AF_DIRECT) + ci] = (val - lo) / (hi - lo)

    # Fill missing rows with column means
    col_means = matrix.mean(axis=0)
    for ti, track in enumerate(tracks):
        if track["id"] not in feat_map:
            matrix[ti] = col_means

    track_genres = ["other"] * len(tracks)
    print(f"[pipeline] audio features fetched for {len(feat_map)}/{len(tracks)} tracks")
    return matrix, track_genres


# ---------------------------------------------------------------------------
# Genre-based embeddings (fallback when audio features are unavailable)
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
# CLAP embeddings (optional)
# ---------------------------------------------------------------------------

def _clap_embeddings(
    tracks: list[dict],
    headers: dict,
    on_progress: Callable | None = None,
) -> np.ndarray:
    """
    Stream preview audio into CLAP model.
    Falls back to audio features for tracks without previews.
    """
    import io
    from concurrent.futures import ThreadPoolExecutor, as_completed

    clap_model = CLAP(version="2023", use_cuda=False)
    embeddings: dict[str, np.ndarray] = {}

    def _embed_one(track: dict) -> tuple[str, np.ndarray | None]:
        url = track.get("preview_url")
        if not url:
            return track["id"], None
        try:
            resp = requests.get(url, timeout=15)
            audio_bytes = io.BytesIO(resp.content)
            emb = clap_model.get_audio_embeddings([audio_bytes])
            return track["id"], np.array(emb[0])
        except Exception:
            return track["id"], None

    with ThreadPoolExecutor(max_workers=PREVIEW_CONCURRENCY) as pool:
        futures = {pool.submit(_embed_one, t): t for t in tracks}
        done = 0
        for fut in as_completed(futures):
            tid, emb = fut.result()
            embeddings[tid] = emb
            done += 1
            pct = 35 + int((done / len(tracks)) * 35)
            if on_progress:
                on_progress(pct, f"Embedded {done}/{len(tracks)} tracks with CLAP…")

    # Tracks without CLAP embedding → fallback to audio features
    missing = [t for t in tracks if embeddings.get(t["id"]) is None]
    if missing:
        feat_matrix = _audio_feature_embeddings(missing, headers)
        for i, t in enumerate(missing):
            embeddings[t["id"]] = feat_matrix[i]

    return np.array([embeddings[t["id"]] for t in tracks])


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
# LLM mood grouping
# ---------------------------------------------------------------------------

def _llm_mood_groups(
    playlist_meta: list[dict],
    pl_track_samples: dict[str, list[str]],
    api_key: str = "",
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
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

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

        print(f"[pipeline] sending {len(playlist_meta)} playlists to LLM for mood grouping")
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1200,
            system=system_msg,
            messages=[{"role": "user", "content": user_msg}],
        )

        import re as _re
        raw = resp.content[0].text.strip()
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
        })

    return {
        "points": points,
        "generated_at": int(time.time()),
        "track_count": len(points),
        "embedding_method": "clap" if CLAP_AVAILABLE else "genres",
        "display_name": display_name,
        "playlists": playlist_meta or [],
        "moods": sorted(set(p["mood"] for p in points)),
        "persona": persona,
        "remaining": remaining or [],
    }
