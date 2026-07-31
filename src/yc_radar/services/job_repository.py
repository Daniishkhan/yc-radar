from __future__ import annotations

from datetime import datetime
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Select, and_, insert, select, update
from sqlalchemy.engine import Connection, Engine

from yc_radar.services.database import (
    career_sources_table,
    companies_table,
    job_posting_observations_table,
    job_posting_versions_table,
    job_postings_table,
    source_sync_runs_table,
)


class JobRepository:
    """SQLAlchemy persistence boundary for source-neutral jobs and their audit records."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def register_career_source(
        self,
        *,
        company_id: int,
        provider: str,
        source_kind: str,
        external_source_id: str,
        source_url: str,
        discovered_from_url: str | None,
        now: datetime,
        raw_json: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool, bool]:
        """Register one provider board without coupling it to a company-directory source."""
        with self.engine.begin() as connection:
            existing = self.get_career_source_by_external(connection, provider, external_source_id)
            if existing and int(existing["company_id"]) != company_id:
                return existing, False, False
            if existing:
                connection.execute(
                    update(career_sources_table)
                    .where(career_sources_table.c.id == existing["id"])
                    .values(
                        source_url=source_url,
                        discovered_from_url=discovered_from_url,
                        raw_json=raw_json or existing.get("raw_json") or {},
                        updated_at=now,
                    )
                )
                return self.get_career_source(connection, int(existing["id"])), True, False
            source_id = connection.execute(
                insert(career_sources_table)
                .values(
                    company_id=company_id,
                    provider=provider,
                    source_kind=source_kind,
                    external_source_id=external_source_id,
                    source_url=source_url,
                    discovered_from_url=discovered_from_url,
                    status="active",
                    raw_json=raw_json or {},
                    created_at=now,
                    updated_at=now,
                )
                .returning(career_sources_table.c.id)
            ).scalar_one()
            return self.get_career_source(connection, int(source_id)), True, True

    def get_career_source(self, connection: Connection, source_id: int) -> dict[str, Any]:
        row = connection.execute(
            select(career_sources_table).where(career_sources_table.c.id == source_id)
        ).mappings().one()
        return dict(row)

    def get_career_source_by_external(
        self,
        connection: Connection,
        provider: str,
        external_source_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            select(career_sources_table).where(
                and_(
                    career_sources_table.c.provider == provider,
                    career_sources_table.c.external_source_id == external_source_id,
                )
            )
        ).mappings().first()
        return dict(row) if row else None

    def active_career_sources(
        self,
        *,
        provider: str | None = None,
        company_id: int | None = None,
        source_ids: Iterable[int] | None = None,
        min_source_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        statement: Select[Any] = select(career_sources_table).where(
            career_sources_table.c.status == "active"
        )
        if provider:
            statement = statement.where(career_sources_table.c.provider == provider)
        if company_id is not None:
            statement = statement.where(career_sources_table.c.company_id == company_id)
        if source_ids is not None:
            selected_ids = tuple(source_ids)
            if not selected_ids:
                return []
            statement = statement.where(career_sources_table.c.id.in_(selected_ids))
        if min_source_id is not None:
            statement = statement.where(career_sources_table.c.id >= min_source_id)
        statement = statement.order_by(career_sources_table.c.id)
        if limit is not None:
            statement = statement.limit(limit)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    def get_run(
        self,
        connection: Connection,
        career_source_id: int,
        run_key: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            select(source_sync_runs_table).where(
                and_(
                    source_sync_runs_table.c.career_source_id == career_source_id,
                    source_sync_runs_table.c.run_key == run_key,
                )
            )
        ).mappings().first()
        return dict(row) if row else None

    def create_run(self, connection: Connection, values: dict[str, Any]) -> int:
        return int(
            connection.execute(
                insert(source_sync_runs_table).values(values).returning(source_sync_runs_table.c.id)
            ).scalar_one()
        )

    def get_run_by_id(self, connection: Connection, run_id: int) -> dict[str, Any] | None:
        row = connection.execute(
            select(source_sync_runs_table).where(source_sync_runs_table.c.id == run_id)
        ).mappings().first()
        return dict(row) if row else None

    def finalize_run(
        self,
        connection: Connection,
        run_id: int,
        values: dict[str, Any],
    ) -> None:
        connection.execute(
            update(source_sync_runs_table)
            .where(source_sync_runs_table.c.id == run_id)
            .values(values)
        )

    def update_career_source_sync_state(
        self,
        connection: Connection,
        source_id: int,
        *,
        status: str,
        now: datetime,
    ) -> None:
        values: dict[str, Any] = {"last_sync_status": status, "updated_at": now}
        if status == "completed":
            values["last_synced_at"] = now
        connection.execute(
            update(career_sources_table)
            .where(career_sources_table.c.id == source_id)
            .values(values)
        )

    def source_jobs_for_update(
        self,
        connection: Connection,
        career_source_id: int,
    ) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            select(job_postings_table)
            .where(job_postings_table.c.career_source_id == career_source_id)
            .with_for_update()
        ).mappings()
        return {str(row["external_job_id"]): dict(row) for row in rows}

    def insert_job(self, connection: Connection, values: dict[str, Any]) -> int:
        return int(
            connection.execute(insert(job_postings_table).values(values).returning(job_postings_table.c.id)).scalar_one()
        )

    def update_job(self, connection: Connection, job_id: int, values: dict[str, Any]) -> None:
        connection.execute(update(job_postings_table).where(job_postings_table.c.id == job_id).values(values))

    def insert_version(self, connection: Connection, values: dict[str, Any]) -> int:
        return int(
            connection.execute(
                insert(job_posting_versions_table)
                .values(values)
                .returning(job_posting_versions_table.c.id)
            ).scalar_one()
        )

    def insert_observation(self, connection: Connection, values: dict[str, Any]) -> None:
        connection.execute(insert(job_posting_observations_table).values(values))

    def active_job_rows(
        self,
        *,
        include_closed: bool = False,
        changed_since: datetime | None = None,
        provider: str | None = None,
        company_slug: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        statement = (
            select(
                job_postings_table,
                companies_table.c.slug.label("company_slug"),
                companies_table.c.name.label("company_name"),
                career_sources_table.c.source_kind.label("career_source_kind"),
                career_sources_table.c.source_url.label("career_source_url"),
                job_posting_versions_table.c.description_text,
                job_posting_versions_table.c.description_html,
            )
            .join(companies_table, companies_table.c.id == job_postings_table.c.company_id)
            .join(career_sources_table, career_sources_table.c.id == job_postings_table.c.career_source_id)
            .outerjoin(
                job_posting_versions_table,
                job_posting_versions_table.c.id == job_postings_table.c.current_version_id,
            )
            .order_by(job_postings_table.c.last_changed_at.desc(), job_postings_table.c.id)
        )
        if not include_closed:
            statement = statement.where(job_postings_table.c.status == "active")
        if changed_since is not None:
            statement = statement.where(job_postings_table.c.last_changed_at >= changed_since)
        if provider:
            statement = statement.where(job_postings_table.c.provider == provider)
        if company_slug:
            statement = statement.where(companies_table.c.slug == company_slug.lower())
        if limit is not None:
            statement = statement.limit(limit)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]
