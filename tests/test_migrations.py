from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text
from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot
from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    engine_from_url,
    job_postings_table,
    upsert_yc_companies,
    yc_company_profiles_table,
)
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_source_registry import JobSourceRegistry
from yc_radar.services.job_sync_service import JobSyncService
from yc_radar.services.migrations import alembic_config, rebuild_database, verify_existing_baseline


def test_alembic_head_contains_legacy_and_source_neutral_schema(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    assert {
        "companies",
        "yc_job_postings",
        "company_sources",
        "yc_company_profiles",
        "career_sources",
        "source_sync_runs",
        "job_postings",
        "job_posting_versions",
        "job_posting_observations",
    }.issubset(tables)
    assert "company_primary_career_pages" in inspector.get_view_names()
    discovery_columns = {
        column["name"] for column in inspector.get_columns("career_page_discovery_events")
    }
    career_page_columns = {
        column["name"] for column in inspector.get_columns("company_career_pages")
    }
    assert "yc_is_hiring" not in discovery_columns
    assert "yc_job_count" not in discovery_columns
    assert "yc_is_hiring" not in career_page_columns
    assert "yc_job_count" not in career_page_columns
    assert {index["name"] for index in inspector.get_indexes("job_postings")} >= {
        "ix_job_postings_company_active",
        "ix_job_postings_content_hash",
    }
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "0004_source_registries"


def test_baseline_verifier_rejects_an_unversioned_neutral_schema(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))

    diagnostics = verify_existing_baseline(engine)

    assert "schema contains yc_company_profiles and is not the known unversioned 0001 baseline" in diagnostics
    assert "unexpected table: company_sources" in diagnostics
    assert "unexpected table: job_postings" in diagnostics


def test_baseline_verifier_rejects_unversioned_0002_schema(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        command.downgrade(config, "0002_source_neutral_jobs")
    finally:
        config.attributes["connection"].close()
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))

    diagnostics = verify_existing_baseline(engine)

    assert "unexpected table: company_sources" in diagnostics
    assert "unexpected table: career_sources" in diagnostics
    assert "unexpected table: job_postings" in diagnostics


def test_baseline_verifier_rejects_unknown_tables_and_views(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        command.downgrade(config, "0001_baseline")
    finally:
        config.attributes["connection"].close()
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text("CREATE TABLE career_surfaces (id integer primary key)"))
        connection.execute(text("CREATE VIEW extra_legacy_view AS SELECT 1 AS value"))

    diagnostics = verify_existing_baseline(engine)

    assert "unexpected table: career_surfaces" in diagnostics
    assert "unexpected view: extra_legacy_view" in diagnostics


def test_baseline_verifier_accepts_only_exact_unversioned_0001_baseline(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        command.downgrade(config, "0001_baseline")
    finally:
        config.attributes["connection"].close()
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))

    assert verify_existing_baseline(engine) == []


def test_baseline_verifier_rejects_structural_drift(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        command.downgrade(config, "0001_baseline")
    finally:
        config.attributes["connection"].close()
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text("ALTER TABLE companies ALTER COLUMN name TYPE TEXT"))
        connection.execute(text("ALTER TABLE companies ALTER COLUMN name DROP NOT NULL"))
        connection.execute(text("DROP INDEX ix_companies_slug"))
        connection.execute(text("CREATE INDEX ix_companies_slug ON companies (name)"))
        connection.execute(
            text("ALTER TABLE source_documents DROP CONSTRAINT source_documents_discovered_url_id_fkey")
        )
        connection.execute(text("DROP INDEX ix_source_documents_company_id"))
        connection.execute(
            text("CREATE INDEX ix_source_documents_company_id ON source_documents (company_slug)")
        )
        connection.execute(
            text(
                "CREATE OR REPLACE VIEW company_primary_career_pages AS "
                "SELECT company_id, company_slug, company_name, website, yc_is_hiring, "
                "yc_job_count, career_page_url, page_type, discovery_source, confidence, "
                "http_status, evidence, checked_at FROM company_career_pages "
                "WHERE is_primary = false"
            )
        )

    diagnostics = verify_existing_baseline(engine)

    assert "column type mismatch: companies.name expected varchar, found text" in diagnostics
    assert "column nullability mismatch: companies.name expected False, found True" in diagnostics
    assert "index columns mismatch: companies.ix_companies_slug" in diagnostics
    assert "index uniqueness mismatch: companies.ix_companies_slug" in diagnostics
    assert "foreign key mismatch: source_documents" in diagnostics
    assert "index columns mismatch: source_documents.ix_source_documents_company_id" in diagnostics
    assert "view definition mismatch: company_primary_career_pages" in diagnostics


def test_company_profile_round_trip_preserves_canonical_jobs(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('companies', 'id'), 900, false)"
            )
        )
    upsert_yc_companies(
        engine,
        [
            {
                "id": 900,
                "name": "Migration Example",
                "slug": "migration-example",
                "website": "https://migration.example",
                "batch": "S24",
                "regions": ["Remote"],
                "industries": [],
                "tags": [],
            }
        ],
    )
    with engine.connect() as connection:
        local_company_id = int(
            connection.scalar(
                select(yc_company_profiles_table.c.company_id).where(
                    yc_company_profiles_table.c.yc_company_id == 900
                )
            )
        )
    source, allowed, _ = JobRepository(engine).register_career_source(
        company_id=local_company_id,
        provider="greenhouse",
        source_kind="ats_board",
        external_source_id="migration-example",
        source_url="https://boards.greenhouse.io/migration-example",
        discovered_from_url="https://migration.example",
        now=now,
    )
    assert allowed is True
    JobSyncService(engine, clock=lambda: now).sync_snapshot(
        career_source_id=int(source["id"]),
        run_key="migration-round-trip",
        snapshot=SourceSnapshot(
            provider="greenhouse",
            external_source_id="migration-example",
            adapter_version="test",
            is_complete=True,
            http_status=200,
            jobs=[
                NormalizedJob(
                    external_job_id="1",
                    title="Senior Backend Engineer",
                    content_hash="migration-hash",
                    raw_payload={"id": "1"},
                )
            ],
        ),
    )

    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        command.downgrade(config, "0002_source_neutral_jobs")
    finally:
        config.attributes["connection"].close()
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(job_postings_table)) == 1
        assert connection.scalar(text("SELECT yc_url FROM companies")) == (
            "https://www.ycombinator.com/companies/migration-example"
        )

    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        command.upgrade(config, "head")
    finally:
        config.attributes["connection"].close()
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(job_postings_table)) == 1
        assert connection.scalar(
            select(func.count()).select_from(company_sources_table).where(
                company_sources_table.c.provider == "greenhouse"
            )
        ) == 0
        profile = connection.execute(select(yc_company_profiles_table)).mappings().one()
    assert profile["yc_company_id"] == 900
    assert profile["batch"] == "S24"


def test_company_schema_downgrade_refuses_independent_yc_local_ids(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 900,
                "name": "Independent ID Example",
                "slug": "independent-id-example",
                "website": "https://independent-id.example",
                "regions": [],
                "industries": [],
                "tags": [],
            }
        ],
    )
    with engine.connect() as connection:
        profile = connection.execute(select(yc_company_profiles_table)).mappings().one()
    assert profile["company_id"] != profile["yc_company_id"]

    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        with pytest.raises(RuntimeError, match="external IDs differ from local company IDs"):
            command.downgrade(config, "0002_source_neutral_jobs")
    finally:
        config.attributes["connection"].close()

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0004_source_registries"
        )


def test_company_schema_downgrade_refuses_non_yc_companies(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    company = CompanyRegistry(engine).register_company(
        name="Non YC Employer",
        website="https://non-yc.example",
    )
    JobSourceRegistry(engine).register_url(
        company_id=company.company_id,
        source_url="https://boards.greenhouse.io/non-yc-employer",
    )
    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        with pytest.raises(RuntimeError, match="non-YC companies exist"):
            command.downgrade(config, "0002_source_neutral_jobs")
    finally:
        config.attributes["connection"].close()

    inspector = inspect(engine)
    assert "yc_company_profiles" in inspector.get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0004_source_registries"
        )


def test_company_migration_normalizes_url_hostnames_like_runtime(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        command.downgrade(config, "0002_source_neutral_jobs")
    finally:
        config.attributes["connection"].close()

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO companies (
                    id, name, slug, yc_url, website, is_hiring, regions, industries, tags,
                    raw_json, created_at, updated_at
                ) VALUES
                    (1, 'Flight Control', 'flight-control',
                     'https://www.ycombinator.com/companies/flight-control',
                     'https://www.flightcontrol.dev?ref=bookface',
                     false, '[]', '[]', '[]', '{}', now(), now()),
                    (2, 'Port Example', 'port-example',
                     'https://www.ycombinator.com/companies/port-example',
                     'https://user:pass@www.port.example:8443/careers',
                     false, '[]', '[]', '[]', '{}', now(), now()),
                    (3, 'Malformed Website', 'malformed-website',
                     'https://www.ycombinator.com/companies/malformed-website',
                     'https://first.example, https://second.example',
                     false, '[]', '[]', '[]', '{}', now(), now())
                """
            )
        )

    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        command.upgrade(config, "head")
    finally:
        config.attributes["connection"].close()

    with engine.connect() as connection:
        domains = dict(
            connection.execute(text("SELECT slug, primary_domain FROM companies")).tuples().all()
        )
    assert domains == {
        "flight-control": "flightcontrol.dev",
        "port-example": "port.example",
        "malformed-website": None,
    }


def test_company_and_job_source_registries_do_not_share_provider_ownership(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        command.downgrade(config, "0002_source_neutral_jobs")
    finally:
        config.attributes["connection"].close()

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO companies (
                    id, name, slug, yc_url, website, is_hiring, regions, industries, tags,
                    raw_json, created_at, updated_at
                ) VALUES
                    (1, 'Company One', 'company-one',
                     'https://www.ycombinator.com/companies/company-one',
                     'https://one.example', false, '[]', '[]', '[]', '{}', now(), now()),
                    (2, 'Company Two', 'company-two',
                     'https://www.ycombinator.com/companies/company-two',
                     'https://two.example', false, '[]', '[]', '[]', '{}', now(), now())
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO company_sources (
                    company_id, provider, external_company_id, source_url, raw_json,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (
                    1, 'greenhouse', 'shared-token',
                    'https://boards.greenhouse.io/shared-token', '{}',
                    now(), now(), now(), now()
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO career_sources (
                    company_id, provider, source_kind, external_source_id, source_url,
                    status, raw_json, created_at, updated_at
                ) VALUES (
                    2, 'greenhouse', 'ats_board', 'shared-token',
                    'https://boards.greenhouse.io/shared-token',
                    'active', '{}', now(), now()
                )
                """
            )
        )

    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    try:
        command.upgrade(config, "head")
    finally:
        config.attributes["connection"].close()

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0004_source_registries"
        )
        assert connection.scalar(text("SELECT count(*) FROM companies")) == 2
        assert connection.scalar(text("SELECT count(*) FROM career_sources")) == 1
        assert connection.scalar(text("SELECT count(*) FROM company_sources")) == 0
    assert "yc_company_profiles" in inspect(engine).get_table_names()


def test_rebuild_database_replaces_an_unversioned_legacy_schema(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    with engine.begin() as connection:
        for table_name in (
            "job_posting_observations",
            "job_postings",
            "job_posting_versions",
            "source_sync_runs",
            "career_sources",
            "company_sources",
        ):
            connection.execute(text(f"DROP TABLE {table_name} CASCADE"))
        connection.execute(text("DROP TABLE alembic_version"))

    rebuild_database(engine)

    inspector = inspect(engine)
    assert {"companies", "company_sources", "yc_company_profiles", "job_postings", "job_posting_observations"}.issubset(
        inspector.get_table_names()
    )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0004_source_registries"
        )


def test_destructive_rebuild_handles_non_yc_and_independent_ids(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 900,
                "name": "YC Independent ID",
                "slug": "yc-independent-id",
                "website": "https://yc-independent.example",
                "regions": [],
                "industries": [],
                "tags": [],
            }
        ],
    )
    company = CompanyRegistry(engine).register_company(
        name="Non YC Employer",
        website="https://non-yc-rebuild.example",
    )
    JobSourceRegistry(engine).register_url(
        company_id=company.company_id,
        source_url="https://boards.greenhouse.io/non-yc-rebuild",
    )

    rebuild_database(engine)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0004_source_registries"
        )
        assert connection.scalar(select(func.count()).select_from(companies_table)) == 0
        assert connection.scalar(select(func.count()).select_from(yc_company_profiles_table)) == 0
