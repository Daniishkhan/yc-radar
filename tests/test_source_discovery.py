from datetime import UTC, datetime

from yc_radar.services.database import engine_from_url, replace_career_page_data, upsert_companies
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.source_discovery import discover_greenhouse_sources


def test_greenhouse_discovery_registers_idempotently_and_refuses_reassignment(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_companies(
        engine,
        [
            {"id": 1, "name": "One", "slug": "one", "regions": [], "industries": [], "tags": []},
            {"id": 2, "name": "Two", "slug": "two", "regions": [], "industries": [], "tags": []},
        ],
    )
    pages = [
        {
            "company_id": 1,
            "company_slug": "one",
            "company_name": "One",
            "career_page_url": "https://boards.greenhouse.io/acme/jobs/10",
            "normalized_url": "https://boards.greenhouse.io/acme/jobs/10",
            "page_type": "ats",
            "discovery_source": "test",
            "checked_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
        {
            "company_id": 2,
            "company_slug": "two",
            "company_name": "Two",
            "career_page_url": "https://boards.greenhouse.io/acme/jobs/11",
            "normalized_url": "https://boards.greenhouse.io/acme/jobs/11",
            "page_type": "ats",
            "discovery_source": "test",
            "checked_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ]
    replace_career_page_data(engine, discovery_events=[], career_pages=pages, company_slugs=["one", "two"])

    first = discover_greenhouse_sources(engine)
    second = discover_greenhouse_sources(engine)

    assert first["registered"] == 1
    assert first["existing"] == 0
    assert first["conflicts"] == [
        {"board_token": "acme", "existing_company_id": 1, "requested_company_id": 2}
    ]
    assert second["registered"] == 0
    assert second["existing"] == 1
    assert len(second["conflicts"]) == 1
    assert len(JobRepository(engine).active_career_sources(provider="greenhouse")) == 1
