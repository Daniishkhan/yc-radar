from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, case, func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine

from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    jobs_table,
    sync_runs_table,
)

DEFAULT_OBSERVATION_MAX_AGE_DAYS = 45


class JobRepository:
    """Persistence boundary for company sources, their current jobs, and sync runs."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def register_source(
        self,
        *,
        company_id: int,
        provider: str,
        source_kind: str,
        external_id: str,
        source_url: str | None,
        sync_mode: str,
        now: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool, bool]:
        """Register one provider identity without moving it between companies.

        The booleans are ``allowed`` and ``created``. A provider identity already attached to
        another company is returned unchanged with ``allowed=False`` so callers can fail closed.
        """
        normalized_provider = provider.strip().lower()
        normalized_external_id = external_id.strip()
        if not normalized_provider or not normalized_external_id:
            raise ValueError("provider and external_id are required")
        if sync_mode not in {"none", "complete_snapshot", "observation"}:
            raise ValueError(f"unsupported sync mode: {sync_mode}")

        with self.engine.begin() as connection:
            existing = self.get_source_by_external(
                connection,
                provider=normalized_provider,
                external_id=normalized_external_id,
            )
            if existing is not None and int(existing["company_id"]) != company_id:
                return existing, False, False
            if existing is not None:
                merged_metadata = dict(existing.get("metadata") or {})
                merged_metadata.update(metadata or {})
                connection.execute(
                    update(company_sources_table)
                    .where(company_sources_table.c.id == existing["id"])
                    .values(
                        source_kind=source_kind,
                        source_url=source_url,
                        sync_mode=sync_mode,
                        metadata=merged_metadata,
                        updated_at=now,
                    )
                )
                return self.get_source(connection, int(existing["id"])), True, False

            source_id = connection.execute(
                insert(company_sources_table)
                .values(
                    company_id=company_id,
                    provider=normalized_provider,
                    source_kind=source_kind,
                    external_id=normalized_external_id,
                    source_url=source_url,
                    sync_mode=sync_mode,
                    status="active",
                    metadata=metadata or {},
                    created_at=now,
                    updated_at=now,
                )
                .returning(company_sources_table.c.id)
            ).scalar_one()
            return self.get_source(connection, int(source_id)), True, True

    def get_source(self, connection: Connection, source_id: int) -> dict[str, Any]:
        row = (
            connection.execute(
                select(company_sources_table).where(company_sources_table.c.id == source_id)
            )
            .mappings()
            .one()
        )
        return dict(row)

    def get_source_by_external(
        self,
        connection: Connection,
        *,
        provider: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        row = (
            connection.execute(
                select(company_sources_table).where(
                    and_(
                        company_sources_table.c.provider == provider,
                        company_sources_table.c.external_id == external_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def active_sources(
        self,
        *,
        provider: str | None = None,
        company_id: int | None = None,
        source_ids: Iterable[int] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        statement: Select[Any] = select(company_sources_table).where(
            company_sources_table.c.status == "active",
            company_sources_table.c.sync_mode == "complete_snapshot",
        )
        if provider:
            statement = statement.where(company_sources_table.c.provider == provider)
        if company_id is not None:
            statement = statement.where(company_sources_table.c.company_id == company_id)
        if source_ids is not None:
            selected_ids = tuple(source_ids)
            if not selected_ids:
                return []
            statement = statement.where(company_sources_table.c.id.in_(selected_ids))
        statement = statement.order_by(company_sources_table.c.id)
        if limit is not None:
            statement = statement.limit(limit)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    def get_run(
        self,
        connection: Connection,
        company_source_id: int,
        run_key: str,
    ) -> dict[str, Any] | None:
        row = (
            connection.execute(
                select(sync_runs_table).where(
                    sync_runs_table.c.company_source_id == company_source_id,
                    sync_runs_table.c.run_key == run_key,
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def create_run(self, connection: Connection, values: dict[str, Any]) -> int:
        return int(
            connection.execute(
                insert(sync_runs_table).values(values).returning(sync_runs_table.c.id)
            ).scalar_one()
        )

    def get_run_by_id(self, connection: Connection, run_id: int) -> dict[str, Any] | None:
        row = (
            connection.execute(select(sync_runs_table).where(sync_runs_table.c.id == run_id))
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def finalize_run(
        self,
        connection: Connection,
        run_id: int,
        values: dict[str, Any],
    ) -> None:
        connection.execute(
            update(sync_runs_table).where(sync_runs_table.c.id == run_id).values(values)
        )

    def touch_source(self, connection: Connection, source_id: int, *, now: datetime) -> None:
        connection.execute(
            update(company_sources_table)
            .where(company_sources_table.c.id == source_id)
            .values(updated_at=now)
        )

    def source_jobs_for_update(
        self,
        connection: Connection,
        company_source_id: int,
    ) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            select(jobs_table)
            .where(jobs_table.c.company_source_id == company_source_id)
            .with_for_update()
        ).mappings()
        return {str(row["external_job_id"]): dict(row) for row in rows}

    def insert_job(self, connection: Connection, values: dict[str, Any]) -> int:
        return int(
            connection.execute(
                insert(jobs_table).values(values).returning(jobs_table.c.id)
            ).scalar_one()
        )

    def update_job(self, connection: Connection, job_id: int, values: dict[str, Any]) -> None:
        connection.execute(update(jobs_table).where(jobs_table.c.id == job_id).values(values))

    def list_jobs(
        self,
        *,
        include_closed: bool = False,
        changed_since: datetime | None = None,
        provider: str | None = None,
        company_slug: str | None = None,
        source_kind: str | None = None,
        origin_kind: str | None = None,
        observation_max_age_days: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return the sole, source-neutral inventory used by ranking and exports.

        When ``observation_max_age_days`` is set, only observation-mode rows are subject to
        freshness filtering. Complete snapshots remain authoritative regardless of ``last_seen_at``.
        """
        if observation_max_age_days is not None and observation_max_age_days < 0:
            raise ValueError("observation_max_age_days must be non-negative or None")
        latest_status = (
            select(sync_runs_table.c.status)
            .where(sync_runs_table.c.company_source_id == company_sources_table.c.id)
            .order_by(sync_runs_table.c.started_at.desc(), sync_runs_table.c.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        latest_completed_at = (
            select(sync_runs_table.c.completed_at)
            .where(sync_runs_table.c.company_source_id == company_sources_table.c.id)
            .order_by(sync_runs_table.c.started_at.desc(), sync_runs_table.c.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        origin_expression = case(
            (company_sources_table.c.provider == "yc", "yc"),
            (company_sources_table.c.source_kind == "ats_board", "ats"),
            else_=company_sources_table.c.source_kind,
        )
        lifecycle_managed = company_sources_table.c.sync_mode == "complete_snapshot"
        statement = (
            select(
                (
                    literal("source:")
                    + company_sources_table.c.provider
                    + literal(":")
                    + company_sources_table.c.external_id
                    + literal(":")
                    + jobs_table.c.external_job_id
                ).label("job_key"),
                jobs_table.c.id.label("id"),
                company_sources_table.c.source_kind.label("source_kind"),
                origin_expression.label("origin_kind"),
                jobs_table.c.id.label("source_record_id"),
                companies_table.c.id.label("company_id"),
                companies_table.c.name.label("company_name"),
                companies_table.c.slug.label("company_slug"),
                company_sources_table.c.provider.label("provider"),
                jobs_table.c.external_job_id,
                company_sources_table.c.id.label("company_source_id"),
                company_sources_table.c.external_id.label("source_external_id"),
                company_sources_table.c.source_url.label("source_url"),
                (company_sources_table.c.status == "active").label("source_enabled"),
                latest_status.label("source_sync_status"),
                latest_completed_at.label("source_last_synced_at"),
                jobs_table.c.title,
                jobs_table.c.posting_url,
                jobs_table.c.apply_url,
                jobs_table.c.location,
                jobs_table.c.department,
                jobs_table.c.employment_type,
                jobs_table.c.description_text,
                jobs_table.c.structured_evidence,
                jobs_table.c.content_hash,
                literal(None).label("visa"),
                literal(None).label("salary_range"),
                literal(None).label("equity_range"),
                literal([], type_=JSONB).label("skills"),
                jobs_table.c.status,
                (jobs_table.c.status == "active").label("is_active"),
                lifecycle_managed.label("lifecycle_managed"),
                case(
                    (lifecycle_managed, "complete_snapshot"),
                    else_="observation",
                ).label("status_confidence"),
                jobs_table.c.consecutive_complete_misses,
                jobs_table.c.source_published_at,
                jobs_table.c.source_updated_at,
                jobs_table.c.last_seen_at.label("observed_at"),
                jobs_table.c.first_seen_at,
                jobs_table.c.last_seen_at,
                jobs_table.c.last_changed_at,
                jobs_table.c.closed_at,
            )
            .join(
                company_sources_table,
                company_sources_table.c.id == jobs_table.c.company_source_id,
            )
            .join(
                companies_table,
                companies_table.c.id == company_sources_table.c.company_id,
            )
            .where(company_sources_table.c.status == "active")
            .order_by(jobs_table.c.last_changed_at.desc(), jobs_table.c.id)
        )
        if not include_closed:
            statement = statement.where(jobs_table.c.status == "active")
        if changed_since is not None:
            statement = statement.where(jobs_table.c.last_changed_at >= changed_since)
        if provider:
            statement = statement.where(company_sources_table.c.provider == provider)
        if company_slug:
            statement = statement.where(companies_table.c.slug == company_slug.strip().lower())
        if source_kind:
            statement = statement.where(company_sources_table.c.source_kind == source_kind)
        if origin_kind:
            statement = statement.where(origin_expression == origin_kind)
        if observation_max_age_days is not None:
            observation_cutoff = func.now() - timedelta(days=observation_max_age_days)
            statement = statement.where(
                or_(
                    company_sources_table.c.sync_mode != "observation",
                    jobs_table.c.last_seen_at >= observation_cutoff,
                )
            )
        if limit is not None:
            statement = statement.limit(limit)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]
