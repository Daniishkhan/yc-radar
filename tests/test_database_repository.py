from sqlalchemy import inspect

from yc_radar.services.company_repository import CompanyRepository
from yc_radar.services.database import (
    create_schema,
    engine_from_url,
    fetch_companies_for_discovery,
    fetch_discovered_url_rows,
    fetch_page_classification_rows,
    fetch_source_document_rows,
    fetch_yc_job_rows,
    replace_career_page_data,
    upsert_career_page_discovery_statuses,
    upsert_yc_companies,
    upsert_page_classifications,
    upsert_source_documents,
    upsert_yc_job_postings,
)


def test_postgres_schema_includes_document_intelligence_tables(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    create_schema(engine)

    table_names = set(inspect(engine).get_table_names())

    assert {
        "source_documents",
        "discovered_urls",
        "page_classifications",
        "career_page_discovery_statuses",
        "external_job_postings",
        "job_extraction_runs",
        "document_chunks",
        "document_embeddings",
        "job_role_signals",
    }.issubset(table_names)


def test_company_repository_reads_from_postgres(postgres_database_url: str) -> None:
    database_url = postgres_database_url
    engine = engine_from_url(database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "Agent Data",
                "slug": "agent-data",
                "website": "https://agentdata.example",
                "one_liner": "AI data pipelines for operators",
                "batch": "Winter 2026",
                "status": "Active",
                "team_size": 4,
                "isHiring": True,
                "regions": ["Remote"],
                "industry": "B2B",
                "subindustry": "B2B -> Analytics",
                "industries": ["B2B", "Analytics"],
                "tags": ["Artificial Intelligence", "Developer Tools"],
                "prototype_score": 42,
                "prototype_angle": "Build a focused data-agent demo.",
            }
        ],
    )

    repo = CompanyRepository(database_url=database_url)

    company = repo.get_by_slug("agent-data")
    assert company is not None
    assert company.name == "Agent Data"
    assert company.is_hiring is True
    assert repo.search(query="pipelines", hiring=True, max_team_size=10)[0].slug == "agent-data"


def test_upsert_yc_job_postings_round_trips_structured_fields(
    postgres_database_url: str,
) -> None:
    database_url = postgres_database_url
    engine = engine_from_url(database_url)

    upsert_yc_job_postings(
        engine,
        [
            {
                "id": 94317,
                "company_id": 29425,
                "company_slug": "zep-ai",
                "company_name": "Zep AI",
                "company_yc_url": "https://www.ycombinator.com/companies/zep-ai",
                "title": "Lead Forward Deployed Engineer",
                "url": "/companies/zep-ai/jobs/3rQuV1s-lead-forward-deployed-engineer",
                "location": "San Francisco, United States / Remote (US)",
                "salaryRange": "$175K - $250K",
                "equityRange": "0.50% - 1.50%",
                "visa": "US citizen/visa only",
                "skills": ["Python", "TypeScript", "LLMs"],
            }
        ],
    )

    jobs = fetch_yc_job_rows(engine)

    assert len(jobs) == 1
    assert jobs[0]["visa"] == "US citizen/visa only"
    assert jobs[0]["skills"] == ["Python", "TypeScript", "LLMs"]


def test_replace_career_page_data_separates_raw_events_from_canonical_pages(
    postgres_database_url: str,
) -> None:
    database_url = postgres_database_url
    engine = engine_from_url(database_url)

    replace_career_page_data(
        engine,
        discovery_events=[
            {
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "website": "https://example.com",
                "url": "https://www.ycombinator.com/companies/example/jobs/abc",
                "normalized_url": "https://www.ycombinator.com/companies/example/jobs/abc",
                "page_type": "yc_job",
                "discovery_source": "yc_job_posting",
                "confidence": 1.0,
                "evidence": "Engineer",
            },
            {
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "website": "https://example.com",
                "url": "https://example.com/careers",
                "normalized_url": "https://example.com/careers",
                "page_type": "careers_page",
                "discovery_source": "homepage_link",
                "confidence": 0.84,
                "http_status": 200,
                "evidence": "Careers",
            },
        ],
        career_pages=[
            {
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "website": "https://example.com",
                "career_page_url": "https://example.com/careers",
                "normalized_url": "https://example.com/careers",
                "page_type": "careers_page",
                "discovery_source": "homepage_link",
                "confidence": 0.84,
                "http_status": 200,
                "evidence": "Careers",
                "is_primary": True,
                "observed_source_count": 1,
            }
        ],
        company_slugs=["example"],
        statuses=[
            {
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "website": "https://example.com",
                "status": "completed",
                "discovery_event_count": 2,
                "career_page_count": 1,
            }
        ],
    )

    with engine.connect() as connection:
        event_count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM career_page_discovery_events"
        ).scalar_one()
        page_count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM company_career_pages"
        ).scalar_one()
        discovered_url_count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM discovered_urls"
        ).scalar_one()
        primary_count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM company_primary_career_pages"
        ).scalar_one()
        status_count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM career_page_discovery_statuses WHERE status = 'completed'"
        ).scalar_one()

    assert event_count == 2
    assert page_count == 1
    assert discovered_url_count == 1
    assert primary_count == 1
    assert status_count == 1

    discovered_urls = fetch_discovered_url_rows(engine)
    assert discovered_urls[0]["url_kind"] == "careers_page"
    assert discovered_urls[0]["url_key"] == "https://example.com/careers"


def test_failed_discovery_checkpoint_preserves_existing_events_pages_and_queue(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    event = {
        "company_id": 1,
        "company_slug": "example",
        "company_name": "Example",
        "website": "https://example.com",
        "url": "https://example.com/careers",
        "normalized_url": "https://example.com/careers",
        "page_type": "careers_page",
        "discovery_source": "homepage_link",
        "confidence": 0.84,
        "http_status": 200,
        "evidence": "Careers",
    }
    page = {
        **event,
        "career_page_url": event["url"],
        "is_primary": True,
        "observed_source_count": 1,
    }
    replace_career_page_data(engine, [event], [page], company_slugs=["example"])
    before_urls = fetch_discovered_url_rows(engine)
    with engine.connect() as connection:
        before_events = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM career_page_discovery_events"
        ).scalar_one()

    upsert_career_page_discovery_statuses(
        engine,
        [
            {
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "website": "https://example.com",
                "status": "failed",
                "error": "TooManyRedirects",
                "raw_json": {"error_class": "TooManyRedirects"},
            }
        ],
    )

    after_urls = fetch_discovered_url_rows(engine)
    with engine.connect() as connection:
        after_events = connection.exec_driver_sql("SELECT COUNT(*) FROM career_page_discovery_events").scalar_one()
        status = connection.exec_driver_sql("SELECT status, error FROM career_page_discovery_statuses").one()
    assert [row["id"] for row in after_urls] == [row["id"] for row in before_urls]
    assert after_events == before_events
    assert status == ("failed", "TooManyRedirects")


def test_pending_discovery_limit_is_applied_after_completed_rows_are_excluded(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {"id": 1, "name": "Done", "slug": "a-done", "regions": [], "industries": [], "tags": []},
            {"id": 2, "name": "Pending One", "slug": "b-pending", "regions": [], "industries": [], "tags": []},
            {"id": 3, "name": "Pending Two", "slug": "c-pending", "regions": [], "industries": [], "tags": []},
        ],
    )
    upsert_career_page_discovery_statuses(
        engine,
        [{"company_id": 1, "company_slug": "a-done", "company_name": "Done", "status": "completed"}],
    )

    rows = fetch_companies_for_discovery(engine, only_pending=True, limit=2)

    assert [row["slug"] for row in rows] == ["b-pending", "c-pending"]


def test_source_documents_and_page_classifications_round_trip(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)

    replace_career_page_data(
        engine,
        discovery_events=[],
        career_pages=[
            {
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "website": "https://example.com",
                "career_page_url": "https://example.com/careers/software-engineer",
                "normalized_url": "https://example.com/careers/software-engineer",
                "page_type": "jobs_page",
                "discovery_source": "sitemap",
                "confidence": 0.78,
                "is_primary": True,
                "observed_source_count": 1,
            }
        ],
        company_slugs=["example"],
    )
    discovered_url = fetch_discovered_url_rows(engine)[0]
    source_key = f"{discovered_url['company_slug']}:{discovered_url['url_key']}"

    upsert_source_documents(
        engine,
        [
            {
                "discovered_url_id": discovered_url["id"],
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "source_type": "career_url",
                "source_key": source_key,
                "url": discovered_url["normalized_url"],
                "normalized_url": discovered_url["normalized_url"],
                "title": "Software Engineer at Example",
                "raw_text": "<h1>Software Engineer</h1>",
                "clean_text": "Software Engineer Apply now Requirements",
                "content_hash": "abc123",
            }
        ],
    )
    source_document = fetch_source_document_rows(engine, source_keys=[source_key])[0]

    upsert_page_classifications(
        engine,
        [
            {
                "source_document_id": source_document["id"],
                "discovered_url_id": discovered_url["id"],
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "url": discovered_url["normalized_url"],
                "normalized_url": discovered_url["normalized_url"],
                "page_kind": "job_detail",
                "confidence": 0.86,
                "parser_name": "test_parser",
                "parser_version": "test",
                "http_status": 200,
                "job_title": "Software Engineer",
                "role_titles": ["Software Engineer"],
                "job_count": 1,
                "evidence": {"detail_marker_hits": 2},
            }
        ],
    )

    classifications = fetch_page_classification_rows(engine)

    assert source_document["discovered_url_id"] == discovered_url["id"]
    assert classifications[0]["page_kind"] == "job_detail"
    assert classifications[0]["role_titles"] == ["Software Engineer"]


def test_fetch_error_retry_selection_is_explicit_and_budgeted(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    page = {
        "company_id": 1,
        "company_slug": "example",
        "company_name": "Example",
        "website": "https://example.com",
        "career_page_url": "https://example.com/careers",
        "normalized_url": "https://example.com/careers",
        "page_type": "careers_page",
        "discovery_source": "test",
        "confidence": 0.8,
        "is_primary": True,
    }
    replace_career_page_data(engine, [], [page], company_slugs=["example"])
    discovered_url = fetch_discovered_url_rows(engine)[0]
    upsert_source_documents(
        engine,
        [
            {
                "discovered_url_id": discovered_url["id"],
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "source_type": "career_url",
                "source_key": "example:retry",
                "content_hash": "retry",
            }
        ],
    )
    document = fetch_source_document_rows(engine, source_keys=["example:retry"])[0]
    upsert_page_classifications(
        engine,
        [
            {
                "source_document_id": document["id"],
                "discovered_url_id": discovered_url["id"],
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "url": page["normalized_url"],
                "normalized_url": page["normalized_url"],
                "page_kind": "fetch_error",
                "parser_name": "test",
                "parser_version": "test",
                "evidence": {"fetch": {"retryable": True, "attempt_count": 1}},
            }
        ],
    )

    assert fetch_discovered_url_rows(engine, only_unclassified=True) == []
    retryable = fetch_discovered_url_rows(
        engine, retry_fetch_errors=True, max_fetch_attempts=3
    )
    exhausted = fetch_discovered_url_rows(
        engine, retry_fetch_errors=True, max_fetch_attempts=1
    )

    assert retryable[0]["id"] == discovered_url["id"]
    assert retryable[0]["fetch_attempt_count"] == 1
    assert exhausted == []
