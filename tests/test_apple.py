"""Apple Music library fetching under rate limits.

A 429 storm used to leave every page empty, which the pipeline then reported
as "No tracks found in your Apple Music library" - pointing the user at their
library when the real problem was Apple throttling us.
"""
import pytest

import backend.pipeline as pipeline
from backend.pipeline import AppleAPIError, _fetch_apple_library


class _Resp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {"data": []}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)


def test_rate_limiting_is_reported_not_silently_empty(monkeypatch):
    monkeypatch.setattr(
        pipeline.requests, "get",
        lambda *a, **kw: _Resp(429, headers={"Retry-After": "1"}, text="rate limited"),
    )
    with pytest.raises(AppleAPIError) as excinfo:
        _fetch_apple_library("dev-token", "user-token")

    assert excinfo.value.status == 429
    message = str(excinfo.value)
    assert "429" in message
    assert "rate-limiting" in message
    assert "No tracks found" not in message


def test_expired_session_names_itself(monkeypatch):
    monkeypatch.setattr(pipeline.requests, "get", lambda *a, **kw: _Resp(401, text="unauthorized"))
    with pytest.raises(AppleAPIError) as excinfo:
        _fetch_apple_library("dev-token", "user-token")

    assert excinfo.value.status == 401
    assert "reconnect Apple Music" in str(excinfo.value)


def test_a_throttled_page_is_retried_rather_than_dropped(monkeypatch):
    calls = {"n": 0}

    def flaky_get(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:                       # first request throttled
            return _Resp(429, headers={"Retry-After": "0"})
        return _Resp(payload={"data": [], "meta": {"total": 0}})

    monkeypatch.setattr(pipeline.requests, "get", flaky_get)
    tracks = _fetch_apple_library("dev-token", "user-token")

    assert calls["n"] > 1, "the throttled request should have been retried"
    assert tracks == []       # genuinely empty library, reported as such


def test_an_empty_or_missing_playlist_does_not_kill_the_run(monkeypatch):
    """Apple answers 404 for an empty or removed playlist.

    That is a fact about one playlist, not about the library, so the run must
    carry on and still return the songs it did find.
    """
    def routed_get(url, **kw):
        if "/library/playlists/p.DEAD/tracks" in url:
            return _Resp(404, text="not found")
        if "/library/playlists" in url and "/tracks" not in url:
            return _Resp(payload={
                "data": [{"id": "p.DEAD", "attributes": {"name": "Gone"}}],
                "meta": {"total": 1},
            })
        if "/library/songs" in url:
            return _Resp(payload={
                "data": [{"id": "i.keep", "attributes": {"name": "Kept", "artistName": "A"}}],
                "meta": {"total": 1},
            })
        return _Resp(payload={"data": [], "meta": {"total": 0}})

    monkeypatch.setattr(pipeline.requests, "get", routed_get)
    tracks = _fetch_apple_library("dev-token", "user-token")

    assert [t["name"] for t in tracks] == ["Kept"]


def test_auth_failure_is_still_fatal_even_for_one_playlist(monkeypatch):
    """A 403 means the token is dead, so skipping and carrying on would just
    produce a silently empty map."""
    def routed_get(url, **kw):
        if "/library/playlists" in url and "/tracks" in url:
            return _Resp(403, text="forbidden")
        if "/library/playlists" in url:
            return _Resp(payload={
                "data": [{"id": "p.1", "attributes": {"name": "One"}}],
                "meta": {"total": 1},
            })
        return _Resp(payload={"data": [], "meta": {"total": 0}})

    monkeypatch.setattr(pipeline.requests, "get", routed_get)
    with pytest.raises(AppleAPIError) as excinfo:
        _fetch_apple_library("dev-token", "user-token")
    assert excinfo.value.status == 403


def test_play_history_failing_does_not_stop_the_map(monkeypatch):
    """Heavy rotation and recently-played are a ranking bonus, not a source
    of tracks. Apple answers 400 there for an over-sized page, and that must
    not cost the user their whole map."""
    seen = {}

    def routed_get(url, **kw):
        if "/history/" in url or "/recent/" in url:
            seen[url.split("?")[0]] = url
            return _Resp(400, text="bad request")
        if "/library/songs" in url:
            return _Resp(payload={
                "data": [{"id": "i.1", "attributes": {
                    "name": "Kept", "artistName": "A", "dateAdded": "2026-01-02T03:04:05Z",
                }}],
                "meta": {"total": 1},
            })
        return _Resp(payload={"data": [], "meta": {"total": 0}})

    monkeypatch.setattr(pipeline.requests, "get", routed_get)
    tracks = _fetch_apple_library("dev-token", "user-token")

    assert [t["name"] for t in tracks] == ["Kept"]
    assert seen, "the history endpoints should still have been attempted"
    assert tracks[0]["is_top_track"] is True, "should fall back to date-added ranking"


def test_history_endpoints_ask_for_a_page_size_they_allow(monkeypatch):
    """Apple caps these well below the library's 100 and rejects more."""
    asked = {}

    def routed_get(url, **kw):
        if "/history/heavy-rotation" in url:
            asked["heavy"] = url
        if "/recent/played" in url:
            asked["recent"] = url
        return _Resp(payload={"data": [], "meta": {"total": 0}})

    monkeypatch.setattr(pipeline.requests, "get", routed_get)
    _fetch_apple_library("dev-token", "user-token")

    assert "limit=10&" in asked["heavy"], asked["heavy"]
    assert "limit=30&" in asked["recent"], asked["recent"]


def test_a_client_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def bad_request(*a, **kw):
        calls["n"] += 1
        return _Resp(400, text="bad request")

    monkeypatch.setattr(pipeline.requests, "get", bad_request)
    with pytest.raises(AppleAPIError):
        _fetch_apple_library("dev-token", "user-token")
    assert calls["n"] == 1, "a 400 will not improve on retry"
