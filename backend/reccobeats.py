"""ReccoBeats audio feature extraction client."""

from __future__ import annotations

import os
import re
import time
from urllib.parse import urlparse

import requests

RECCOBEATS_AUDIO_FEATURES_URL = "https://api.reccobeats.com/v1/analysis/audio-features"
RECCOBEATS_AUDIO_FEATURES_LOOKUP_URL = "https://api.reccobeats.com/v1/audio-features"
MAX_AUDIO_BYTES = 5 * 1024 * 1024
ALLOWED_AUDIO_HOST_SUFFIXES = (
    "scdn.co",
    "spotifycdn.com",
    "audio-ssl.itunes.apple.com",
)
FEATURE_KEYS = (
    "acousticness",
    "danceability",
    "energy",
    "instrumentalness",
    "liveness",
    "loudness",
    "speechiness",
    "tempo",
    "valence",
)
SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 SoundMap/1.0",
}


def _normalise_features(data: dict) -> dict:
    """Return the numeric feature subset SoundMap currently cares about."""
    out = {}
    for key in FEATURE_KEYS:
        value = data.get(key, 0)
        out[key] = 0.0 if value is None else float(value)
    return out


def _spotify_id_from_href(href: str) -> str:
    """Extract a Spotify track ID from a Spotify track URL when present."""
    parsed = urlparse(href or "")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[-2] == "track" and SPOTIFY_ID_RE.match(parts[-1]):
        return parts[-1]
    return ""


def _validate_audio_url(url: str) -> None:
    """Reject non-HTTPS and unexpected hosts before server-side fetching."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError("preview_url must be an HTTPS URL")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_AUDIO_HOST_SUFFIXES):
        raise ValueError("preview_url host is not allowed")


def _download_preview_audio(url: str) -> tuple[bytes, str]:
    """Download a preview into memory, enforcing ReccoBeats' 5 MB limit."""
    _validate_audio_url(url)
    resp = requests.get(url, stream=True, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"Could not fetch preview audio: HTTP {resp.status_code}")

    content_type = resp.headers.get("content-type") or "audio/mpeg"
    chunks: list[bytes] = []
    size = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_AUDIO_BYTES:
            raise ValueError("Preview audio is larger than ReccoBeats' 5 MB limit")
        chunks.append(chunk)
    if not chunks:
        raise ValueError("Preview audio was empty")
    return b"".join(chunks), content_type


def extract_audio_features_from_url(preview_url: str) -> dict:
    """
    Extract ReccoBeats audio features from a Spotify/Apple preview URL.

    Audio is kept in memory only and sent as multipart field ``audioFile``.
    """
    audio_bytes, content_type = _download_preview_audio(preview_url)
    filename = "preview.mp3"
    if "ogg" in content_type:
        filename = "preview.ogg"
    elif "wav" in content_type:
        filename = "preview.wav"

    resp = requests.post(
        RECCOBEATS_AUDIO_FEATURES_URL,
        files={"audioFile": (filename, audio_bytes, content_type)},
        timeout=45,
    )
    if resp.status_code != 200:
        detail = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
        raise RuntimeError(f"ReccoBeats audio feature extraction failed: {detail}")
    return _normalise_features(resp.json())


def get_audio_features_for_ids(track_ids: list[str]) -> list[dict]:
    """
    Fetch ReccoBeats' stored audio features for Spotify/ReccoBeats track IDs.

    ReccoBeats exposes this as ``GET /v1/audio-features?ids=id1,id2``.
    Keep batches conservative; community usage indicates large requests can fail.
    """
    clean_ids = [tid.strip() for tid in track_ids if tid and tid.strip()]
    if not clean_ids:
        raise ValueError("At least one track id is required")

    out: list[dict] = []
    for start in range(0, len(clean_ids), 40):
        batch = clean_ids[start:start + 40]
        resp = requests.get(
            RECCOBEATS_AUDIO_FEATURES_LOOKUP_URL,
            params={"ids": ",".join(batch)},
            headers=DEFAULT_HEADERS,
            timeout=30,
        )
        if resp.status_code == 404:
            continue
        if resp.status_code != 200:
            detail = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
            raise RuntimeError(f"ReccoBeats audio features lookup failed: {detail}")

        payload = resp.json()
        # The endpoint answers either {"content": [...]} or a bare list. The
        # old spelling called .get() before checking, so a list response raised
        # AttributeError instead of being handled.
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("content") or []
            if not isinstance(rows, list):
                rows = [rows]
        else:
            rows = []
        for row in rows:
            if isinstance(row, dict):
                normalised = _normalise_features(row)
                spotify_id = _spotify_id_from_href(str(row.get("href") or ""))
                if spotify_id:
                    normalised["spotify_id"] = spotify_id
                for key in ("id", "href", "isrc", "key", "mode"):
                    if key in row:
                        normalised[key] = row[key]
                out.append(normalised)
    return out


def _spotify_search_token(explicit_token: str | None = None) -> str:
    """Return a Spotify token for metadata search, preferring the user's token."""
    if explicit_token:
        return explicit_token
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return ""
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=15,
    )
    if resp.status_code != 200:
        return ""
    return resp.json().get("access_token", "")


def _resolve_spotify_id(track: dict, token: str) -> str:
    """Resolve Spotify ID directly, then by ISRC, then by title/artist search."""
    tid = str(track.get("spotify_id") or track.get("id") or "").strip()
    if SPOTIFY_ID_RE.match(tid):
        return tid
    if not token:
        return ""

    headers = {"Authorization": f"Bearer {token}"}
    isrc = str(track.get("isrc") or "").strip()
    queries = []
    if isrc:
        queries.append(f"isrc:{isrc}")
    name = str(track.get("name") or "").strip()
    artist = str(track.get("artist") or "").strip()
    if name and artist:
        queries.append(f'track:"{name}" artist:"{artist}"')
    elif name:
        queries.append(name)

    for query in queries:
        resp = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": query, "type": "track", "limit": 1},
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            continue
        items = ((resp.json().get("tracks") or {}).get("items") or [])
        if items and items[0].get("id"):
            return items[0]["id"]
        time.sleep(0.04)
    return ""


def get_audio_features_for_tracks(
    tracks: list[dict],
    spotify_token: str | None = None,
) -> dict[str, dict]:
    """
    Resolve a mixed Spotify/Apple/library track list to Spotify IDs, then fetch ReccoBeats features.

    Returns a map keyed by the original SoundMap track id. Each value includes:
    ``spotify_id``, ``lookup_method``, and ``features``.
    """
    if not tracks:
        return {}

    source_to_spotify: dict[str, str] = {}
    needs_resolve: list[dict] = []

    # Pass 1 — no network: direct Spotify IDs, then the ISRC cache.
    # Apple/YouTube libraries carry ISRCs, so on a rebuild almost every
    # track resolves from the cache instead of a per-track Spotify search.
    isrc_cache: dict[str, dict] = {}
    try:
        from . import storage
        isrcs = [str(t.get("isrc") or "").strip() for t in tracks
                 if t.get("isrc") and not SPOTIFY_ID_RE.match(str(t.get("id") or "").strip())]
        if isrcs:
            isrc_cache = storage.load_track_audio_features_by_isrc(isrcs)
    except Exception as exc:
        print(f"[reccobeats] ISRC cache unavailable ({exc})")

    for track in tracks:
        source_id = str(track.get("id") or "").strip()
        if not source_id:
            continue
        tid = str(track.get("spotify_id") or track.get("id") or "").strip()
        if SPOTIFY_ID_RE.match(tid):
            source_to_spotify[source_id] = tid
            continue
        cached_row = isrc_cache.get(str(track.get("isrc") or "").strip().upper())
        cached_sid = str((cached_row or {}).get("spotify_id") or "").strip()
        if cached_sid:
            source_to_spotify[source_id] = cached_sid
            continue
        needs_resolve.append(track)

    # Pass 2 — resolve the rest against Spotify search, in parallel.
    if needs_resolve:
        token = _spotify_search_token(spotify_token)
        if token:
            print(f"[reccobeats] resolving {len(needs_resolve)}/{len(tracks)} tracks via Spotify search")
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=6) as pool:
                resolved = pool.map(lambda t: _resolve_spotify_id(t, token), needs_resolve)
                for track, spotify_id in zip(needs_resolve, resolved, strict=True):
                    if spotify_id:
                        source_to_spotify[str(track.get("id") or "").strip()] = spotify_id

    spotify_ids = list(dict.fromkeys(source_to_spotify.values()))
    if not spotify_ids:
        return {}

    by_spotify_id: dict[str, dict] = {}
    try:
        from . import storage
        cached = storage.load_track_audio_features(spotify_ids)
    except Exception:
        cached = {}

    for sid, row in cached.items():
        by_spotify_id[sid] = {
            "spotify_id": sid,
            "id": row.get("reccobeats_id"),
            "href": row.get("href"),
            "isrc": row.get("isrc"),
            "key": row.get("musical_key"),
            "mode": row.get("musical_mode"),
            **{key: row.get(key) for key in FEATURE_KEYS if row.get(key) is not None},
        }

    missing_ids = [sid for sid in spotify_ids if sid not in by_spotify_id]
    if missing_ids:
        rows = get_audio_features_for_ids(missing_ids)
        for row in rows:
            sid = str(row.get("spotify_id") or "").strip()
            if sid:
                by_spotify_id[sid] = row

    out: dict[str, dict] = {}
    for source_id, spotify_id in source_to_spotify.items():
        features = by_spotify_id.get(spotify_id)
        if not features:
            continue
        out[source_id] = {
            "spotify_id": spotify_id,
            "reccobeats_id": features.get("id"),
            "lookup_method": "spotify_id" if source_id == spotify_id else "spotify_search",
            "features": {key: features[key] for key in FEATURE_KEYS if key in features},
            **{key: features[key] for key in ("href", "isrc", "key", "mode") if key in features},
        }
    return out
