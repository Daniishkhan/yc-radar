"""Read-only freshness diagnostics for the canonical source-sync pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, distinct, func, select
from sqlalchemy.engine import Engine

from yc_radar.services.database import (
    company_sources_table,
    jobs_table,
    sync_runs_table,
)

MONITORED_SOURCE_KIND = "ats_board"


@dataclass(frozen=True)
class ProviderFreshness:
    provider: str
    active_complete_source_count: int
    latest_attempt_at: datetime | None
    latest_successful_complete_sync_at: datetime | None

    def as_dict(self, *, now: datetime, max_age: timedelta) -> dict[str, Any]:
        latest_success = _as_utc(self.latest_successful_complete_sync_at)
        age_seconds = (
            max(0.0, (now - latest_success).total_seconds())
            if latest_success is not None
            else None
        )
        return {
            "provider": self.provider,
            "active_complete_source_count": self.active_complete_source_count,
            "latest_attempt_at": _isoformat(self.latest_attempt_at),
            "latest_successful_complete_sync_at": _isoformat(latest_success),
            "successful_sync_age_seconds": age_seconds,
            "fresh": age_seconds is not None and age_seconds <= max_age.total_seconds(),
        }


@dataclass(frozen=True)
class PipelineFreshnessSnapshot:
    active_complete_source_count: int
    active_job_count: int
    running_sync_count: int
    latest_attempt_at: datetime | None
    latest_successful_complete_sync_at: datetime | None
    latest_job_seen_at: datetime | None
    providers: tuple[ProviderFreshness, ...] = ()


def inspect_pipeline_freshness(
    engine: Engine,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
) -> dict[str, Any]:
    """Return a read-only health report and never create or mutate schema state."""
    checked_at = _as_utc(now or datetime.now(UTC))
    if max_age.total_seconds() <= 0:
        raise ValueError("max_age must be positive")

    successful_complete = (
        (sync_runs_table.c.status == "completed")
        & sync_runs_table.c.is_complete.is_(True)
        & sync_runs_table.c.completed_at.is_not(None)
    )
    active_complete_source = (
        (company_sources_table.c.status == "active")
        & (company_sources_table.c.sync_mode == "complete_snapshot")
        # The recurring synchronizer owns ATS boards. Directory snapshots such as YC are
        # refreshed by their own import command and must not make this daily alarm permanently
        # stale.
        & (company_sources_table.c.source_kind == MONITORED_SOURCE_KIND)
    )

    with engine.connect() as connection:
        source_summary = connection.execute(
            select(
                func.count(distinct(company_sources_table.c.id)).label("source_count"),
                func.max(sync_runs_table.c.started_at).label("latest_attempt_at"),
                func.max(
                    case(
                        (successful_complete, sync_runs_table.c.completed_at),
                        else_=None,
                    )
                ).label("latest_success_at"),
                func.count(distinct(sync_runs_table.c.id))
                .filter(sync_runs_table.c.status == "running")
                .label("running_count"),
            )
            .select_from(
                company_sources_table.outerjoin(
                    sync_runs_table,
                    sync_runs_table.c.company_source_id == company_sources_table.c.id,
                )
            )
            .where(active_complete_source)
        ).mappings().one()

        job_summary = connection.execute(
            select(
                func.count().filter(jobs_table.c.status == "active").label("active_job_count"),
                func.max(jobs_table.c.last_seen_at).label("latest_job_seen_at"),
            ).select_from(jobs_table)
        ).mappings().one()

        provider_rows = connection.execute(
            select(
                company_sources_table.c.provider,
                func.count(distinct(company_sources_table.c.id)).label("source_count"),
                func.max(sync_runs_table.c.started_at).label("latest_attempt_at"),
                func.max(
                    case(
                        (successful_complete, sync_runs_table.c.completed_at),
                        else_=None,
                    )
                ).label("latest_success_at"),
            )
            .select_from(
                company_sources_table.outerjoin(
                    sync_runs_table,
                    sync_runs_table.c.company_source_id == company_sources_table.c.id,
                )
            )
            .where(active_complete_source)
            .group_by(company_sources_table.c.provider)
            .order_by(company_sources_table.c.provider)
        ).mappings()

        providers = tuple(
            ProviderFreshness(
                provider=str(row["provider"]),
                active_complete_source_count=int(row["source_count"] or 0),
                latest_attempt_at=row["latest_attempt_at"],
                latest_successful_complete_sync_at=row["latest_success_at"],
            )
            for row in provider_rows
        )

    snapshot = PipelineFreshnessSnapshot(
        active_complete_source_count=int(source_summary["source_count"] or 0),
        active_job_count=int(job_summary["active_job_count"] or 0),
        running_sync_count=int(source_summary["running_count"] or 0),
        latest_attempt_at=source_summary["latest_attempt_at"],
        latest_successful_complete_sync_at=source_summary["latest_success_at"],
        latest_job_seen_at=job_summary["latest_job_seen_at"],
        providers=providers,
    )
    return assess_pipeline_freshness(snapshot, now=checked_at, max_age=max_age)


def assess_pipeline_freshness(
    snapshot: PipelineFreshnessSnapshot,
    *,
    now: datetime,
    max_age: timedelta,
) -> dict[str, Any]:
    """Classify a database snapshot separately from the SQL inspection boundary."""
    checked_at = _as_utc(now)
    if max_age.total_seconds() <= 0:
        raise ValueError("max_age must be positive")

    latest_success = _as_utc(snapshot.latest_successful_complete_sync_at)
    age_seconds = (
        max(0.0, (checked_at - latest_success).total_seconds())
        if latest_success is not None
        else None
    )
    failures: list[dict[str, str]] = []
    if snapshot.active_complete_source_count == 0:
        failures.append(
            {
                "code": "no_active_complete_sources",
                "message": "No active complete-snapshot company sources are registered.",
            }
        )
    if latest_success is None:
        failures.append(
            {
                "code": "no_successful_complete_sync",
                "message": "No successful complete source snapshot has finished.",
            }
        )
    elif age_seconds is not None and age_seconds > max_age.total_seconds():
        failures.append(
            {
                "code": "successful_complete_sync_stale",
                "message": (
                    "The newest successful complete source snapshot is older than "
                    f"{max_age.total_seconds() / 3600:g} hours."
                ),
            }
        )

    for provider in snapshot.providers:
        if provider.active_complete_source_count <= 0:
            continue
        provider_latest_success = _as_utc(provider.latest_successful_complete_sync_at)
        if provider_latest_success is None:
            failures.append(
                {
                    "code": "provider_no_successful_complete_sync",
                    "provider": provider.provider,
                    "message": (
                        f"Provider {provider.provider!r} has active complete-snapshot sources "
                        "but no successful complete sync."
                    ),
                }
            )
            continue
        provider_age_seconds = max(
            0.0,
            (checked_at - provider_latest_success).total_seconds(),
        )
        if provider_age_seconds > max_age.total_seconds():
            failures.append(
                {
                    "code": "provider_successful_complete_sync_stale",
                    "provider": provider.provider,
                    "message": (
                        f"Provider {provider.provider!r} has no successful complete sync within "
                        f"{max_age.total_seconds() / 3600:g} hours."
                    ),
                }
            )

    return {
        "schema_version": 1,
        "status": "stale" if failures else "healthy",
        "checked_at": checked_at.isoformat(),
        "max_age_seconds": max_age.total_seconds(),
        "active_complete_source_count": snapshot.active_complete_source_count,
        "active_job_count": snapshot.active_job_count,
        "running_sync_count": snapshot.running_sync_count,
        "latest_attempt_at": _isoformat(snapshot.latest_attempt_at),
        "latest_successful_complete_sync_at": _isoformat(latest_success),
        "successful_complete_sync_age_seconds": age_seconds,
        "latest_job_seen_at": _isoformat(snapshot.latest_job_seen_at),
        "providers": [
            provider.as_dict(now=checked_at, max_age=max_age)
            for provider in snapshot.providers
        ],
        "failures": failures,
    }


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _isoformat(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized is not None else None
