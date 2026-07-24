from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class NormalizedJob(BaseModel):
    """Provider-neutral public job representation with a stable provider ID."""

    external_job_id: str
    title: str
    posting_url: str | None = None
    apply_url: str | None = None
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    content_hash: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class SourceSnapshot(BaseModel):
    """One adapter fetch result. Only complete snapshots may alter job lifecycle state."""

    provider: str
    external_source_id: str
    adapter_version: str
    is_complete: bool
    jobs: list[NormalizedJob] = Field(default_factory=list)
    http_status: int | None = None
    errors: list[dict[str, str]] = Field(default_factory=list)
    request_metadata: dict[str, Any] = Field(default_factory=dict)


class SyncResult(BaseModel):
    career_source_id: int
    run_id: int
    run_key: str
    status: str
    is_complete_scan: bool
    jobs_fetched: int = 0
    jobs_added: int = 0
    jobs_updated: int = 0
    jobs_unchanged: int = 0
    jobs_missed: int = 0
    jobs_closed: int = 0
    jobs_reactivated: int = 0
    errors_count: int = 0
    idempotent_replay: bool = False
