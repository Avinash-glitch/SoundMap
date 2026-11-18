"""The single place SoundMap talks to a language model.

Everything AI in this app goes through call_llm(). One provider, one key,
one code path - previously the routing existed twice (pipeline.py and
main.py) and drifted.

Gemini is reached through its OpenAI-compatible endpoint, so this uses the
openai package that is already a dependency rather than adding an SDK.
"""

import os
import threading

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


def _client(api_key: str):
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL, timeout=90.0, max_retries=2)


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
            available = [m.id for m in _client(api_key).models.list()]
            names = [a.split("/")[-1] for a in available]
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


def call_llm(
    system: str,
    user: str,
    api_key: str | None = None,
    max_tokens: int = 1200,
) -> str:
    """One chat completion. Returns the reply text."""
    key = api_key_from(api_key)
    model = resolve_model(key)
    resp = _client(key).chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def health(api_key: str | None = None) -> dict:
    """What the AI features would do right now, without guessing."""
    try:
        key = api_key_from(api_key)
    except LLMNotConfigured as exc:
        return {"ok": False, "configured": False, "error": str(exc)}
    try:
        model = resolve_model(key, refresh=True)
        reply = call_llm("Reply with the single word: ok", "ping", api_key=key, max_tokens=16)
        return {"ok": True, "configured": True, "model": model, "reply": reply[:40]}
    except Exception as exc:
        return {"ok": False, "configured": True, "error": f"{type(exc).__name__}: {exc}"[:300]}
