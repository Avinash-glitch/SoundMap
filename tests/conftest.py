"""Shared test setup.

The app reads configuration at import time, so the environment has to be in a
known state before `backend.main` is first imported.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Deterministic, obviously-fake configuration. No test may reach the network.
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-anywhere-real")
os.environ.setdefault("CANONICAL_HOST", "soundmap.example.com")
os.environ.setdefault("SPOTIFY_CLIENT_ID", "test-client-id")
os.environ.setdefault("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback")
os.environ.pop("SOUNDMAP_DEBUG_ROUTES", None)
