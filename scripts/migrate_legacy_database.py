#!/usr/bin/env python3
"""Clone the legacy production database and migrate its canonical data side by side.

The 2026 pipeline simplification intentionally introduced a clean Alembic baseline. Production
may still be on ``0005_job_structured_evidence``. This command preserves that database, clones it
to a new database, moves the cloned legacy schema aside, creates the current schema, and copies
companies, sources, sync runs, and jobs with set-based SQL.

It never drops or rewrites source data, but the physical clone temporarily disables source
connections and terminates sessions. A target name, ``--yes``, and
``--allow-source-outage`` are mandatory.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, URL, make_url

from yc_radar.core.config import get_settings
from yc_radar.services.migrations import upgrade_database


LEGACY_REVISION = "0005_job_structured_evidence"
CURRENT_REVISION = "0002_ingest_staging"
IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SOURCE_OUTAGE_RECOVERY_TEMPLATE = (
    'ALTER DATABASE "{source_database}" WITH ALLOW_CONNECTIONS true;'
)
LEGACY_INTEGRITY_QUERIES = {
    "directory_source_orphans": """
        SELECT count(*)
        FROM {schema}.company_sources AS source
        LEFT JOIN {schema}.companies AS company ON company.id = source.company_id
        WHERE company.id IS NULL
    """,
    "career_source_orphans": """
        SELECT count(*)
        FROM {schema}.career_sources AS source
        LEFT JOIN {schema}.companies AS company ON company.id = source.company_id
        WHERE company.id IS NULL
    """,
    "run_source_orphans": """
        SELECT count(*)
        FROM {schema}.source_sync_runs AS run
        LEFT JOIN {schema}.career_sources AS source ON source.id = run.career_source_id
        WHERE source.id IS NULL
    """,
    "job_source_orphans": """
        SELECT count(*)
        FROM {schema}.job_postings AS job
        LEFT JOIN {schema}.career_sources AS source ON source.id = job.career_source_id
        WHERE source.id IS NULL
    """,
    "job_source_company_mismatches": """
        SELECT count(*)
        FROM {schema}.job_postings AS job
        JOIN {schema}.career_sources AS source ON source.id = job.career_source_id
        WHERE job.company_id IS DISTINCT FROM source.company_id
    """,
    "job_source_provider_mismatches": """
        SELECT count(*)
        FROM {schema}.job_postings AS job
        JOIN {schema}.career_sources AS source ON source.id = job.career_source_id
        WHERE job.provider IS DISTINCT FROM source.provider
    """,
    "run_source_provider_mismatches": """
        SELECT count(*)
        FROM {schema}.source_sync_runs AS run
        JOIN {schema}.career_sources AS source ON source.id = run.career_source_id
        WHERE run.provider IS DISTINCT FROM source.provider
    """,
    "current_version_mismatches": """
        SELECT count(*)
        FROM {schema}.job_postings AS job
        LEFT JOIN {schema}.job_posting_versions AS version
          ON version.id = job.current_version_id
        WHERE version.id IS NULL
           OR version.job_posting_id IS DISTINCT FROM job.id
    """,
    "current_version_run_source_mismatches": """
        SELECT count(*)
        FROM {schema}.job_postings AS job
        JOIN {schema}.job_posting_versions AS version
          ON version.id = job.current_version_id
        JOIN {schema}.source_sync_runs AS run
          ON run.id = version.source_sync_run_id
        WHERE run.career_source_id IS DISTINCT FROM job.career_source_id
    """,
    "current_version_run_orphans": """
        SELECT count(*)
        FROM {schema}.job_postings AS job
        JOIN {schema}.job_posting_versions AS version
          ON version.id = job.current_version_id
        LEFT JOIN {schema}.source_sync_runs AS run
          ON run.id = version.source_sync_run_id
        WHERE run.id IS NULL
    """,
    "seen_observation_run_source_mismatches": """
        SELECT count(*)
        FROM {schema}.job_posting_observations AS observation
        JOIN {schema}.job_postings AS job ON job.id = observation.job_posting_id
        JOIN {schema}.source_sync_runs AS run
          ON run.id = observation.source_sync_run_id
        WHERE observation.observation_kind = 'seen'
          AND run.career_source_id IS DISTINCT FROM job.career_source_id
    """,
    "seen_observation_orphans": """
        SELECT count(*)
        FROM {schema}.job_posting_observations AS observation
        LEFT JOIN {schema}.job_postings AS job ON job.id = observation.job_posting_id
        LEFT JOIN {schema}.source_sync_runs AS run
          ON run.id = observation.source_sync_run_id
        WHERE observation.observation_kind = 'seen'
          AND (job.id IS NULL OR run.id IS NULL)
    """,
    "cross_registry_identity_collisions": """
        SELECT count(*)
        FROM {schema}.company_sources AS directory_source
        JOIN {schema}.career_sources AS career_source
          ON career_source.provider = directory_source.provider
         AND career_source.external_source_id = directory_source.external_company_id
    """,
}


class DatabaseCloneError(RuntimeError):
    def __init__(self, message: str, *, target_created: bool) -> None:
        super().__init__(message)
        self.target_created = target_created


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone a legacy YC Radar database into the current canonical schema.",
        epilog=(
            "This operation temporarily disables source-database connections and terminates "
            "existing sessions. If the process is killed while the source is disabled, connect "
            "to a maintenance database and run: ALTER DATABASE \"SOURCE\" WITH "
            "ALLOW_CONNECTIONS true;"
        ),
    )
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--legacy-schema", default="legacy_v1")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument(
        "--allow-source-outage",
        action="store_true",
        help=(
            "Acknowledge that the physical clone blocks new source connections and terminates "
            "existing source sessions. Required in addition to --yes."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.yes:
        raise SystemExit("Refusing database clone without --yes")
    source_url = make_url(get_settings().database_url)
    if not args.allow_source_outage:
        recovery = SOURCE_OUTAGE_RECOVERY_TEMPLATE.format(
            source_database=str(source_url.database or "SOURCE")
        )
        raise SystemExit(
            "Refusing database clone without --allow-source-outage. The clone temporarily "
            "sets ALLOW_CONNECTIONS=false and terminates source sessions. Recovery after an "
            f"interrupted run: {recovery}"
        )
    manifest = clone_and_migrate(
        source_url=source_url,
        target_database=args.target_database,
        legacy_schema=args.legacy_schema,
        allow_source_outage=args.allow_source_outage,
    )
    output = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(output, encoding="utf-8")
        print(f"Wrote migration manifest: {args.manifest}")
    print(output, end="")


def clone_and_migrate(
    *,
    source_url: URL,
    target_database: str,
    legacy_schema: str = "legacy_v1",
    allow_source_outage: bool = False,
) -> dict[str, Any]:
    source_database = str(source_url.database or "")
    _validate_identifier(source_database, label="source database")
    _validate_identifier(target_database, label="target database")
    _validate_identifier(legacy_schema, label="legacy schema")
    if source_database == target_database:
        raise ValueError("target database must differ from the source database")
    if legacy_schema in {"public", "ingest"}:
        raise ValueError("legacy schema must not use the reserved public or ingest schema")
    if not allow_source_outage:
        recovery = SOURCE_OUTAGE_RECOVERY_TEMPLATE.format(
            source_database=source_database
        )
        raise ValueError(
            "physical database cloning requires allow_source_outage=True because it disables "
            f"source connections and terminates sessions; interruption recovery: {recovery}"
        )

    target_created = False
    target_engine: Engine | None = None
    try:
        _clone_database(source_url, target_database=target_database)
        target_created = True
        target_url = source_url.set(database=target_database)
        target_engine = _engine(target_url)

        # The clone is the only authoritative source snapshot. The live source can change before
        # its connections are disabled, so pre-clone counts cannot validate the copied rows.
        source_revision = _revision(target_engine, schema="public")
        if source_revision != LEGACY_REVISION:
            raise RuntimeError(
                f"cloned source database is at {source_revision!r}, "
                f"expected {LEGACY_REVISION!r}"
            )
        source_counts = _legacy_counts(target_engine, schema="public")
        _validate_legacy_integrity(target_engine, schema="public")

        _prepare_cloned_schema(target_engine, legacy_schema=legacy_schema)
        upgrade_database(target_engine)
        target_revision = _revision(target_engine, schema="public")
        if target_revision != CURRENT_REVISION:
            raise RuntimeError(
                f"target database is at {target_revision!r}, expected {CURRENT_REVISION!r}"
            )
        target_counts = _copy_legacy_data(
            target_engine,
            legacy_schema=legacy_schema,
            source_counts=source_counts,
        )
    except BaseException as migration_error:
        target_created = target_created or (
            isinstance(migration_error, DatabaseCloneError)
            and migration_error.target_created
        )
        if target_engine is not None:
            target_engine.dispose()
            target_engine = None
        if target_created:
            try:
                _drop_database(source_url, target_database=target_database)
            except BaseException as cleanup_error:
                raise RuntimeError(
                    f"migration failed ({migration_error!r}) and automatic cleanup of newly "
                    f"created target {target_database!r} also failed ({cleanup_error!r}); "
                    f"drop only that target manually before retrying"
                ) from migration_error
        raise
    finally:
        if target_engine is not None:
            target_engine.dispose()

    return {
        "schema_version": 1,
        "state": "completed",
        "completed_at": datetime.now(UTC).isoformat(),
        "source_database": source_database,
        "source_revision": source_revision,
        "source_counts": source_counts,
        "target_database": target_database,
        "target_revision": target_revision,
        "target_counts": target_counts,
        "preserved_legacy_schema": legacy_schema,
    }


def _clone_database(source_url: URL, *, target_database: str) -> None:
    source_database = str(source_url.database)
    maintenance_database = "postgres" if source_database != "postgres" else "template1"
    maintenance_engine = _engine(source_url.set(database=maintenance_database))
    source_identifier = _quote_identifier(maintenance_engine, source_database)
    target_identifier = _quote_identifier(maintenance_engine, target_database)
    recovery = SOURCE_OUTAGE_RECOVERY_TEMPLATE.format(
        source_database=source_database
    )
    try:
        with maintenance_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            source_allows_connections = connection.scalar(
                text("SELECT datallowconn FROM pg_database WHERE datname = :database"),
                {"database": source_database},
            )
            if source_allows_connections is None:
                raise RuntimeError(f"source database {source_database!r} does not exist")
            if source_allows_connections is not True:
                raise RuntimeError(
                    f"source database {source_database!r} already has ALLOW_CONNECTIONS=false; "
                    f"inspect it before retrying and, when safe, recover with: {recovery}"
                )
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :database"),
                {"database": target_database},
            )
            if exists is not None:
                raise RuntimeError(
                    f"target database {target_database!r} already exists; refusing to overwrite it"
                )
            source_disabled = False
            target_created = False
            clone_error: BaseException | None = None
            recovery_error: BaseException | None = None
            try:
                connection.exec_driver_sql(
                    f"ALTER DATABASE {source_identifier} WITH ALLOW_CONNECTIONS false"
                )
                source_disabled = True
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database AND pid <> pg_backend_pid()"
                    ),
                    {"database": source_database},
                )
                connection.exec_driver_sql(
                    f"CREATE DATABASE {target_identifier} WITH TEMPLATE {source_identifier}"
                )
                target_created = True
            except BaseException as error:
                clone_error = error
            finally:
                if source_disabled:
                    try:
                        connection.exec_driver_sql(
                            f"ALTER DATABASE {source_identifier} WITH ALLOW_CONNECTIONS true"
                        )
                    except BaseException as error:
                        recovery_error = error
            if recovery_error is not None:
                detail = f"; clone error was {clone_error!r}" if clone_error else ""
                raise DatabaseCloneError(
                    "failed to re-enable source database connections after clone"
                    f"{detail}; run this immediately from a maintenance database: {recovery}",
                    target_created=target_created,
                ) from recovery_error
            if clone_error is not None:
                if target_created:
                    raise DatabaseCloneError(
                        f"database clone failed after target creation: {clone_error!r}",
                        target_created=True,
                    ) from clone_error
                raise clone_error
    finally:
        maintenance_engine.dispose()


def _drop_database(source_url: URL, *, target_database: str) -> None:
    """Force-drop only a target that the current migration invocation created."""
    source_database = str(source_url.database)
    if source_database == target_database:
        raise ValueError("refusing to drop the source database")
    maintenance_database = "postgres" if source_database != "postgres" else "template1"
    maintenance_engine = _engine(source_url.set(database=maintenance_database))
    target_identifier = _quote_identifier(maintenance_engine, target_database)
    try:
        with maintenance_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(
                f"DROP DATABASE IF EXISTS {target_identifier} WITH (FORCE)"
            )
    finally:
        maintenance_engine.dispose()


def _validate_legacy_integrity(engine: Engine, *, schema: str) -> None:
    quoted_schema = _quote_identifier(engine, schema)
    with engine.connect() as connection:
        violations = {
            name: count
            for name, statement in LEGACY_INTEGRITY_QUERIES.items()
            if (
                count := int(
                    connection.scalar(
                        text(statement.format(schema=quoted_schema))
                    )
                    or 0
                )
            )
        }
    _raise_for_legacy_integrity_violations(violations)


def _raise_for_legacy_integrity_violations(violations: dict[str, int]) -> None:
    if not violations:
        return
    details = ", ".join(
        f"{name}={count}" for name, count in sorted(violations.items())
    )
    raise RuntimeError(
        "legacy integrity preflight failed; refusing to collapse ownership or provenance: "
        f"{details}"
    )


def _prepare_cloned_schema(engine: Engine, *, legacy_schema: str) -> None:
    quoted_legacy = _quote_identifier(engine, legacy_schema)
    with engine.begin() as connection:
        existing_ingest = connection.scalar(
            text("SELECT 1 FROM pg_namespace WHERE nspname = 'ingest'")
        )
        if existing_ingest is not None:
            raise RuntimeError(
                "cloned legacy database already contains the reserved ingest schema"
            )
        existing = connection.scalar(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"),
            {"schema": legacy_schema},
        )
        if existing is not None:
            raise RuntimeError(f"schema {legacy_schema!r} already exists in target")
        connection.exec_driver_sql(f"ALTER SCHEMA public RENAME TO {quoted_legacy}")
        connection.exec_driver_sql("CREATE SCHEMA public AUTHORIZATION CURRENT_USER")
        connection.exec_driver_sql("GRANT USAGE ON SCHEMA public TO PUBLIC")


def _copy_legacy_data(
    engine: Engine,
    *,
    legacy_schema: str,
    source_counts: dict[str, int],
) -> dict[str, int]:
    schema = _quote_identifier(engine, legacy_schema)
    with engine.begin() as connection:
        source_offset = int(
            connection.scalar(text(f"SELECT COALESCE(max(id), 0) FROM {schema}.company_sources"))
            or 0
        )
        connection.execute(
            text(
                f"""
                INSERT INTO public.companies (
                    id, name, normalized_name, slug, website, primary_domain,
                    identity_state, metadata, created_at, updated_at
                )
                SELECT company.id, company.name, company.normalized_name, company.slug,
                       company.website, company.primary_domain,
                       CASE
                           WHEN company.primary_domain IS NOT NULL
                                OR EXISTS (
                                    SELECT 1 FROM {schema}.company_sources AS identity
                                    WHERE identity.company_id = company.id
                                      AND identity.provider = 'yc'
                                )
                           THEN 'verified'
                           ELSE 'provisional'
                       END,
                       jsonb_build_object(
                           'migrated_from', CAST(:legacy_revision AS text)
                       ),
                       company.created_at, company.updated_at
                FROM {schema}.companies AS company
                ORDER BY company.id
                """
            ),
            {"legacy_revision": LEGACY_REVISION},
        )
        connection.execute(
            text(
                f"""
                INSERT INTO public.company_sources (
                    id, company_id, provider, source_kind, external_id, source_url,
                    sync_mode, status, metadata, created_at, updated_at
                )
                SELECT source.id, source.company_id, source.provider, 'directory',
                       source.external_company_id, source.source_url,
                       CASE WHEN source.provider = 'yc' THEN 'complete_snapshot' ELSE 'none' END,
                       'active',
                       CASE WHEN source.provider = 'yc' THEN
                           jsonb_strip_nulls(jsonb_build_object(
                               'slug', company.slug,
                               'one_liner', profile.one_liner,
                               'batch', profile.batch,
                               'status', profile.status,
                               'stage', profile.stage,
                               'team_size', profile.team_size,
                               'is_hiring', profile.is_hiring,
                               'all_locations', profile.all_locations,
                               'regions', profile.regions,
                               'industry', profile.industry,
                               'subindustry', profile.subindustry,
                               'industries', profile.industries,
                               'tags', profile.tags,
                               'prototype_score', profile.prototype_score,
                               'prototype_angle', profile.prototype_angle,
                               'raw_payload', profile.raw_json,
                               'migrated_from', CAST(:legacy_revision AS text)
                           ))
                       ELSE jsonb_build_object(
                           'legacy_raw_json', source.raw_json,
                           'migrated_from', CAST(:legacy_revision AS text)
                       ) END,
                       source.created_at, source.updated_at
                FROM {schema}.company_sources AS source
                JOIN {schema}.companies AS company ON company.id = source.company_id
                LEFT JOIN {schema}.yc_company_profiles AS profile
                  ON profile.company_id = source.company_id
                ORDER BY source.id
                """
            ),
            {"legacy_revision": LEGACY_REVISION},
        )
        connection.execute(
            text(
                f"""
                INSERT INTO public.company_sources (
                    id, company_id, provider, source_kind, external_id, source_url,
                    sync_mode, status, metadata, created_at, updated_at
                )
                SELECT :source_offset + source.id, source.company_id, source.provider,
                       source.source_kind, source.external_source_id, source.source_url,
                       'complete_snapshot', source.status,
                       jsonb_strip_nulls(jsonb_build_object(
                           'discovered_from_url', source.discovered_from_url,
                           'legacy_raw_json', source.raw_json,
                           'legacy_last_synced_at', source.last_synced_at,
                           'legacy_last_sync_status', source.last_sync_status,
                           'migrated_from', CAST(:legacy_revision AS text),
                           'legacy_career_source_id', source.id
                       )),
                       source.created_at, source.updated_at
                FROM {schema}.career_sources AS source
                ORDER BY source.id
                """
            ),
            {
                "source_offset": source_offset,
                "legacy_revision": LEGACY_REVISION,
            },
        )
        connection.execute(
            text(
                f"""
                INSERT INTO public.sync_runs (
                    id, company_source_id, run_key, status, is_complete,
                    stats, details, started_at, completed_at
                )
                SELECT run.id, :source_offset + run.career_source_id, run.run_key,
                       run.status,
                       run.status = 'completed' AND run.is_complete_scan,
                       jsonb_build_object(
                           'jobs_fetched', run.jobs_fetched,
                           'jobs_added', run.jobs_added,
                           'jobs_updated', run.jobs_updated,
                           'jobs_unchanged', run.jobs_unchanged,
                           'jobs_missed', run.jobs_missed,
                           'jobs_closed', run.jobs_closed,
                           'jobs_reactivated', run.jobs_reactivated,
                           'errors_count', run.errors_count
                       ),
                       jsonb_strip_nulls(jsonb_build_object(
                           'provider', run.provider,
                           'adapter_version', run.adapter_version,
                           'http_status', run.http_status,
                           'errors', run.errors,
                           'request_metadata', run.request_metadata,
                           'migrated_from', CAST(:legacy_revision AS text)
                       )),
                       run.started_at, run.completed_at
                FROM {schema}.source_sync_runs AS run
                ORDER BY run.id
                """
            ),
            {
                "source_offset": source_offset,
                "legacy_revision": LEGACY_REVISION,
            },
        )
        connection.execute(
            text(
                f"""
                INSERT INTO public.jobs (
                    id, company_source_id, external_job_id, title, posting_url, apply_url,
                    description_text, location, department, employment_type,
                    structured_evidence, raw_payload, status, consecutive_complete_misses,
                    content_hash, source_published_at, source_updated_at, first_seen_at,
                    last_seen_at, last_changed_at, closed_at, last_seen_run_id,
                    created_at, updated_at
                )
                SELECT job.id, :source_offset + job.career_source_id, job.external_job_id,
                       job.title, job.posting_url, job.apply_url, version.description_text,
                       job.location, job.department, job.employment_type,
                       COALESCE(job.structured_evidence, '{{}}'::jsonb),
                       COALESCE(version.raw_payload, '{{}}'::jsonb),
                       job.status, job.consecutive_complete_misses, job.content_hash,
                       job.source_published_at, job.source_updated_at, job.first_seen_at,
                       job.last_seen_at, job.last_changed_at, job.closed_at,
                       latest_seen.source_sync_run_id,
                       job.created_at, job.updated_at
                FROM {schema}.job_postings AS job
                LEFT JOIN {schema}.job_posting_versions AS version
                  ON version.id = job.current_version_id
                 AND version.job_posting_id = job.id
                LEFT JOIN LATERAL (
                    SELECT observation.source_sync_run_id
                    FROM {schema}.job_posting_observations AS observation
                    WHERE observation.job_posting_id = job.id
                      AND observation.observation_kind = 'seen'
                    ORDER BY observation.observed_at DESC, observation.id DESC
                    LIMIT 1
                ) AS latest_seen ON true
                ORDER BY job.id
                """
            ),
            {"source_offset": source_offset},
        )
        for table_name in ("companies", "company_sources", "sync_runs", "jobs"):
            connection.exec_driver_sql(
                f"""
                SELECT setval(
                    pg_get_serial_sequence('public.{table_name}', 'id'),
                    COALESCE((SELECT max(id) FROM public.{table_name}), 1),
                    EXISTS (SELECT 1 FROM public.{table_name})
                )
                """
            )
        target_counts = _canonical_counts_connection(connection)
        _validate_counts(
            source_counts=source_counts,
            target_counts=target_counts,
        )
        return target_counts


def _legacy_counts(engine: Engine, *, schema: str) -> dict[str, int]:
    quoted = _quote_identifier(engine, schema)
    queries = {
        "companies": f"SELECT count(*) FROM {quoted}.companies",
        "directory_sources": f"SELECT count(*) FROM {quoted}.company_sources",
        "ats_sources": f"SELECT count(*) FROM {quoted}.career_sources",
        "sync_runs": f"SELECT count(*) FROM {quoted}.source_sync_runs",
        "jobs": f"SELECT count(*) FROM {quoted}.job_postings",
        "active_jobs": f"SELECT count(*) FROM {quoted}.job_postings WHERE status = 'active'",
        "closed_jobs": f"SELECT count(*) FROM {quoted}.job_postings WHERE status = 'closed'",
    }
    with engine.connect() as connection:
        return {
            key: int(connection.scalar(text(statement)) or 0)
            for key, statement in queries.items()
        }


def _canonical_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return _canonical_counts_connection(connection)


def _canonical_counts_connection(connection: Connection) -> dict[str, int]:
    queries = {
        "companies": "SELECT count(*) FROM public.companies",
        "sources": "SELECT count(*) FROM public.company_sources",
        "sync_runs": "SELECT count(*) FROM public.sync_runs",
        "jobs": "SELECT count(*) FROM public.jobs",
        "active_jobs": "SELECT count(*) FROM public.jobs WHERE status = 'active'",
        "closed_jobs": "SELECT count(*) FROM public.jobs WHERE status = 'closed'",
    }
    return {
        key: int(connection.scalar(text(statement)) or 0)
        for key, statement in queries.items()
    }


def _validate_counts(
    *,
    source_counts: dict[str, int],
    target_counts: dict[str, int],
) -> None:
    expected = {
        "companies": source_counts["companies"],
        "sources": source_counts["directory_sources"] + source_counts["ats_sources"],
        "sync_runs": source_counts["sync_runs"],
        "jobs": source_counts["jobs"],
        "active_jobs": source_counts["active_jobs"],
        "closed_jobs": source_counts["closed_jobs"],
    }
    if target_counts != expected:
        raise RuntimeError(
            "legacy migration count mismatch: "
            f"expected={expected!r}, actual={target_counts!r}"
        )


def _revision(engine: Engine, *, schema: str = "public") -> str | None:
    quoted_schema = _quote_identifier(engine, schema)
    with engine.connect() as connection:
        return connection.scalar(
            text(f"SELECT version_num FROM {quoted_schema}.alembic_version")
        )


def _engine(url: URL) -> Engine:
    if not url.drivername.startswith("postgresql"):
        raise ValueError("legacy migration requires a PostgreSQL DATABASE_URL")
    return create_engine(url, future=True, pool_pre_ping=True)


def _validate_identifier(value: str, *, label: str) -> None:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must match {IDENTIFIER_PATTERN.pattern!r}")


def _quote_identifier(engine: Engine, value: str) -> str:
    _validate_identifier(value, label="database or schema identifier")
    return engine.dialect.identifier_preparer.quote_identifier(value)


if __name__ == "__main__":
    main()
