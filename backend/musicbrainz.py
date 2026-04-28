"""
MusicBrainz genre tag lookup with persistent disk cache.

Two-step per uncached track:
  1. Find MBID via ISRC (precise) or name+artist search
  2. Fetch recording tags via MBID lookup (?inc=tags)

Rate: 1.1 s between requests (MusicBrainz guideline).
Cache: user_data/_mb_cache.json — keyed by Spotify track ID.
       Shared across all users since tags are global.
"""

import json
import time
from pathlib import Path

import requests

MB_API = "https://musicbrainz.org/ws/2"
MB_HEADERS = {
    "User-Agent": "SoundMap/1.0 (https://github.com/Avinash-glitch/SoundMap)",
    "Accept": "application/json",
}
CACHE_PATH = Path("./user_data/_mb_cache.json")
RATE_DELAY = 1.1  # seconds between any two outbound requests

_cache: dict[str, list[str]] | None = None


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load() -> dict[str, list[str]]:
    global _cache
    if _cache is not None:
        return _cache
    if CACHE_PATH.exists():
        try:
            _cache = json.loads(CACHE_PATH.read_text())
            return _cache
        except Exception:
            pass
    _cache = {}
    return _cache


def _save() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_tags(track_id: str, name: str, artist: str, isrc: str | None = None) -> list[str]:
    """
    Return MusicBrainz genre tags for a track, using local cache.
    On a cache miss, makes 2–3 real HTTP calls and sleeps 1.1 s between each.
    Returns [] on any failure — never raises.
    """
    cache = _load()
    if track_id in cache:
        return cache[track_id]

    tags = _fetch(name, artist, isrc)
    cache[track_id] = tags
    _save()
    return tags


def get_tags_batch(
    tracks: list[dict],
    on_progress: callable | None = None,
) -> dict[str, list[str]]:
    """
    Fetch MusicBrainz tags for a list of track dicts.
    Each dict needs: id, name, artist, and optionally isrc.
    Returns {track_id: [tag, ...]} for every track in the input.
    Progress callback: on_progress(done, total).
    """
    result: dict[str, list[str]] = {}
    cache = _load()

    # Separate cached from uncached
    cached = {t["id"]: cache[t["id"]] for t in tracks if t["id"] in cache}
    uncached = [t for t in tracks if t["id"] not in cache]

    result.update(cached)
    print(f"[musicbrainz] {len(cached)} cache hits, {len(uncached)} to fetch")

    for i, t in enumerate(uncached):
        tags = _fetch(t["name"], t["artist"], t.get("isrc"))
        cache[t["id"]] = tags
        result[t["id"]] = tags
        if on_progress:
            on_progress(len(cached) + i + 1, len(tracks))
        if i < len(uncached) - 1:
            time.sleep(RATE_DELAY)  # only sleep between calls, not after last one

    if uncached:
        _save()

    return result


# ---------------------------------------------------------------------------
# Internal fetch logic
# ---------------------------------------------------------------------------

def _fetch(name: str, artist: str, isrc: str | None) -> list[str]:
    """Two-step: find MBID, then fetch tags. Returns [] on failure."""
    mbid = _find_mbid(name, artist, isrc)
    if not mbid:
        return []
    time.sleep(RATE_DELAY)
    return _fetch_tags(mbid)


def _find_mbid(name: str, artist: str, isrc: str | None) -> str | None:
    """Search MusicBrainz for a recording, return its MBID or None."""
    # ISRC lookup is most precise — try first
    if isrc:
        mbid = _search(f"isrc:{isrc}", limit=1)
        if mbid:
            return mbid
        time.sleep(RATE_DELAY)

    # Name + artist search
    safe = lambda s: s.replace('"', "").replace("\\", "")[:100]
    return _search(f'recording:"{safe(name)}" AND artist:"{safe(artist)}"', limit=1)


def _search(query: str, limit: int = 1) -> str | None:
    """Run a MB recording search and return the top MBID, or None."""
    try:
        resp = requests.get(
            f"{MB_API}/recording",
            params={"query": query, "fmt": "json", "limit": limit},
            headers=MB_HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            recs = resp.json().get("recordings", [])
            if recs:
                return recs[0].get("id")
    except Exception as exc:
        print(f"[musicbrainz] search error ({query[:60]}): {exc}")
    return None


def _fetch_tags(mbid: str) -> list[str]:
    """Fetch tags for a known recording MBID."""
    try:
        resp = requests.get(
            f"{MB_API}/recording/{mbid}",
            params={"fmt": "json", "inc": "tags"},
            headers=MB_HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            tags = resp.json().get("tags", [])
            # Sort by community vote count, return top 15 tag names
            return [t["name"] for t in sorted(tags, key=lambda x: x.get("count", 0), reverse=True)[:15]]
    except Exception as exc:
        print(f"[musicbrainz] tag fetch error ({mbid}): {exc}")
    return []
