"""Map data persistence — Supabase with file-based fallback."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

STORAGE_DIR = Path("./user_data")

_supabase = None


def _get_supabase():
    global _supabase
    if _supabase is not None:
        return _supabase
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _supabase = create_client(url, key)
        print("[storage] Supabase connected")
        return _supabase
    except Exception as e:
        print(f"[storage] Supabase init failed: {e}")
        return None


def save_map(user_id: str, map_data: dict) -> None:
    """Persist map data. Uses Supabase if configured, else file system."""
    sb = _get_supabase()
    if sb:
        try:
            sb.table("user_maps").upsert({
                "user_id": user_id,
                "map_data": map_data,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            return
        except Exception as e:
            print(f"[storage] Supabase save failed, falling back to file: {e}")
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    (STORAGE_DIR / f"{user_id}.json").write_text(json.dumps(map_data), encoding="utf-8")


def load_map(user_id: str) -> dict | None:
    """Return map data or None if not found."""
    sb = _get_supabase()
    if sb:
        try:
            result = sb.table("user_maps").select("map_data").eq("user_id", user_id).execute()
            if result.data:
                return result.data[0]["map_data"]
            return None
        except Exception as e:
            print(f"[storage] Supabase load failed, falling back to file: {e}")
    p = STORAGE_DIR / f"{user_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def map_exists(user_id: str) -> bool:
    """Check if a map exists for a user."""
    sb = _get_supabase()
    if sb:
        try:
            result = sb.table("user_maps").select("user_id").eq("user_id", user_id).execute()
            return bool(result.data)
        except Exception as e:
            print(f"[storage] Supabase exists check failed: {e}")
    return (STORAGE_DIR / f"{user_id}.json").exists()


def map_age_hours(user_id: str) -> float:
    """Return age of stored map in hours, or infinity if missing."""
    sb = _get_supabase()
    if sb:
        try:
            result = sb.table("user_maps").select("updated_at").eq("user_id", user_id).execute()
            if result.data:
                updated_at = result.data[0]["updated_at"]
                dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            return float("inf")
        except Exception as e:
            print(f"[storage] Supabase age check failed: {e}")
    p = STORAGE_DIR / f"{user_id}.json"
    if not p.exists():
        return float("inf")
    return (time.time() - p.stat().st_mtime) / 3600
