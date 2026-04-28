"""Spotify OAuth 2.0 with PKCE flow."""

import hashlib
import base64
import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
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


def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Redirect user to Spotify OAuth consent screen."""
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    redirect_uri = os.environ["SPOTIFY_REDIRECT_URI"]

    code_verifier, code_challenge = _generate_pkce_pair()
    request.session["code_verifier"] = code_verifier

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "scope": SCOPES,
        "show_dialog": "true",
    }
    return RedirectResponse(f"{SPOTIFY_AUTH_URL}?{urlencode(params)}")


@router.get("/callback")
async def callback(request: Request, code: str | None = None, error: str | None = None) -> RedirectResponse:
    """Handle Spotify OAuth callback, exchange code for token, start pipeline job."""
    app_url = os.environ.get("APP_URL", "http://localhost:8000")

    if error or not code:
        return RedirectResponse(f"{app_url}/?error=spotify_denied")

    code_verifier = request.session.pop("code_verifier", None)
    if not code_verifier:
        return RedirectResponse(f"{app_url}/?error=session_expired")

    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    redirect_uri = os.environ["SPOTIFY_REDIRECT_URI"]

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
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

    # Skip processing if a fresh map already exists
    if storage.map_exists(user_id) and storage.map_age_hours(user_id) < 24:
        print(f"[auth] Fresh map found for {user_id} — skipping pipeline")
        return RedirectResponse(f"{app_url}/map.html?user={user_id}")

    return RedirectResponse(f"{app_url}/loading.html?user={user_id}")


async def refresh_access_token(request: Request) -> str | None:
    """Exchange the stored refresh token for a new access token. Updates session in place."""
    refresh_token = request.session.get("refresh_token", "")
    if not refresh_token:
        return None
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
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
