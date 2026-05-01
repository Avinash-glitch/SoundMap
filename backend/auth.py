"""Spotify OAuth 2.0 with PKCE flow."""

import hashlib
import base64
import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .jobs import submit_job
from . import storage

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

SCOPES = " ".join([
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-top-read",
    "user-read-recently-played",
    "user-read-private",
    "user-read-currently-playing",
    "user-read-playback-state",
])

router = APIRouter(prefix="/auth")
_oauth_options: dict[str, dict] = {}
_spotify_client_secrets_by_user: dict[str, str] = {}


def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


@router.get("/login")
async def login(
    request: Request,
    next: str | None = None,
    force: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    share: str | None = None,
) -> RedirectResponse:
    """Redirect user to Spotify OAuth consent screen."""
    return _start_login(request, next, force, client_id, client_secret, share)


@router.post("/custom-login")
async def custom_login(
    request: Request,
    client_id: str = Form(""),
    client_secret: str = Form(""),
    share: str = Form("1"),
) -> RedirectResponse:
    """Start Spotify OAuth with optional user-supplied app credentials."""
    return _start_login(request, None, None, client_id, client_secret, share)


def _start_login(
    request: Request,
    next: str | None,
    force: str | None,
    client_id: str | None,
    client_secret: str | None,
    share: str | None,
) -> RedirectResponse:
    spotify_client_id = (client_id or "").strip() or os.environ["SPOTIFY_CLIENT_ID"]
    spotify_client_secret = (client_secret or "").strip()
    redirect_uri = os.environ["SPOTIFY_REDIRECT_URI"]

    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    request.session["code_verifier"] = code_verifier
    request.session["oauth_state"] = state
    request.session["spotify_client_id"] = spotify_client_id
    if next:
        request.session["login_next"] = next
    if force in ("1", "true", "yes"):
        request.session["force_rebuild"] = True
    if share in ("0", "false", "no"):
        request.session["share_for_comparison"] = False
    else:
        request.session["share_for_comparison"] = True
    _oauth_options[state] = {
        "client_id": spotify_client_id,
        "client_secret": spotify_client_secret,
        "share_for_comparison": bool(request.session["share_for_comparison"]),
    }

    params = {
        "client_id": spotify_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "scope": SCOPES,
        "show_dialog": "true",
    }
    return RedirectResponse(f"{SPOTIFY_AUTH_URL}?{urlencode(params)}", status_code=303)


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
) -> RedirectResponse:
    """Handle Spotify OAuth callback, exchange code for token, start pipeline job."""
    app_url = os.environ.get("APP_URL", "http://localhost:8000")

    if error or not code:
        return RedirectResponse(f"{app_url}/?error=spotify_denied")

    code_verifier = request.session.pop("code_verifier", None)
    if not code_verifier:
        return RedirectResponse(f"{app_url}/?error=session_expired")
    expected_state = request.session.pop("oauth_state", None)
    if not state or state != expected_state:
        return RedirectResponse(f"{app_url}/?error=session_expired")

    oauth_options = _oauth_options.pop(state, {})
    client_id = oauth_options.get("client_id") or request.session.get("spotify_client_id") or os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = oauth_options.get("client_secret") or ""
    redirect_uri = os.environ["SPOTIFY_REDIRECT_URI"]

    token_payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        token_payload["client_secret"] = client_secret

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            SPOTIFY_TOKEN_URL,
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if token_resp.status_code != 200:
        return RedirectResponse(f"{app_url}/?error=token_exchange_failed")

    token_data = token_resp.json()
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token", "")

    # Fetch user profile
    async with httpx.AsyncClient() as client:
        profile_resp = await client.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if profile_resp.status_code != 200:
        return RedirectResponse(f"{app_url}/?error=profile_fetch_failed")

    profile = profile_resp.json()
    user_id = profile["id"]
    display_name = profile.get("display_name", user_id)

    print(f"[auth] User logged in: {display_name} ({user_id})")

    # Store tokens in session for downstream endpoints and playlist creation
    request.session["access_token"] = access_token
    request.session["refresh_token"] = refresh_token
    request.session["user_id"] = user_id
    request.session["display_name"] = display_name
    if client_secret:
        _spotify_client_secrets_by_user[user_id] = client_secret

    login_next = request.session.pop("login_next", None)
    share_for_comparison = bool(oauth_options.get("share_for_comparison", request.session.get("share_for_comparison", True)))
    extra = "&connect_apple=1" if login_next == "apple" else ""
    if not share_for_comparison:
        extra += "&private_map=1"

    # Skip processing if a fresh map already exists
    force_rebuild = bool(request.session.pop("force_rebuild", False))
    existing_map = storage.load_map(user_id) if storage.map_exists(user_id) else None
    existing_share = bool(existing_map.get("share_for_comparison", True)) if existing_map else None
    share_changed = existing_share is not None and existing_share != share_for_comparison
    if not force_rebuild and not share_changed and existing_map and storage.map_age_hours(user_id) < 24:
        print(f"[auth] Fresh map found for {user_id} — skipping pipeline")
        return RedirectResponse(f"{app_url}/map.html?user={user_id}{extra}")

    return RedirectResponse(f"{app_url}/loading.html?user={user_id}{extra}")


async def refresh_access_token(request: Request) -> str | None:
    """Exchange the stored refresh token for a new access token. Updates session in place."""
    refresh_token = request.session.get("refresh_token", "")
    if not refresh_token:
        return None
    client_id = request.session.get("spotify_client_id") or os.environ["SPOTIFY_CLIENT_ID"]
    user_id = request.session.get("user_id", "")
    client_secret = _spotify_client_secrets_by_user.get(user_id, "")
    payload = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    if client_secret:
        payload["client_secret"] = client_secret
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SPOTIFY_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        return None
    data = resp.json()
    new_token = data.get("access_token")
    if new_token:
        request.session["access_token"] = new_token
    if data.get("refresh_token"):
        request.session["refresh_token"] = data["refresh_token"]
    return new_token


def forget_user_credentials(user_id: str) -> None:
    """Remove any server-side custom Spotify client secret cached for a user."""
    _spotify_client_secrets_by_user.pop(user_id, None)
