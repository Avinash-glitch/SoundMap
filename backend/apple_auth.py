"""Apple Music developer token generation via ES256 JWT."""

import os
import time


def get_developer_token(expiry_seconds: int = 15777000) -> str:
    """Generate a short-lived Apple Music developer token (valid ~6 months by default)."""
    team_id = os.environ.get("APPLE_TEAM_ID", "")
    key_id = os.environ.get("APPLE_KEY_ID", "")
    private_key_raw = os.environ.get("APPLE_PRIVATE_KEY", "")

    if not all([team_id, key_id, private_key_raw]):
        raise EnvironmentError(
            "APPLE_TEAM_ID, APPLE_KEY_ID, and APPLE_PRIVATE_KEY must all be set"
        )

    # Env vars store newlines as \n literals — restore them
    private_key = private_key_raw.replace("\\n", "\n")

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
