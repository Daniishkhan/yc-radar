from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_legacy_database.py"
SPEC = importlib.util.spec_from_file_location("migrate_legacy_database", SCRIPT_PATH)
assert SPEC and SPEC.loader
migrate_legacy_database = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migrate_legacy_database)


def test_identifier_validation_rejects_unsafe_database_names() -> None:
    for value in ("", "YC-Radar", "yc radar", "yc_radar;drop", "9radar"):
        with pytest.raises(ValueError):
            migrate_legacy_database._validate_identifier(value, label="database")


def test_cli_requires_explicit_source_outage_acknowledgement(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_legacy_database.py",
            "--target-database",
            "yc_radar_next",
            "--yes",
        ],
    )
    args = migrate_legacy_database.parse_args()
    assert args.yes is True
    assert args.allow_source_outage is False

    source_url = make_url("postgresql+psycopg://radar:test@localhost/yc_radar")
    with pytest.raises(ValueError, match="allow_source_outage=True"):
        migrate_legacy_database.clone_and_migrate(
            source_url=source_url,
            target_database="yc_radar_next",
        )

    for reserved_schema in ("public", "ingest"):
        with pytest.raises(ValueError, match="reserved public or ingest"):
            migrate_legacy_database.clone_and_migrate(
                source_url=source_url,
                target_database="yc_radar_next",
                legacy_schema=reserved_schema,
                allow_source_outage=True,
            )


def test_integrity_validation_reports_every_unsafe_collapse() -> None:
    with pytest.raises(RuntimeError, match="job_source_company_mismatches=2") as exc_info:
        migrate_legacy_database._raise_for_legacy_integrity_violations(
            {
                "job_source_provider_mismatches": 1,
                "job_source_company_mismatches": 2,
            }
        )
    assert "job_source_provider_mismatches=1" in str(exc_info.value)


def test_integrity_sql_detects_legacy_job_ownership_drift(
    postgres_database_url: str,
) -> None:
    engine = create_engine(postgres_database_url, future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE SCHEMA legacy_integrity")
        connection.exec_driver_sql(
            "CREATE TABLE legacy_integrity.companies (id bigint PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE legacy_integrity.company_sources (
                id bigint PRIMARY KEY,
                company_id bigint NOT NULL,
                provider text NOT NULL,
                external_company_id text NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE legacy_integrity.career_sources (
                id bigint PRIMARY KEY,
                company_id bigint NOT NULL,
                provider text NOT NULL,
                external_source_id text NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE legacy_integrity.source_sync_runs (
                id bigint PRIMARY KEY,
                career_source_id bigint NOT NULL,
                provider text NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE legacy_integrity.job_postings (
                id bigint PRIMARY KEY,
                career_source_id bigint NOT NULL,
                company_id bigint NOT NULL,
                provider text NOT NULL,
                current_version_id bigint
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE legacy_integrity.job_posting_versions (
                id bigint PRIMARY KEY,
                job_posting_id bigint NOT NULL,
                source_sync_run_id bigint NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE legacy_integrity.job_posting_observations (
                id bigint PRIMARY KEY,
                job_posting_id bigint NOT NULL,
                source_sync_run_id bigint NOT NULL,
                observation_kind text NOT NULL,
                observed_at timestamptz NOT NULL
            )
            """
        )
        connection.execute(text("INSERT INTO legacy_integrity.companies VALUES (1)"))
        connection.execute(
            text(
                "INSERT INTO legacy_integrity.company_sources "
                "VALUES (1, 1, 'yc', '1')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO legacy_integrity.career_sources "
                "VALUES (1, 1, 'greenhouse', 'board')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO legacy_integrity.source_sync_runs "
                "VALUES (1, 1, 'greenhouse')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO legacy_integrity.job_postings "
                "VALUES (1, 1, 1, 'greenhouse', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO legacy_integrity.job_posting_versions "
                "VALUES (1, 1, 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO legacy_integrity.job_posting_observations "
                "VALUES (1, 1, 1, 'seen', now())"
            )
        )

    migrate_legacy_database._validate_legacy_integrity(
        engine,
        schema="legacy_integrity",
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE legacy_integrity.job_postings SET company_id = 2 WHERE id = 1")
        )
    with pytest.raises(RuntimeError, match="job_source_company_mismatches=1"):
        migrate_legacy_database._validate_legacy_integrity(
            engine,
            schema="legacy_integrity",
        )
    engine.dispose()


def test_copy_sql_uses_latest_seen_observation_not_current_content_version() -> None:
    source = inspect.getsource(migrate_legacy_database._copy_legacy_data)
    assert "LEFT JOIN LATERAL" in source
    assert "observation.observation_kind = 'seen'" in source
    assert "ORDER BY observation.observed_at DESC, observation.id DESC" in source
    assert "latest_seen.source_sync_run_id" in source
    assert "version.source_sync_run_id," not in source


def test_failed_migration_drops_only_the_new_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url = make_url("postgresql+psycopg://radar:test@localhost/yc_radar")
    events: list[tuple[str, str]] = []

    class FakeEngine:
        def dispose(self) -> None:
            events.append(("dispose", "target"))

    monkeypatch.setattr(migrate_legacy_database, "_clone_database", lambda *args, **kwargs: None)
    monkeypatch.setattr(migrate_legacy_database, "_engine", lambda *args, **kwargs: FakeEngine())
    monkeypatch.setattr(
        migrate_legacy_database,
        "_revision",
        lambda *args, **kwargs: migrate_legacy_database.LEGACY_REVISION,
    )
    monkeypatch.setattr(
        migrate_legacy_database,
        "_legacy_counts",
        lambda *args, **kwargs: {
            "companies": 0,
            "directory_sources": 0,
            "ats_sources": 0,
            "sync_runs": 0,
            "jobs": 0,
            "active_jobs": 0,
            "closed_jobs": 0,
        },
    )
    monkeypatch.setattr(
        migrate_legacy_database,
        "_validate_legacy_integrity",
        lambda *args, **kwargs: None,
    )

    def fail_prepare(*args, **kwargs) -> None:
        raise RuntimeError("synthetic migration failure")

    def record_drop(*args, target_database: str, **kwargs) -> None:
        events.append(("drop", target_database))

    monkeypatch.setattr(migrate_legacy_database, "_prepare_cloned_schema", fail_prepare)
    monkeypatch.setattr(migrate_legacy_database, "_drop_database", record_drop)

    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        migrate_legacy_database.clone_and_migrate(
            source_url=source_url,
            target_database="yc_radar_next",
            allow_source_outage=True,
        )

    assert events == [("dispose", "target"), ("drop", "yc_radar_next")]


def test_existing_target_clone_failure_is_never_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url = make_url("postgresql+psycopg://radar:test@localhost/yc_radar")
    dropped: list[str] = []

    def fail_clone(*args, **kwargs) -> None:
        raise RuntimeError("target database already exists")

    monkeypatch.setattr(migrate_legacy_database, "_clone_database", fail_clone)
    monkeypatch.setattr(
        migrate_legacy_database,
        "_drop_database",
        lambda *args, target_database, **kwargs: dropped.append(target_database),
    )

    with pytest.raises(RuntimeError, match="already exists"):
        migrate_legacy_database.clone_and_migrate(
            source_url=source_url,
            target_database="yc_radar_next",
            allow_source_outage=True,
        )
    assert dropped == []


def test_copy_count_validation_runs_before_transaction_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def __init__(self, owner) -> None:
            self.owner = owner

        def scalar(self, statement, parameters=None):
            assert self.owner.active is True
            return 0

        def execute(self, statement, parameters=None) -> None:
            assert self.owner.active is True

        def exec_driver_sql(self, statement) -> None:
            assert self.owner.active is True

    class FakeTransaction:
        def __init__(self) -> None:
            self.active = False
            self.exit_error = None
            self.connection = FakeConnection(self)

        def __enter__(self):
            self.active = True
            return self.connection

        def __exit__(self, error_type, error, traceback) -> None:
            self.exit_error = error
            self.active = False

    class FakeEngine:
        def __init__(self) -> None:
            self.transaction = FakeTransaction()

        def begin(self):
            return self.transaction

    engine = FakeEngine()
    monkeypatch.setattr(
        migrate_legacy_database,
        "_quote_identifier",
        lambda *args, **kwargs: "legacy_v1",
    )
    source_counts = {
        "companies": 0,
        "directory_sources": 0,
        "ats_sources": 0,
        "sync_runs": 0,
        "jobs": 1,
        "active_jobs": 1,
        "closed_jobs": 0,
    }

    with pytest.raises(RuntimeError, match="count mismatch"):
        migrate_legacy_database._copy_legacy_data(
            engine,
            legacy_schema="legacy_v1",
            source_counts=source_counts,
        )

    assert isinstance(engine.transaction.exit_error, RuntimeError)
    assert engine.transaction.active is False


def test_count_validation_requires_every_canonical_row() -> None:
    source = {
        "companies": 3,
        "directory_sources": 3,
        "ats_sources": 2,
        "sync_runs": 7,
        "jobs": 11,
        "active_jobs": 9,
        "closed_jobs": 2,
    }
    expected = {
        "companies": 3,
        "sources": 5,
        "sync_runs": 7,
        "jobs": 11,
        "active_jobs": 9,
        "closed_jobs": 2,
    }

    migrate_legacy_database._validate_counts(
        source_counts=source,
        target_counts=expected,
    )
    with pytest.raises(RuntimeError, match="count mismatch"):
        migrate_legacy_database._validate_counts(
            source_counts=source,
            target_counts={**expected, "jobs": 10},
        )
