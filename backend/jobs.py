"""Background job processing with ThreadPoolExecutor — no Redis needed for V1."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=4)

# job_id → {status, progress, message, user_id, display_name, error}
jobs: dict[str, dict] = {}
_stop_events: dict[str, threading.Event] = {}


def submit_job(
    access_token: str,
    user_id: str,
    display_name: str,
    api_key: str = "",
    provider: str = "",
    share_for_comparison: bool = True,
) -> str:
    """Queue a pipeline job and return the job_id."""
    from . import pipeline  # lazy import avoids circular deps at module load

    job_id = str(uuid.uuid4())
    stop_event = threading.Event()
    _stop_events[job_id] = stop_event

    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": "Queued — waiting to start…",
        "user_id": user_id,
        "display_name": display_name,
        "error": None,
    }

    def _on_progress(pct: int, message: str) -> None:
        jobs[job_id]["progress"] = pct
        jobs[job_id]["message"] = message

    def _run() -> None:
        jobs[job_id]["status"] = "processing"
        try:
            pipeline.process_user(
                access_token, user_id,
                on_progress=_on_progress,
                display_name=display_name,
                api_key=api_key,
                provider=provider,
                stop_event=stop_event,
                share_for_comparison=share_for_comparison,
            )
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["message"] = "Done!"
        except Exception as exc:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(exc)
            jobs[job_id]["message"] = f"Error: {exc}"
            print(f"[jobs] Pipeline failed for {user_id}: {exc}")

    _executor.submit(_run)
    return job_id


def submit_apple_job(
    music_user_token: str,
    user_id: str,
    storefront: str = "us",
    api_key: str = "",
    provider: str = "",
) -> str:
    """Queue an Apple Music pipeline job and return the job_id."""
    from . import pipeline

    apple_id = f"{user_id}_apple"
    job_id = str(uuid.uuid4())

    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": "Queued — waiting to start…",
        "user_id": apple_id,
        "display_name": "Apple Music Library",
        "error": None,
    }

    def _on_progress(pct: int, message: str) -> None:
        jobs[job_id]["progress"] = pct
        jobs[job_id]["message"] = message

    def _run() -> None:
        jobs[job_id]["status"] = "processing"
        try:
            pipeline.process_apple_user(
                music_user_token, user_id, storefront,
                on_progress=_on_progress,
                api_key=api_key,
                provider=provider,
            )
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["message"] = "Done!"
        except Exception as exc:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(exc)
            jobs[job_id]["message"] = f"Error: {exc}"
            print(f"[jobs] Apple pipeline failed for {user_id}: {exc}")

    _executor.submit(_run)
    return job_id


def stop_job(job_id: str) -> bool:
    """Signal the pipeline to stop fetching and build the map with what it has."""
    job = jobs.get(job_id)
    event = _stop_events.get(job_id)
    if job and event and job["status"] == "processing":
        event.set()
        return True
    return False


def get_job(job_id: str) -> dict | None:
    return jobs.get(job_id)
