from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.database import (
    URL_INVENTORY_ADVISORY_LOCK,
    career_sources_table,
    company_sources_table,
    engine_from_url,
    replace_career_page_data,
)
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.source_discovery import discover_job_sources


def test_source_registration_conflicts_with_cleanup_lock(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    with engine.connect() as cleanup:
        assert cleanup.scalar(
            select(func.pg_try_advisory_lock(func.hashtext(URL_INVENTORY_ADVISORY_LOCK)))
        ) is True
        try:
            with pytest.raises(RuntimeError, match="cleanup apply is active"):
                discover_job_sources(engine)
        finally:
            cleanup.execute(
                select(func.pg_advisory_unlock(func.hashtext(URL_INVENTORY_ADVISORY_LOCK)))
            )


def test_source_discovery_registers_all_providers_for_standalone_companies(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    companies = CompanyRegistry(engine)
    one = companies.register_company(name="One", website="https://one.example")
    two = companies.register_company(name="Two", website="https://two.example")
    pages = [
        {
            "company_id": one.company_id,
            "company_slug": "one",
            "company_name": "One",
            "career_page_url": "https://job-boards.greenhouse.io/acme/jobs/10",
            "normalized_url": "https://job-boards.greenhouse.io/acme/jobs/10",
            "page_type": "ats",
            "discovery_source": "test",
            "checked_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
        {
            "company_id": two.company_id,
            "company_slug": "two",
            "company_name": "Two",
            "career_page_url": "https://jobs.ashbyhq.com/two/jobs/11",
            "normalized_url": "https://jobs.ashbyhq.com/two/jobs/11",
            "page_type": "ats",
            "discovery_source": "test",
            "checked_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ]
    replace_career_page_data(
        engine,
        discovery_events=[],
        career_pages=pages,
        company_slugs=["one", "two"],
    )

    first = discover_job_sources(engine)
    second = discover_job_sources(engine)

    assert first["registered"] == 2
    assert first["existing"] == 0
    assert first["conflicts"] == []
    assert second["registered"] == 0
    assert second["existing"] == 2
    assert {
        source["provider"]
        for source in JobRepository(engine).active_career_sources()
    } == {"ashby", "greenhouse"}
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(company_sources_table)) == 0


def test_source_discovery_refuses_board_reassignment_without_company_source_coupling(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    companies = CompanyRegistry(engine)
    one = companies.register_company(name="One", website="https://one.example")
    two = companies.register_company(name="Two", website="https://two.example")
    pages = [
        {
            "company_id": one.company_id,
            "company_slug": "one",
            "company_name": "One",
            "career_page_url": "https://job-boards.greenhouse.io/shared/jobs/10",
            "normalized_url": "https://job-boards.greenhouse.io/shared/jobs/10",
            "page_type": "ats",
            "discovery_source": "test",
            "checked_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
        {
            "company_id": two.company_id,
            "company_slug": "two",
            "company_name": "Two",
            "career_page_url": "https://job-boards.greenhouse.io/shared/jobs/11",
            "normalized_url": "https://job-boards.greenhouse.io/shared/jobs/11",
            "page_type": "ats",
            "discovery_source": "test",
            "checked_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ]
    replace_career_page_data(
        engine,
        discovery_events=[],
        career_pages=pages,
        company_slugs=["one", "two"],
    )

    result = discover_job_sources(engine, provider="greenhouse")

    assert result["registered"] == 1
    assert len(result["conflicts"]) == 1
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(career_sources_table)) == 1
        assert connection.scalar(select(func.count()).select_from(company_sources_table)) == 0
