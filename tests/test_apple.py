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


def test_a_client_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def bad_request(*a, **kw):
        calls["n"] += 1
        return _Resp(400, text="bad request")

    monkeypatch.setattr(pipeline.requests, "get", bad_request)
    with pytest.raises(AppleAPIError):
        _fetch_apple_library("dev-token", "user-token")
    assert calls["n"] == 1, "a 400 will not improve on retry"
