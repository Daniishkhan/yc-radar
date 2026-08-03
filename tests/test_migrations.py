from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from yc_radar.services.database import engine_from_url
from yc_radar.services.migrations import alembic_config, rebuild_database


CORE_TABLES = {"companies", "company_sources", "jobs", "sync_runs"}
INGEST_TABLES = {"runs", "raw_observations", "url_work_items", "job_candidates"}
INGEST_COLUMNS = {
    "runs": {
        "id",
        "run_key",
        "source",
        "status",
        "parser_version",
        "normalizer_version",
        "input_uri",
        "input_sha256",
        "cursor",
        "stats",
        "started_at",
        "completed_at",
    },
    "raw_observations": {
        "id",
        "run_id",
        "url_work_item_id",
        "observation_key",
        "observed_url",
        "payload",
        "observed_at",
    },
    "url_work_items": {
        "id",
        "run_id",
        "normalized_url",
        "host",
        "stage",
        "state",
        "priority",
        "attempt_count",
        "max_attempts",
        "available_at",
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "artifact_uri",
        "http_status",
        "content_type",
        "content_hash",
        "result",
        "last_error",
        "parser_version",
        "normalizer_version",
        "created_at",
        "updated_at",
    },
    "job_candidates": {
        "id",
        "run_id",
        "raw_observation_id",
        "work_item_id",
        "candidate_key",
        "company_source_id",
        "provider",
        "external_source_id",
        "external_job_id",
        "snapshot_complete",
        "title",
        "posting_url",
        "apply_url",
        "description_text",
        "location",
        "department",
        "employment_type",
        "content_hash",
        "source_published_at",
        "source_updated_at",
        "field_provenance",
        "quality_flags",
        "payload",
        "status",
        "parser_version",
        "normalizer_version",
        "error",
        "promoted_job_id",
        "created_at",
        "updated_at",
    },
}


def test_fresh_database_contains_core_and_logged_ingest_staging(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    inspector = inspect(engine)
    with engine.connect() as connection:
        schema = str(connection.dialect.default_schema_name)
        persistence = dict(
            connection.execute(
                text(
                    "SELECT class.relname, class.relpersistence "
                    "FROM pg_class AS class "
                    "JOIN pg_namespace AS namespace ON namespace.oid = class.relnamespace "
                    "WHERE namespace.nspname = 'ingest' AND class.relkind = 'r'"
                )
            ).all()
        )

    assert set(inspector.get_table_names(schema=schema)) == CORE_TABLES | {"alembic_version"}
    assert set(inspector.get_table_names(schema="ingest")) == INGEST_TABLES
    assert inspector.get_view_names(schema=schema) == []
    assert persistence == {table_name: "p" for table_name in INGEST_TABLES}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0002_ingest_staging"
        )


def test_core_schema_derives_job_company_through_company_source(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    inspector = inspect(engine)
    with engine.connect() as connection:
        schema = str(connection.dialect.default_schema_name)

    company_columns = {
        column["name"] for column in inspector.get_columns("companies", schema=schema)
    }
    source_columns = {
        column["name"] for column in inspector.get_columns("company_sources", schema=schema)
    }
    job_columns = {column["name"] for column in inspector.get_columns("jobs", schema=schema)}

    assert {"name", "slug", "website", "primary_domain", "identity_state"} <= company_columns
    assert not {"yc_url", "batch", "stage", "team_size", "is_hiring"} & company_columns
    assert {
        "company_id",
        "provider",
        "source_kind",
        "external_id",
        "sync_mode",
        "metadata",
    } <= source_columns
    assert {
        "company_source_id",
        "external_job_id",
        "structured_evidence",
        "raw_payload",
        "last_seen_run_id",
    } <= job_columns
    assert "company_id" not in job_columns


def test_core_foreign_keys_and_uniqueness_are_explicit(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    inspector = inspect(engine)
    with engine.connect() as connection:
        schema = str(connection.dialect.default_schema_name)

    source_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("company_sources", schema=schema)
    }
    job_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("jobs", schema=schema)
    }
    job_foreign_keys = {
        tuple(foreign_key["constrained_columns"]): tuple(foreign_key["referred_columns"])
        for foreign_key in inspector.get_foreign_keys("jobs", schema=schema)
    }
    job_indexes = {
        index["name"] for index in inspector.get_indexes("jobs", schema=schema)
    }

    assert ("provider", "external_id") in source_uniques
    assert ("company_source_id", "external_job_id") in job_uniques
    assert ("company_id",) not in job_foreign_keys
    assert job_foreign_keys[("company_source_id",)] == ("id",)
    assert job_foreign_keys[("last_seen_run_id",)] == ("id",)
    assert "ix_jobs_status" in job_indexes
    assert "ix_jobs_company_status" not in job_indexes


def test_ingest_staging_contract_is_explicit_and_bounded(
    postgres_database_url: str,
) -> None:
    inspector = inspect(engine_from_url(postgres_database_url))

    for table_name, expected_columns in INGEST_COLUMNS.items():
        actual_columns = {
            column["name"]
            for column in inspector.get_columns(table_name, schema="ingest")
        }
        assert actual_columns == expected_columns

    run_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("runs", schema="ingest")
    }
    raw_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("raw_observations", schema="ingest")
    }
    url_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("url_work_items", schema="ingest")
    }
    candidate_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("job_candidates", schema="ingest")
    }
    assert ("source", "run_key") in run_uniques
    assert ("run_id", "observation_key") in raw_uniques
    assert ("normalized_url", "parser_version", "normalizer_version") in url_uniques
    assert ("run_id", "candidate_key") in candidate_uniques

    url_indexes = {
        index["name"] for index in inspector.get_indexes("url_work_items", schema="ingest")
    }
    candidate_indexes = {
        index["name"] for index in inspector.get_indexes("job_candidates", schema="ingest")
    }
    assert {"ix_ingest_url_work_items_queue", "ix_ingest_url_work_items_lease"} <= url_indexes
    assert "ix_ingest_job_candidates_ready" in candidate_indexes

    check_names = {
        table_name: {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name, schema="ingest")
        }
        for table_name in INGEST_TABLES
    }
    assert {"ck_ingest_runs_cursor", "ck_ingest_runs_stats"} <= check_names["runs"]
    assert "ck_ingest_raw_observations_payload" in check_names["raw_observations"]
    assert {
        "ck_ingest_url_work_items_stage",
        "ck_ingest_url_work_items_state",
        "ck_ingest_url_work_items_lease",
        "ck_ingest_url_work_items_result",
        "ck_ingest_url_work_items_last_error",
    } <= check_names["url_work_items"]
    assert {
        "ck_ingest_job_candidates_status",
        "ck_ingest_job_candidates_lineage",
        "ck_ingest_job_candidates_ready_fields",
        "ck_ingest_job_candidates_promoted_source",
        "ck_ingest_job_candidates_field_provenance",
        "ck_ingest_job_candidates_quality_flags",
        "ck_ingest_job_candidates_payload",
        "ck_ingest_job_candidates_error",
    } <= check_names["job_candidates"]

    candidate_columns = {
        column["name"]: column
        for column in inspector.get_columns("job_candidates", schema="ingest")
    }
    candidate_foreign_keys = {
        tuple(foreign_key["constrained_columns"]): foreign_key
        for foreign_key in inspector.get_foreign_keys("job_candidates", schema="ingest")
    }
    assert candidate_columns["raw_observation_id"]["nullable"] is False
    assert candidate_foreign_keys[("raw_observation_id",)]["options"].get("ondelete") is None


def test_ingest_upgrade_refuses_inconsistent_job_company_ownership(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    config = alembic_config(postgres_database_url)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0001_core")

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO companies (
                    id, name, normalized_name, slug, identity_state, metadata,
                    created_at, updated_at
                ) VALUES
                    (1, 'Source Owner', 'source owner', 'source-owner', 'verified', '{}',
                     now(), now()),
                    (2, 'Drifted Owner', 'drifted owner', 'drifted-owner', 'verified', '{}',
                     now(), now());
                INSERT INTO company_sources (
                    id, company_id, provider, source_kind, external_id, sync_mode, status,
                    metadata, created_at, updated_at
                ) VALUES (
                    1, 1, 'greenhouse', 'ats_board', 'drifted-board',
                    'complete_snapshot', 'active', '{}', now(), now()
                );
                INSERT INTO jobs (
                    company_id, company_source_id, external_job_id, title,
                    structured_evidence, raw_payload, status, consecutive_complete_misses,
                    content_hash, first_seen_at, last_seen_at, last_changed_at,
                    created_at, updated_at
                ) VALUES (
                    2, 1, 'job-1', 'Backend Engineer', '{}', '{}', 'active', 0,
                    'hash', now(), now(), now(), now(), now()
                );
                """
            )
        )

    config = alembic_config(postgres_database_url)
    with pytest.raises(IntegrityError, match="refusing to remove redundant job ownership"):
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    assert "company_id" in {column["name"] for column in inspect(engine).get_columns("jobs")}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_core"


def test_raw_observation_cannot_be_deleted_while_candidate_references_it(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    with engine.begin() as connection:
        run_id = int(
            connection.scalar(
                text(
                    "INSERT INTO ingest.runs "
                    "(run_key, source, status, parser_version, normalizer_version, started_at) "
                    "VALUES ('lineage', 'test', 'running', 'p1', 'n1', now()) RETURNING id"
                )
            )
        )
        raw_id = int(
            connection.scalar(
                text(
                    "INSERT INTO ingest.raw_observations "
                    "(run_id, observation_key, payload, observed_at) "
                    "VALUES (:run_id, 'row-1', '{}', now()) RETURNING id"
                ),
                {"run_id": run_id},
            )
        )
        connection.execute(
            text(
                "INSERT INTO ingest.job_candidates "
                "(run_id, raw_observation_id, candidate_key, snapshot_complete, status, "
                " parser_version, normalizer_version, created_at, updated_at) "
                "VALUES (:run_id, :raw_id, 'candidate-1', false, 'normalized', "
                " 'p1', 'n1', now(), now())"
            ),
            {"run_id": run_id, "raw_id": raw_id},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM ingest.raw_observations WHERE id = :raw_id"),
                {"raw_id": raw_id},
            )

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM ingest.job_candidates")) == 1

    # Deleting the owning run still removes the whole lineage in one statement;
    # NO ACTION blocks only attempts to orphan a surviving candidate.
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM ingest.runs WHERE id = :run_id"), {"run_id": run_id})
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM ingest.raw_observations")) == 0
        assert connection.scalar(text("SELECT count(*) FROM ingest.job_candidates")) == 0


def test_rebuild_drops_ingest_state_and_reapplies_head(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ingest.runs "
                "(run_key, source, status, parser_version, normalizer_version, started_at) "
                "VALUES ('one', 'test', 'running', 'p1', 'n1', now())"
            )
        )

    rebuild_database(engine)
    rebuild_database(engine)

    inspector = inspect(engine)
    assert set(inspector.get_table_names(schema="ingest")) == INGEST_TABLES
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM ingest.runs")) == 0
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0002_ingest_staging"
        )


def test_alembic_check_ignores_unowned_tables_and_schemas(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE public.unowned_probe (id bigint PRIMARY KEY)"))
        connection.execute(text("CREATE SCHEMA unowned"))
        connection.execute(text("CREATE TABLE unowned.probe (id bigint PRIMARY KEY)"))
        config = alembic_config(postgres_database_url)
        config.attributes["connection"] = connection
        command.check(config)
