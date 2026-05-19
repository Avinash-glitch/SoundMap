"""YouTube Music integration using ytmusicapi.

This is an MVP integration. ytmusicapi is unofficial, so every call is wrapped
and the rest of SoundMap should treat this provider as beta.
"""

from __future__ import annotations

import os
import time
from difflib import SequenceMatcher
from pathlib import Path

from . import storage


def _ytmusic_class():
    """Import YTMusic lazily so the app can boot before the dependency is installed."""
    from ytmusicapi import YTMusic
    return YTMusic


def _oauth_credentials():
    """Return ytmusicapi OAuth credentials when configured.

    ytmusicapi requires a Google OAuth client ID/secret for authenticated
    YouTube Music library and playlist writes. Search can still use an
    unauthenticated client.
    """
    client_id = os.environ.get("YTMUSIC_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YTMUSIC_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None

    from ytmusicapi import OAuthCredentials
    return OAuthCredentials(client_id=client_id, client_secret=client_secret)


def get_ytmusic_client(user_id: str | None = None, authenticated: bool = True):
    """Return a YTMusic client, using per-user auth when required."""
    YTMusic = _ytmusic_class()
    if not authenticated:
        return YTMusic()

    auth_path = storage.youtube_auth_path(user_id or "")
    if not auth_path:
        global_auth = os.environ.get("YTMUSIC_AUTH_FILE", "").strip()
        auth_path = Path(global_auth) if global_auth else None
    if not auth_path or not auth_path.exists():
        raise FileNotFoundError("YouTube Music auth is not configured for this user")

    credentials = _oauth_credentials()
    if not credentials:
        raise FileNotFoundError(
            "YouTube Music OAuth credentials are not configured. "
            "Set YTMUSIC_CLIENT_ID and YTMUSIC_CLIENT_SECRET."
        )
    return YTMusic(str(auth_path), oauth_credentials=credentials)


def youtube_auth_configured(user_id: str) -> bool:
    """Return True when YouTube Music auth exists for this user or globally."""
    global_auth = os.environ.get("YTMUSIC_AUTH_FILE", "").strip()
    has_auth_file = bool(storage.youtube_auth_path(user_id) or (global_auth and Path(global_auth).exists()))
    return has_auth_file and bool(_oauth_credentials())


def _duration_to_ms(value) -> int:
    """Parse ytmusicapi duration strings like 3:42 or 1:02:33 into ms."""
    if isinstance(value, (int, float)):
        return int(value * 1000)
    if not isinstance(value, str) or not value.strip():
        return 0
    try:
        parts = [int(p) for p in value.split(":")]
    except ValueError:
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds * 1000


def _first_artist(track: dict) -> str:
    artists = track.get("artists")
    if isinstance(artists, list) and artists:
        first = artists[0]
        return first.get("name", "") if isinstance(first, dict) else str(first)
    if isinstance(artists, dict):
        return artists.get("name", "")
    return track.get("artist") or track.get("author") or ""


def _artist_names(track: dict) -> list[str]:
    artists = track.get("artists")
    if isinstance(artists, list):
        names = [a.get("name", "") if isinstance(a, dict) else str(a) for a in artists]
        return [n for n in names if n]
    artist = _first_artist(track)
    return [artist] if artist else []


def _album_name(track: dict) -> str:
    album = track.get("album")
    if isinstance(album, dict):
        return album.get("name", "") or album.get("title", "")
    if isinstance(album, str):
        return album
    return ""


def _thumbnail_url(track: dict) -> str:
    thumbs = track.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        return thumbs[-1].get("url", "") if isinstance(thumbs[-1], dict) else ""
    return ""


def normalise_youtube_track(track: dict, playlist_name: str | None = None) -> dict:
    """Convert a ytmusicapi result into SoundMap's track shape."""
    video_id = track.get("videoId") or track.get("video_id") or track.get("id") or ""
    title = track.get("title") or track.get("name") or "Unknown Title"
    artist = _first_artist(track)
    url = f"https://music.youtube.com/watch?v={video_id}" if video_id else ""
    return {
        "id": video_id,
        "youtube_music_id": video_id,
        "name": title,
        "artist": artist,
        "artists": _artist_names(track),
        "artist_ids": [],
        "album": _album_name(track),
        "album_art": _thumbnail_url(track),
        "preview_url": None,
        "external_url": url,
        "youtube_music_url": url,
        "duration_ms": _duration_to_ms(track.get("duration") or track.get("duration_seconds")),
        "popularity": 50,
        "release_year": None,
        "isrc": None,
        "playlists": [playlist_name] if playlist_name else [],
        "genre_tags": [],
        "play_score": 1,
        "liked_at": "",
        "is_top_track": False,
        "current_taste_score": 0,
        "source": "youtube_music",
    }


def fetch_youtube_music_library(user_id: str, on_progress=None) -> tuple[list[dict], list[dict]]:
    """Fetch YouTube Music library songs and playlist membership."""
    yt = get_ytmusic_client(user_id, authenticated=True)

    def progress(message: str) -> None:
        if on_progress:
            on_progress(message)

    tracks_by_id: dict[str, dict] = {}
    playlists: list[dict] = []

    progress("Fetching YouTube Music library songs…")
    try:
        library_songs = yt.get_library_songs(limit=5000) or []
    except Exception as exc:
        print(f"[youtube] library songs fetch failed: {exc}")
        library_songs = []

    for rank, raw in enumerate(library_songs):
        track = normalise_youtube_track(raw)
        if not track["id"]:
            continue
        track["play_score"] = max(1, 100 - min(rank, 99))
        track["current_taste_score"] = track["play_score"]
        track["is_top_track"] = rank < 50
        tracks_by_id.setdefault(track["id"], track)

    progress("Fetching YouTube Music playlists…")
    try:
        raw_playlists = yt.get_library_playlists(limit=5000) or []
    except Exception as exc:
        print(f"[youtube] playlists fetch failed: {exc}")
        raw_playlists = []

    for raw_playlist in raw_playlists:
        playlist_id = raw_playlist.get("playlistId") or raw_playlist.get("browseId") or raw_playlist.get("id")
        playlist_name = raw_playlist.get("title") or raw_playlist.get("name") or "Untitled"
        if not playlist_id:
            continue
        playlist_tracks: list[dict] = []
        try:
            full_playlist = yt.get_playlist(playlist_id, limit=5000) or {}
            raw_tracks = full_playlist.get("tracks") or []
        except Exception as exc:
            print(f"[youtube] playlist {playlist_name} fetch failed: {exc}")
            raw_tracks = []

        for raw_track in raw_tracks:
            track = normalise_youtube_track(raw_track, playlist_name=playlist_name)
            if not track["id"]:
                continue
            if track["id"] in tracks_by_id:
                if playlist_name not in tracks_by_id[track["id"]].setdefault("playlists", []):
                    tracks_by_id[track["id"]]["playlists"].append(playlist_name)
            else:
                tracks_by_id[track["id"]] = track
            playlist_tracks.append(track)

        playlists.append({
            "id": playlist_id,
            "name": playlist_name,
            "track_count": len(playlist_tracks),
            "external_url": f"https://music.youtube.com/playlist?list={playlist_id}",
        })

    tracks = list(tracks_by_id.values())
    print(f"[youtube] library: {len(tracks)} tracks across {len(playlists)} playlists")
    return tracks, playlists


def search_youtube_music_track(name: str, artist: str, album: str = "") -> dict | None:
    """Search YouTube Music and return the best song-like match."""
    yt = get_ytmusic_client(None, authenticated=False)
    query = " ".join(part for part in (name, artist, album) if part).strip()
    if not query:
        return None
    results = yt.search(query, filter="songs", limit=10) or []
    if not results:
        results = yt.search(query, limit=10) or []

    target = f"{name} {artist}".lower().strip()
    best = None
    best_score = 0.0
    for item in results:
        candidate = normalise_youtube_track(item)
        text = f"{candidate.get('name', '')} {candidate.get('artist', '')}".lower().strip()
        score = SequenceMatcher(None, target, text).ratio()
        if score > best_score:
            best = candidate
            best_score = score
    if best and best_score >= 0.68:
        best["match_score"] = round(best_score, 3)
        return best
    return None


def create_youtube_music_playlist(user_id: str, name: str, description: str = "") -> str:
    """Create a YouTube Music playlist and return its playlist id."""
    yt = get_ytmusic_client(user_id, authenticated=True)
    return yt.create_playlist(name, description)


def add_youtube_music_playlist_items(user_id: str, playlist_id: str, video_ids: list[str]) -> dict:
    """Add video IDs to a YouTube Music playlist."""
    yt = get_ytmusic_client(user_id, authenticated=True)
    clean_ids = [vid for vid in dict.fromkeys(video_ids) if vid]
    if not clean_ids:
        return {"added": 0, "response": None}
    response = None
    added = 0
    for i in range(0, len(clean_ids), 50):
        batch = clean_ids[i:i + 50]
        response = yt.add_playlist_items(playlist_id, batch)
        added += len(batch)
        time.sleep(0.1)
    return {"added": added, "response": response}
