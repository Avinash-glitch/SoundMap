"""Map data persistence — Supabase with file-based fallback."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

STORAGE_DIR = Path("./user_data")
AUDIO_FEATURE_CACHE = STORAGE_DIR / "track_audio_features.json"
YTMUSIC_AUTH_DIR = STORAGE_DIR / "ytmusic_auth"

_supabase = None


def _get_supabase():
    global _supabase
    if _supabase is not None:
        return _supabase
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _supabase = create_client(url, key)
        print("[storage] Supabase connected")
        return _supabase
    except Exception as e:
        print(f"[storage] Supabase init failed: {e}")
        return None


def youtube_auth_path(user_id: str) -> Path | None:
    """Return the server-side YouTube Music auth path if it exists."""
    safe_id = str(user_id or "").replace("/", "_").replace("\\", "_").strip()
    if not safe_id:
        return None
    path = YTMUSIC_AUTH_DIR / f"{safe_id}.json"
    return path if path.exists() else None


def save_youtube_auth(user_id: str, auth_data: dict) -> Path:
    """Store YouTube Music auth JSON outside Git-backed app files."""
    safe_id = str(user_id or "").replace("/", "_").replace("\\", "_").strip()
    if not safe_id:
        raise ValueError("user_id required")
    YTMUSIC_AUTH_DIR.mkdir(parents=True, exist_ok=True)
    path = YTMUSIC_AUTH_DIR / f"{safe_id}.json"
    path.write_text(json.dumps(auth_data, indent=2), encoding="utf-8")
    return path


def load_youtube_auth(user_id: str) -> dict | None:
    """Load stored YouTube Music auth JSON for a user."""
    path = youtube_auth_path(user_id)
    if not path:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete_youtube_auth(user_id: str) -> bool:
    """Delete stored YouTube Music auth JSON for a user."""
    path = youtube_auth_path(user_id)
    if not path:
        return False
    path.unlink()
    return True


def save_map(user_id: str, map_data: dict) -> None:
    """Persist map data. Uses Supabase if configured, else file system."""
    sb = _get_supabase()
    if sb:
        try:
            sb.table("user_maps").upsert({
                "user_id": user_id,
                "map_data": map_data,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            return
        except Exception as e:
            print(f"[storage] Supabase save failed, falling back to file: {e}")
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    (STORAGE_DIR / f"{user_id}.json").write_text(json.dumps(map_data), encoding="utf-8")


def delete_map(user_id: str) -> bool:
    """Delete stored map data for a user. Returns True if a stored row/file was removed."""
    deleted = False
    sb = _get_supabase()
    if sb:
        try:
            result = sb.table("user_maps").delete().eq("user_id", user_id).execute()
            deleted = bool(getattr(result, "data", None))
        except Exception as e:
            print(f"[storage] Supabase delete failed, falling back to file: {e}")
    p = STORAGE_DIR / f"{user_id}.json"
    if p.exists():
        p.unlink()
        deleted = True
    return deleted


def save_track_audio_features(rows: list[dict]) -> None:
    """
    Persist per-track audio features separately from user map data.

    Supabase table expected:
      track_audio_features(spotify_id text primary key, reccobeats_id text,
      href text, isrc text, acousticness numeric, danceability numeric,
      energy numeric, instrumentalness numeric, liveness numeric,
      loudness numeric, speechiness numeric, tempo numeric, valence numeric,
      musical_key int, musical_mode int, track_name text, artist text,
      album text, playlists jsonb, playlist_names text[], raw jsonb,
      updated_at timestamptz)
    """
    clean_rows = []
    for row in rows:
        spotify_id = str(row.get("spotify_id") or "").strip()
        if not spotify_id:
            continue
        clean_row = {
            "spotify_id": spotify_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        for key in ("reccobeats_id", "href", "isrc", "artist", "album", "musical_key", "musical_mode"):
            if row.get(key) is not None:
                clean_row[key] = row.get(key)

        track_name = row.get("track_name") or row.get("name")
        if track_name is not None:
            clean_row["track_name"] = track_name

        playlists = row.get("playlists")
        playlist_names = row.get("playlist_names") or playlists
        if playlists:
            clean_row["playlists"] = playlists
        if playlist_names:
            clean_row["playlist_names"] = playlist_names

        for key in (
            "acousticness",
            "danceability",
            "energy",
            "instrumentalness",
            "liveness",
            "loudness",
            "speechiness",
            "tempo",
            "valence",
        ):
            if row.get(key) is not None:
                clean_row[key] = row.get(key)

        raw = row.get("raw")
        if raw:
            clean_row["raw"] = raw

        clean_rows.append(clean_row)
    if not clean_rows:
        print("[storage] No valid audio feature rows to save")
        return

    sb = _get_supabase()
    if sb:
        try:
            sb.table("track_audio_features").upsert(clean_rows, on_conflict="spotify_id").execute()
            print(f"[storage] Saved {len(clean_rows)} audio feature rows to Supabase")
            return
        except Exception as e:
            print(f"[storage] Supabase audio feature save failed, falling back to file: {e}")

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    existing = {}
    if AUDIO_FEATURE_CACHE.exists():
        try:
            existing = json.loads(AUDIO_FEATURE_CACHE.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    for row in clean_rows:
        existing[row["spotify_id"]] = row
    AUDIO_FEATURE_CACHE.write_text(json.dumps(existing), encoding="utf-8")
    print(f"[storage] Saved {len(clean_rows)} audio feature rows to local cache")


def load_track_audio_features(spotify_ids: list[str]) -> dict[str, dict]:
    """Return cached audio features keyed by Spotify track ID."""
    clean_ids = list(dict.fromkeys(str(sid).strip() for sid in spotify_ids if str(sid or "").strip()))
    if not clean_ids:
        return {}

    sb = _get_supabase()
    if sb:
        try:
            rows: dict[str, dict] = {}
            for start in range(0, len(clean_ids), 200):
                batch = clean_ids[start:start + 200]
                result = sb.table("track_audio_features").select("*").in_("spotify_id", batch).execute()
                rows.update({row["spotify_id"]: row for row in (result.data or []) if row.get("spotify_id")})
            return rows
        except Exception as e:
            print(f"[storage] Supabase audio feature load failed, falling back to file: {e}")

    if not AUDIO_FEATURE_CACHE.exists():
        return {}
    try:
        existing = json.loads(AUDIO_FEATURE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {sid: existing[sid] for sid in clean_ids if sid in existing}


def load_track_audio_features_by_isrc(isrcs: list[str]) -> dict[str, dict]:
    """Return cached audio features keyed by ISRC.

    Lets non-Spotify libraries (Apple Music, YouTube Music) skip the
    per-track Spotify search on rebuilds: a track whose ISRC is already
    cached resolves to its spotify_id with zero network requests.
    """
    clean = list(dict.fromkeys(str(i).strip().upper() for i in isrcs if str(i or "").strip()))
    if not clean:
        return {}

    sb = _get_supabase()
    if sb:
        try:
            rows: dict[str, dict] = {}
            for start in range(0, len(clean), 200):
                batch = clean[start:start + 200]
                result = sb.table("track_audio_features").select("*").in_("isrc", batch).execute()
                for row in (result.data or []):
                    isrc = str(row.get("isrc") or "").strip().upper()
                    if isrc and isrc not in rows:
                        rows[isrc] = row
            return rows
        except Exception as e:
            print(f"[storage] Supabase ISRC feature load failed, falling back to file: {e}")

    if not AUDIO_FEATURE_CACHE.exists():
        return {}
    try:
        existing = json.loads(AUDIO_FEATURE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    wanted = set(clean)
    out: dict[str, dict] = {}
    for row in existing.values():
        isrc = str((row or {}).get("isrc") or "").strip().upper()
        if isrc in wanted and isrc not in out:
            out[isrc] = row
    return out


def load_map(user_id: str) -> dict | None:
    """Return map data or None if not found."""
    sb = _get_supabase()
    if sb:
        try:
            result = sb.table("user_maps").select("map_data").eq("user_id", user_id).execute()
            if result.data:
                return result.data[0]["map_data"]
            return None
        except Exception as e:
            print(f"[storage] Supabase load failed, falling back to file: {e}")
    p = STORAGE_DIR / f"{user_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def map_exists(user_id: str) -> bool:
    """Check if a map exists for a user."""
    sb = _get_supabase()
    if sb:
        try:
            result = sb.table("user_maps").select("user_id").eq("user_id", user_id).execute()
            return bool(result.data)
        except Exception as e:
            print(f"[storage] Supabase exists check failed: {e}")
    return (STORAGE_DIR / f"{user_id}.json").exists()


def map_age_hours(user_id: str) -> float:
    """Return age of stored map in hours, or infinity if missing."""
    sb = _get_supabase()
    if sb:
        try:
            result = sb.table("user_maps").select("updated_at").eq("user_id", user_id).execute()
            if result.data:
                updated_at = result.data[0]["updated_at"]
                dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            return float("inf")
        except Exception as e:
            print(f"[storage] Supabase age check failed: {e}")
    p = STORAGE_DIR / f"{user_id}.json"
    if not p.exists():
        return float("inf")
    return (time.time() - p.stat().st_mtime) / 3600
