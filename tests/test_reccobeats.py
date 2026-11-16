"""Track resolution: the ISRC cache must spare us a per-track Spotify search."""
import backend.reccobeats as rb
import backend.storage as storage


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}

    def json(self):
        return self._payload


def test_cached_isrc_and_direct_ids_skip_the_search(monkeypatch):
    searches = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        if "api.spotify.com/v1/search" in url:
            searches["n"] += 1
            return _Resp({"tracks": {"items": [{"id": "B" * 22}]}})
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(rb.requests, "get", fake_get)
    monkeypatch.setattr(rb, "_spotify_search_token", lambda t=None: "token")
    # reccobeats imports storage inside the function, so patch the module itself
    monkeypatch.setattr(
        storage, "load_track_audio_features_by_isrc",
        lambda isrcs: {"ISRC1": {"isrc": "ISRC1", "spotify_id": "A" * 22}},
    )
    # every resolved id is already cached, so no ReccoBeats call is needed
    monkeypatch.setattr(
        storage, "load_track_audio_features",
        lambda ids: {sid: {"spotify_id": sid, "reccobeats_id": "rb-1", "tempo": 124.0} for sid in ids},
    )

    tracks = [
        {"id": "i.1", "isrc": "isrc1", "name": "Cached", "artist": "A"},   # ISRC in cache
        {"id": "i.2", "isrc": "ISRC9", "name": "New", "artist": "B"},      # needs a search
        {"id": "C" * 22, "name": "Direct", "artist": "C"},                 # already a Spotify id
    ]
    out = rb.get_audio_features_for_tracks(tracks, spotify_token=None)

    assert searches["n"] == 1, "only the uncached track should hit Spotify search"
    assert set(out) == {"i.1", "i.2", "C" * 22}
    assert out["i.1"]["spotify_id"] == "A" * 22
    assert out["C" * 22]["lookup_method"] == "spotify_id"


def test_no_tracks_is_not_a_request(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("should not touch the network")

    monkeypatch.setattr(rb.requests, "get", boom)
    assert rb.get_audio_features_for_tracks([]) == {}
