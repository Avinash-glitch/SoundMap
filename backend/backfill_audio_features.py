"""Backfill ReccoBeats audio features for tracks already stored in user_maps.

Run from the repo root:

    python -m backend.backfill_audio_features --batch-size 40

Required environment:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY or SUPABASE_ANON_KEY

For Apple/non-Spotify track resolution:
    SPOTIFY_CLIENT_ID
    SPOTIFY_CLIENT_SECRET
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv may not be installed in bare local Python
    load_dotenv = None

from . import storage
from .reccobeats import FEATURE_KEYS, SPOTIFY_ID_RE, get_audio_features_for_tracks


def _load_env() -> None:
    if load_dotenv:
        load_dotenv()


def _iter_supabase_maps(user_id: str | None = None, page_size: int = 500) -> list[dict]:
    """Load map rows from Supabase user_maps."""
    sb = storage._get_supabase()
    if not sb:
        return []

    rows: list[dict] = []
    start = 0
    while True:
        query = sb.table("user_maps").select("user_id,map_data").range(start, start + page_size - 1)
        if user_id:
            query = query.eq("user_id", user_id)
        result = query.execute()
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


def _iter_local_maps(user_id: str | None = None) -> list[dict]:
    """Load map rows from local user_data JSON files as a fallback."""
    rows = []
    for path in sorted(storage.STORAGE_DIR.glob("*.json")):
        if path.name == storage.AUDIO_FEATURE_CACHE.name:
            continue
        uid = path.stem
        if user_id and uid != user_id:
            continue
        try:
            rows.append({"user_id": uid, "map_data": json.loads(path.read_text(encoding="utf-8"))})
        except Exception as exc:
            print(f"[backfill] Skipping {path}: {exc}")
    return rows


def _track_key(track: dict) -> str:
    """Prefer platform-neutral IDs for dedupe."""
    for key in ("spotify_id", "audio_feature_id", "isrc", "id"):
        value = str(track.get(key) or "").strip()
        if value:
            return f"{key}:{value.lower()}"
    name = str(track.get("name") or "").strip().lower()
    artist = str(track.get("artist") or "").strip().lower()
    return f"text:{name}|{artist}"


def _normalise_track(point: dict) -> dict:
    """Keep only fields needed for Spotify/ReccoBeats resolution."""
    tid = str(point.get("id") or "").strip()
    spotify_id = str(point.get("spotify_id") or point.get("audio_feature_id") or "").strip()
    if not spotify_id and SPOTIFY_ID_RE.match(tid):
        spotify_id = tid
    return {
        "id": tid,
        "spotify_id": spotify_id,
        "isrc": point.get("isrc"),
        "name": point.get("name"),
        "artist": point.get("artist"),
        "album": point.get("album"),
    }


def _clean_csv_value(value: str | None) -> str:
    """Normalize Supabase CSV null-ish values."""
    value = str(value or "").strip()
    return "" if value.lower() in {"null", "none", "nan"} else value


def _load_tracks_from_csv(path: str) -> list[dict]:
    """Load missing track rows exported from Supabase."""
    tracks: dict[str, dict] = {}
    with Path(path).expanduser().open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            track = {
                "id": _clean_csv_value(row.get("track_id")) or _clean_csv_value(row.get("id")),
                "spotify_id": _clean_csv_value(row.get("spotify_id")) or _clean_csv_value(row.get("audio_feature_id")),
                "isrc": _clean_csv_value(row.get("isrc")),
                "name": _clean_csv_value(row.get("name")),
                "artist": _clean_csv_value(row.get("artist")),
                "album": _clean_csv_value(row.get("album")),
            }
            if not track["spotify_id"] and SPOTIFY_ID_RE.match(track["id"]):
                track["spotify_id"] = track["id"]
            if not track["id"] and not track["name"]:
                continue
            tracks.setdefault(_track_key(track), track)
    return list(tracks.values())


def _collect_unique_tracks(map_rows: list[dict]) -> list[dict]:
    tracks: dict[str, dict] = {}
    for row in map_rows:
        data = row.get("map_data") or {}
        points = data.get("points") or []
        if not isinstance(points, list):
            continue
        for point in points:
            if not isinstance(point, dict):
                continue
            track = _normalise_track(point)
            if not track.get("id") and not track.get("name"):
                continue
            tracks.setdefault(_track_key(track), track)
    return list(tracks.values())


def _feature_rows(feature_map: dict[str, dict]) -> list[dict]:
    rows = []
    for enriched in feature_map.values():
        spotify_id = enriched.get("spotify_id")
        features = enriched.get("features") or {}
        if not spotify_id or not features:
            continue
        rows.append({
            "spotify_id": spotify_id,
            "reccobeats_id": enriched.get("reccobeats_id"),
            "href": enriched.get("href"),
            "isrc": enriched.get("isrc"),
            "musical_key": enriched.get("key"),
            "musical_mode": enriched.get("mode"),
            "raw": enriched,
            **{key: features.get(key) for key in FEATURE_KEYS},
        })
    return rows


def _chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def backfill_audio_features(
    *,
    user_id: str | None = None,
    csv_path: str | None = None,
    batch_size: int = 40,
    limit: int | None = None,
    dry_run: bool = False,
    pause_seconds: float = 0.2,
) -> None:
    """Backfill audio features from existing maps."""
    _load_env()

    map_rows = []
    if csv_path:
        tracks = _load_tracks_from_csv(csv_path)
        source = f"CSV: {csv_path}"
    else:
        map_rows = _iter_supabase_maps(user_id=user_id)
        source = "Supabase"
        if not map_rows:
            map_rows = _iter_local_maps(user_id=user_id)
            source = "local user_data"
        tracks = _collect_unique_tracks(map_rows)
    if limit:
        tracks = tracks[:limit]

    spotify_candidates = [
        t["spotify_id"] for t in tracks
        if t.get("spotify_id") and SPOTIFY_ID_RE.match(str(t["spotify_id"]))
    ]
    cached = storage.load_track_audio_features(spotify_candidates)
    tracks_to_process = [
        t for t in tracks
        if not (t.get("spotify_id") and t["spotify_id"] in cached)
    ]

    print(f"[backfill] Source: {source}")
    if not csv_path:
        print(f"[backfill] Maps: {len(map_rows)}")
    print(f"[backfill] Unique tracks: {len(tracks)}")
    print(f"[backfill] Already in track_audio_features by Spotify ID: {len(cached)}")
    print(f"[backfill] To process: {len(tracks_to_process)}")

    if dry_run:
        sample = tracks_to_process[:5]
        print("[backfill] Dry run only. Sample tracks:")
        for track in sample:
            print(f"  - {track.get('name')} — {track.get('artist')} [{track.get('spotify_id') or track.get('isrc') or track.get('id')}]")
        return

    resolved = 0
    saved = 0
    failed_batches = 0
    for index, batch in enumerate(_chunks(tracks_to_process, batch_size), start=1):
        print(f"[backfill] Batch {index}: resolving {len(batch)} tracks")
        try:
            feature_map = get_audio_features_for_tracks(batch)
            rows = _feature_rows(feature_map)
            storage.save_track_audio_features(rows)
            resolved += len(feature_map)
            saved += len(rows)
            print(f"[backfill] Batch {index}: resolved {len(feature_map)}, saved {len(rows)}")
        except Exception as exc:
            failed_batches += 1
            print(f"[backfill] Batch {index} failed: {exc}")
        if pause_seconds:
            time.sleep(pause_seconds)

    print("[backfill] Complete")
    print(f"[backfill] Resolved tracks: {resolved}")
    print(f"[backfill] Saved feature rows: {saved}")
    print(f"[backfill] Failed batches: {failed_batches}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill track_audio_features from existing user_maps")
    parser.add_argument("--user-id", help="Only process one user_id")
    parser.add_argument("--csv", dest="csv_path", help="Process a Supabase CSV export instead of reading user_maps")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--limit", type=int, help="Process at most N unique tracks")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pause-seconds", type=float, default=0.2)
    args = parser.parse_args()

    backfill_audio_features(
        user_id=args.user_id,
        csv_path=args.csv_path,
        batch_size=max(args.batch_size, 1),
        limit=args.limit,
        dry_run=args.dry_run,
        pause_seconds=max(args.pause_seconds, 0),
    )


if __name__ == "__main__":
    main()
