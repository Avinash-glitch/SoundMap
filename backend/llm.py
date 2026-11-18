"""The single place SoundMap talks to a language model.

Everything AI in this app goes through call_llm(). One provider, one key,
one code path - previously the routing existed twice (pipeline.py and
main.py) and drifted.

Gemini is reached through its OpenAI-compatible endpoint, so this uses the
openai package that is already a dependency rather than adding an SDK.
"""

import os
import threading
import time

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Tried in order when GEMINI_MODEL is not set. Matching is by prefix against
# whatever the account can actually see, so a point release (…-flash-002)
# still matches …-flash.
_MODEL_PREFERENCE = (
    "gemini-flash-latest",
    "gemini-3-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-pro-latest",
    "gemini-3-pro",
    "gemini-2.5-pro",
)

_resolved_model: str | None = None
_resolve_lock = threading.Lock()


class LLMNotConfigured(RuntimeError):
    """No API key available, from the caller or the environment."""


def api_key_from(explicit: str | None = None) -> str:
    key = (explicit or "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise LLMNotConfigured(
            "No Gemini API key. Add yours in AI settings, or set GEMINI_API_KEY "
            "on the server."
        )
    return key


def is_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _client(api_key: str, timeout: float = 45.0):
    """max_retries=0 on purpose: retries are handled in call_llm.

    Leaving the SDK's own retries on multiplied with ours - up to three
    models x three attempts x two SDK retries - so a busy model could hang a
    request for minutes instead of failing usefully.
    """
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL, timeout=timeout, max_retries=0)


def list_models(api_key: str, timeout: float = 10.0) -> list[str]:
    """Model ids this key can actually use, newest-looking first."""
    raw = [m.id for m in _client(api_key, timeout=timeout).models.list()]
    return [r.split("/")[-1] for r in raw]


def resolve_model(api_key: str, refresh: bool = False) -> str:
    """Pick a model that exists, rather than trusting a hard-coded name.

    A retired model id is how every AI feature broke last time: the code asked
    for one Google (or in that case NVIDIA) had removed, and every call failed.
    GEMINI_MODEL always wins when set; otherwise the account's own model list
    decides, and the answer is cached for the process.
    """
    override = os.environ.get("GEMINI_MODEL", "").strip()
    if override:
        return override

    global _resolved_model
    if _resolved_model and not refresh:
        return _resolved_model

    with _resolve_lock:
        if _resolved_model and not refresh:
            return _resolved_model
        fallback = "gemini-2.5-flash"
        try:
            names = list_models(api_key)
            chosen = next(
                (n for pref in _MODEL_PREFERENCE for n in names if n.startswith(pref)),
                None,
            ) or next((n for n in names if "flash" in n), None) or (names[0] if names else fallback)
            print(f"[llm] using Gemini model {chosen} ({len(names)} available)", flush=True)
            _resolved_model = chosen
        except Exception as exc:
            print(f"[llm] could not list models ({exc}); falling back to {fallback}", flush=True)
            _resolved_model = fallback
        return _resolved_model


# Worth waiting out: the service is busy, not the request wrong.
_TRANSIENT = {429, 500, 502, 503, 504}
# The model itself is the problem: retrying it changes nothing, another might work.
_WRONG_MODEL = {400, 404}


def _status_of(exc: Exception) -> int | None:
    for attr in ("status_code", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    return status if isinstance(status, int) else None


def _friendly(status: int | None, exc: Exception) -> str:
    if status in (401, 403):
        return "Gemini rejected the API key. Check it in AI settings, or GEMINI_API_KEY on the server."
    if status == 429:
        return "Gemini rate limit reached. Wait a moment and try again."
    if status in _TRANSIENT:
        return "Gemini is busy right now and did not respond in time. Try again in a moment."
    if status in _WRONG_MODEL:
        return f"Gemini rejected the request ({status}). {exc}"[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


class LLMUnavailable(RuntimeError):
    """The model could not be reached after retrying. Usually transient."""


def _candidates(key: str) -> list[str]:
    """The resolved model first, then a couple of alternates.

    Gemini answers 503 when a model is overloaded, which is common on the
    free tier, so a second model is often the difference between a working
    feature and an error message.
    """
    first = resolve_model(key)
    rest = [m for m in ("gemini-2.5-flash", "gemini-2.0-flash") if m != first]
    return [first, *rest]


def call_llm(
    system: str,
    user: str,
    api_key: str | None = None,
    max_tokens: int = 1200,
) -> str:
    """One chat completion, retried through the noise. Returns the reply text."""
    key = api_key_from(api_key)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_exc: Exception | None = None
    last_status: int | None = None

    for model in _candidates(key):
        for attempt in range(3):
            try:
                resp = _client(key).chat.completions.create(
                    model=model, max_tokens=max_tokens, messages=messages,
                )
                return (resp.choices[0].message.content or "").strip()
            except Exception as exc:
                last_exc, last_status = exc, _status_of(exc)
                if last_status in (401, 403):
                    raise LLMUnavailable(_friendly(last_status, exc)) from exc
                if last_status in _TRANSIENT and attempt < 2:
                    wait = 1.5 * (2 ** attempt)
                    print(f"[llm] {model} returned {last_status}; retrying in {wait:.1f}s", flush=True)
                    time.sleep(wait)
                    continue
                break  # out of retries, or the model itself is wrong: try the next
        print(f"[llm] giving up on {model} (last status {last_status})", flush=True)

    raise LLMUnavailable(_friendly(last_status, last_exc or RuntimeError("unknown")))


def health(api_key: str | None = None) -> dict:
    """What the AI features would do right now, without guessing.

    One attempt on a short timeout, no retries and no model fallback: a probe
    that can take minutes to answer tells you nothing you can act on.
    """
    try:
        key = api_key_from(api_key)
    except LLMNotConfigured as exc:
        return {"ok": False, "configured": False, "error": str(exc)}

    model = None
    try:
        model = resolve_model(key, refresh=True)
        resp = _client(key, timeout=12.0).chat.completions.create(
            model=model,
            max_tokens=16,
            messages=[
                {"role": "system", "content": "Reply with the single word: ok"},
                {"role": "user", "content": "ping"},
            ],
        )
        reply = (resp.choices[0].message.content or "").strip()
        return {"ok": True, "configured": True, "model": model, "reply": reply[:40]}
    except Exception as exc:
        status = _status_of(exc)
        out = {
            "ok": False,
            "configured": True,
            "model": model,
            "status": status,
            "error": _friendly(status, exc),
        }
        # Which model to use is the usual answer to a failing health check,
        # so say what this key can actually reach rather than making someone
        # guess. Set GEMINI_MODEL to one of these to pin it.
        try:
            out["available"] = list_models(key)[:25]
        except Exception as list_exc:
            out["available_error"] = f"{type(list_exc).__name__}: {list_exc}"[:200]
        return out
