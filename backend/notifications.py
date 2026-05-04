"""Best-effort webhook notifications for visits, logins, and map jobs."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import requests

_executor = ThreadPoolExecutor(max_workers=2)


def _webhook_url() -> str:
    """Return the configured notification webhook URL, if any."""
    return os.environ.get("SOUNDMAP_NOTIFY_WEBHOOK_URL", "").strip()


def notifications_enabled() -> bool:
    """Return True when webhook notifications are configured."""
    return bool(_webhook_url())


def _truncate(value: str, limit: int = 180) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _format_fields(fields: dict[str, Any] | None) -> str:
    if not fields:
        return ""
    lines: list[str] = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {_truncate(str(value))}")
    return "\n".join(lines)


def send_notification(title: str, **fields: Any) -> None:
    """Send a best-effort webhook notification without blocking the caller."""
    webhook_url = _webhook_url()
    if not webhook_url:
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    body = _format_fields(fields)
    message = f"SoundMap: {title}\n{body}\nTime: {timestamp}" if body else f"SoundMap: {title}\nTime: {timestamp}"
    payload = {"text": message, "content": message, "username": "SoundMap"}

    def _post() -> None:
        try:
            requests.post(webhook_url, json=payload, timeout=5)
        except Exception as exc:
            print(f"[notify] Failed to send notification: {exc}")

    _executor.submit(_post)
