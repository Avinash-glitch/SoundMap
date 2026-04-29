"""Apple Music developer token generation via ES256 JWT."""

import os
import re as _re
import time


def _rebuild_pem(raw: str) -> str:
    """
    Reconstruct a well-formed PEM block regardless of how the key was stored.
    Handles: real newlines, literal \\n, all-on-one-line, extra whitespace.
    """
    s = raw.replace("\\n", "\n").strip()
    m = _re.match(r"(-----BEGIN[^-]*-----)(.+?)(-----END[^-]*-----)", s, _re.DOTALL)
    if not m:
        return s  # already odd — pass through and let JWT lib raise a clear error
    header = m.group(1).strip()
    body = _re.sub(r"\s+", "", m.group(2))   # strip ALL whitespace from base64 body
    footer = m.group(3).strip()
    wrapped = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"{header}\n{wrapped}\n{footer}\n"


def get_developer_token(expiry_seconds: int = 15777000) -> str:
    """Generate a short-lived Apple Music developer token (valid ~6 months by default)."""
    team_id = os.environ.get("APPLE_TEAM_ID", "")
    key_id = os.environ.get("APPLE_KEY_ID", "")
    private_key_raw = os.environ.get("APPLE_PRIVATE_KEY", "")

    if not all([team_id, key_id, private_key_raw]):
        raise EnvironmentError(
            "APPLE_TEAM_ID, APPLE_KEY_ID, and APPLE_PRIVATE_KEY must all be set"
        )

    private_key = _rebuild_pem(private_key_raw)

    import jwt as _jwt  # PyJWT with cryptography backend
    now = int(time.time())
    return _jwt.encode(
        {"iss": team_id, "iat": now, "exp": now + expiry_seconds},
        private_key,
        algorithm="ES256",
        headers={"kid": key_id},
    )


def is_configured() -> bool:
    return bool(
        os.environ.get("APPLE_TEAM_ID")
        and os.environ.get("APPLE_KEY_ID")
        and os.environ.get("APPLE_PRIVATE_KEY")
    )
