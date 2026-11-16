"""The three risks called out in CURRENT_STATE.md, pinned down by tests."""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    import backend.main as main
    importlib.reload(main)
    return TestClient(main.app)


def test_healthz_is_open_and_cheap(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_debug_token_is_hidden_by_default(client):
    """Risk 1: /debug/token returned the caller's Spotify access token."""
    r = client.get("/debug/token")
    assert r.status_code == 404, "debug routes must not be reachable unless opted in"


def test_debug_playlists_is_hidden_by_default(client):
    r = client.get("/debug/playlists")
    assert r.status_code == 404


def test_debug_routes_can_be_switched_on(monkeypatch):
    """With the flag set they exist again, but still demand a session."""
    monkeypatch.setenv("SOUNDMAP_DEBUG_ROUTES", "1")
    import backend.main as main
    importlib.reload(main)
    try:
        r = TestClient(main.app).get("/debug/token")
        assert r.status_code == 401  # reachable, but nobody is logged in
    finally:
        monkeypatch.delenv("SOUNDMAP_DEBUG_ROUTES", raising=False)
        importlib.reload(main)


def test_cors_never_wildcards_with_credentials():
    """Risk: allow_origins=['*'] plus allow_credentials reflects any Origin."""
    import backend.main as main
    origins = main._allowed_origins()
    assert "*" not in origins
    assert "https://soundmap.example.com" in origins
    assert all(o.startswith("http://") or o.startswith("https://") for o in origins)


def test_cors_extra_origins_come_from_env(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://dev.example.com, https://other.example.com")
    import backend.main as main
    origins = main._allowed_origins()
    assert "https://dev.example.com" in origins
    assert "https://other.example.com" in origins


def test_session_secret_prefers_the_environment(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "a-real-secret")
    import backend.main as main
    assert main._session_secret() == "a-real-secret"


def test_session_secret_has_no_shared_default(monkeypatch):
    """Risk 3: a known fallback secret lets anyone forge a session."""
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    import backend.main as main
    first, second = main._session_secret(), main._session_secret()
    assert first != second, "generated secrets must not be predictable or reused"
    assert len(first) >= 32
    assert "dev-secret" not in first
