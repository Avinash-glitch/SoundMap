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

    def boom(_):
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

    monkeypatch.setattr(llm, "_client", lambda key: _Client())
    assert llm.resolve_model("k") == "gemini-2.5-flash-002"


def test_an_unreachable_model_list_still_yields_something_usable(monkeypatch):
    def boom(key):
        raise RuntimeError("network down")

    monkeypatch.setattr(llm, "_client", boom)
    assert llm.resolve_model("k").startswith("gemini-")


def test_health_reports_a_missing_key_without_calling_out():
    out = llm.health()
    assert out["ok"] is False and out["configured"] is False
    assert "GEMINI_API_KEY" in out["error"]
