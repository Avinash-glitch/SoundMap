"""File-based map data persistence — V1 (no database)."""

import json
import time
from pathlib import Path

STORAGE_DIR = Path("./user_data")


def _path(user_id: str) -> Path:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return STORAGE_DIR / f"{user_id}.json"


def save_map(user_id: str, map_data: dict) -> None:
    """Persist map data for a user."""
    _path(user_id).write_text(json.dumps(map_data), encoding="utf-8")


def load_map(user_id: str) -> dict | None:
    """Return map data or None if not found."""
    p = _path(user_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def map_exists(user_id: str) -> bool:
    return _path(user_id).exists()


def map_age_hours(user_id: str) -> float:
    """Return age of stored map in hours, or infinity if missing."""
    p = _path(user_id)
    if not p.exists():
        return float("inf")
    return (time.time() - p.stat().st_mtime) / 3600
