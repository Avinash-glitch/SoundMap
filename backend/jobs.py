"""Background job processing with ThreadPoolExecutor — no Redis needed for V1."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

_executor = ThreadPoolExecutor(max_workers=4)

# job_id → {status, progress, message, user_id, display_name, error}
jobs: dict[str, dict] = {}


def submit_job(access_token: str, user_id: str, display_name: str) -> str:
    """Queue a pipeline job and return the job_id."""
    from . import pipeline  # lazy import avoids circular deps at module load

    job_id = str(uuid.uuid4())
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
            pipeline.process_user(access_token, user_id, on_progress=_on_progress, display_name=display_name)
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


def get_job(job_id: str) -> dict | None:
    return jobs.get(job_id)
