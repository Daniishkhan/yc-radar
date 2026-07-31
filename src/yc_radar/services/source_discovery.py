from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine

from yc_radar.services.database import url_inventory_writer_lock
from yc_radar.services.job_source_registry import JobSourceRegistry


def discover_job_sources(engine: Engine, *, provider: str | None = None) -> dict[str, Any]:
    """Register supported job sources while cleanup cannot invalidate their provenance."""
    with url_inventory_writer_lock(engine):
        return JobSourceRegistry(engine).discover_from_career_pages(provider=provider)


def discover_greenhouse_sources(engine: Engine) -> dict[str, Any]:
    """Backward-compatible Greenhouse-only registration entry point."""
    return discover_job_sources(engine, provider="greenhouse")
