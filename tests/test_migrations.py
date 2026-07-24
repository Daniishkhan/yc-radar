from sqlalchemy import inspect, text

from yc_radar.services.database import engine_from_url
from yc_radar.services.migrations import rebuild_database, verify_existing_baseline


def test_alembic_head_contains_legacy_and_source_neutral_schema(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    assert {
        "companies",
        "yc_job_postings",
        "company_sources",
        "career_sources",
        "source_sync_runs",
        "job_postings",
        "job_posting_versions",
        "job_posting_observations",
    }.issubset(tables)
    assert "company_primary_career_pages" in inspector.get_view_names()
    assert {index["name"] for index in inspector.get_indexes("job_postings")} >= {
        "ix_job_postings_company_active",
        "ix_job_postings_content_hash",
    }
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "0002_source_neutral_jobs"


def test_baseline_verifier_is_read_only_and_accepts_migrated_schema(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)

    assert verify_existing_baseline(engine) == []


def test_baseline_verifier_rejects_structural_drift(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    with engine.begin() as connection:
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
    assert {"companies", "company_sources", "job_postings", "job_posting_observations"}.issubset(
        inspector.get_table_names()
    )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002_source_neutral_jobs"
