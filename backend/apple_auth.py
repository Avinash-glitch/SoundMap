"""Apple Music developer token generation via ES256 JWT."""

import base64 as _b64
import os
import re as _re
import time


def _load_ec_key(raw: str):
    """
    Load the EC private key from an env var regardless of how it was stored.
    Strips all PEM headers/footers and whitespace, decodes the raw base64 body
    as DER, and returns a cryptography key object — no PEM parsing required.
    """
    from cryptography.hazmat.primitives.serialization import load_der_private_key
    # Remove everything that is not base64: headers, footers, whitespace, literal \n
    b64 = _re.sub(r"-----[^-]+-----|\\n|\s+", "", raw)
    der = _b64.b64decode(b64)
    return load_der_private_key(der, password=None)


def get_developer_token(expiry_seconds: int = 15777000) -> str:
    """Generate a short-lived Apple Music developer token (valid ~6 months by default)."""
    team_id = os.environ.get("APPLE_TEAM_ID", "")
    key_id = os.environ.get("APPLE_KEY_ID", "")
    private_key_raw = os.environ.get("APPLE_PRIVATE_KEY", "")

    if not all([team_id, key_id, private_key_raw]):
        raise EnvironmentError(
            "APPLE_TEAM_ID, APPLE_KEY_ID, and APPLE_PRIVATE_KEY must all be set"
        )

    private_key = _load_ec_key(private_key_raw)

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
