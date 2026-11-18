"""The single LLM entry point."""
import pytest

import backend.llm as llm


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(llm, "_resolved_model", None, raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def test_a_missing_key_says_what_to_do():
    with pytest.raises(llm.LLMNotConfigured) as exc:
        llm.api_key_from(None)
    assert "GEMINI_API_KEY" in str(exc.value)


def test_a_caller_key_beats_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "server-key")
    assert llm.api_key_from("user-key") == "user-key"
    assert llm.api_key_from(None) == "server-key"


def test_an_explicit_model_is_never_second_guessed(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-pinned")

    def boom(*a, **kw):
        raise AssertionError("should not ask the API when GEMINI_MODEL is set")

    monkeypatch.setattr(llm, "_client", boom)
    assert llm.resolve_model("k") == "gemini-pinned"


def test_the_model_is_discovered_rather_than_hard_coded(monkeypatch):
    """A retired model id is how every AI feature broke last time."""
    class _M:
        def __init__(self, i):
            self.id = i

    class _Client:
        class models:
            @staticmethod
            def list():
                return [_M("models/gemini-2.0-flash"), _M("models/gemini-2.5-flash-002")]

    monkeypatch.setattr(llm, "_client", lambda key, timeout=45.0: _Client())
    assert llm.resolve_model("k") == "gemini-2.5-flash-002"


def test_an_unreachable_model_list_still_yields_something_usable(monkeypatch):
    def boom(key, timeout=45.0):
        raise RuntimeError("network down")

    monkeypatch.setattr(llm, "_client", boom)
    assert llm.resolve_model("k").startswith("gemini-")


def test_health_reports_a_missing_key_without_calling_out():
    out = llm.health()
    assert out["ok"] is False and out["configured"] is False
    assert "GEMINI_API_KEY" in out["error"]


class _Err(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _client_raising(*statuses):
    """A client whose create() raises the given statuses in turn, then succeeds."""
    calls = {"n": 0, "models": []}

    class _Resp:
        class _C:
            class message:
                content = "  the answer  "
        choices = [_C()]

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(model, **kw):
                    calls["models"].append(model)
                    i = calls["n"]
                    calls["n"] += 1
                    if i < len(statuses):
                        raise _Err(statuses[i])
                    return _Resp()

        class models:
            @staticmethod
            def list():
                return []

    return _Client, calls


def test_a_busy_model_is_retried_not_surfaced(monkeypatch):
    """503 means overloaded. Riding it out beats showing an error code."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    Client, calls = _client_raising(503)
    monkeypatch.setattr(llm, "_client", lambda key, timeout=45.0: Client())

    assert llm.call_llm("s", "u", api_key="k") == "the answer"
    assert calls["n"] == 2


def test_a_model_that_keeps_failing_falls_back_to_another(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-busy")
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    Client, calls = _client_raising(503, 503)
    monkeypatch.setattr(llm, "_client", lambda key, timeout=45.0: Client())

    assert llm.call_llm("s", "u", api_key="k") == "the answer"
    assert calls["models"][0] == "gemini-busy"
    assert calls["models"][-1] != "gemini-busy", "should have moved to another model"


def test_a_bad_key_fails_immediately(monkeypatch):
    """No point retrying or trying other models with a rejected key."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    Client, calls = _client_raising(403, 403, 403, 403, 403, 403, 403, 403, 403)
    monkeypatch.setattr(llm, "_client", lambda key, timeout=45.0: Client())

    with pytest.raises(llm.LLMUnavailable) as exc:
        llm.call_llm("s", "u", api_key="k")
    assert "API key" in str(exc.value)
    assert calls["n"] == 1, "a rejected key should not be retried"


def test_retrying_is_capped_so_a_failure_cannot_burn_the_quota(monkeypatch):
    """Each failed try still bills its input tokens, and these prompts carry
    a whole music library."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    Client, calls = _client_raising(*([503] * 50))
    monkeypatch.setattr(llm, "_client", lambda key, timeout=45.0: Client())

    with pytest.raises(llm.LLMUnavailable):
        llm.call_llm("s", "u", api_key="k")
    assert calls["n"] == llm.MAX_ATTEMPTS, f"made {calls['n']} calls, budget is {llm.MAX_ATTEMPTS}"


def test_persistent_overload_explains_itself(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    Client, _ = _client_raising(*([503] * 20))
    monkeypatch.setattr(llm, "_client", lambda key, timeout=45.0: Client())

    with pytest.raises(llm.LLMUnavailable) as exc:
        llm.call_llm("s", "u", api_key="k")
    assert "busy" in str(exc.value).lower()
    assert "503" not in str(exc.value), "the user should get words, not a status code"
