"""Route wiring.

A helper function once landed between @app.post("/apple/start-job") and its
handler, so FastAPI registered the helper as the endpoint and answered every
request with 422 "field required" for a query parameter nobody sends. The
real handler was never reachable. These tests pin the wiring itself.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    import backend.main as main
    return TestClient(main.app)


def test_apple_start_job_is_wired_to_its_own_handler(client):
    import backend.main as main
    route = next(
        r for r in main.app.routes
        if getattr(r, "path", None) == "/apple/start-job"
    )
    assert route.endpoint.__name__ == "apple_start_job"


def test_apple_start_job_rejects_an_empty_body_in_plain_english(client):
    """An empty body is a missing token, not a framework validation error."""
    resp = client.post("/apple/start-job", json={})
    assert resp.status_code != 422, "422 means the route is bound to the wrong function"
    assert resp.status_code == 400
    assert resp.json()["detail"] == "music_user_token required"


@pytest.mark.parametrize("path,name", [
    ("/apple/start-job", "apple_start_job"),
    ("/start-job", "start_job"),
    ("/status/{job_id}", "job_status"),
    ("/healthz", "healthz"),
    ("/apple/token", "apple_developer_token"),
])
def test_routes_point_at_the_function_they_are_named_for(path, name):
    """Cheap guard against a decorator drifting onto the wrong definition."""
    import backend.main as main
    matches = [r for r in main.app.routes if getattr(r, "path", None) == path]
    assert matches, f"no route registered at {path}"
    assert any(r.endpoint.__name__ == name for r in matches), (
        f"{path} is handled by {[r.endpoint.__name__ for r in matches]}, expected {name}"
    )


def test_ai_health_is_wired_and_answers_without_a_key(client):
    """Every AI feature failed silently for weeks on a retired model id.
    This endpoint exists so that question has a direct answer."""
    import backend.main as main
    route = next(r for r in main.app.routes if getattr(r, "path", None) == "/ai/health")
    assert route.endpoint.__name__ == "ai_health"

    body = client.get("/ai/health").json()
    assert set(body) >= {"ok", "configured"}
def test_apple_disconnect_clears_the_server_side_token(client):
    """The escape hatch: without this the server keeps replaying a token
    Apple has already rejected."""
    import backend.main as main
    route = next(r for r in main.app.routes if getattr(r, "path", None) == "/apple/disconnect")
    assert route.endpoint.__name__ == "apple_disconnect"

    resp = client.post("/apple/disconnect")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
