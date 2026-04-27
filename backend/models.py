"""Pydantic models for API responses."""

from pydantic import BaseModel


class JobStatus(BaseModel):
    job_id: str
    status: str  # queued | processing | done | error
    progress: int  # 0-100
    message: str
    user_id: str
    display_name: str
    error: str | None = None
