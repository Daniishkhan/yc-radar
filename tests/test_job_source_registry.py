from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select

from yc_radar.adapters.ashby import AshbyAdapter
from yc_radar.adapters.greenhouse import GreenhouseAdapter
from yc_radar.services.database import companies_table, company_sources_table, engine_from_url
from yc_radar.services.job_source_registry import (
    JobSourceProviderRegistry,
    JobSourceRegistry,
    UnknownJobSourceProvider,
)


def test_provider_registry_detects_supported_sources_without_yc_context() -> None:
    registry = JobSourceProviderRegistry([GreenhouseAdapter(), AshbyAdapter()])

    greenhouse = registry.detect("https://job-boards.greenhouse.io/acme/jobs/42")
    ashby = registry.detect("https://jobs.ashbyhq.com/other/jobs/42")

    assert registry.providers == ("ashby", "greenhouse")
    assert greenhouse is not None
    assert greenhouse.provider == "greenhouse"
    assert greenhouse.external_id == "acme"
    assert greenhouse.canonical_url == "https://job-boards.greenhouse.io/acme"
    assert ashby is not None
    assert ashby.provider == "ashby"
    assert ashby.external_id == "other"
    assert ashby.canonical_url == "https://jobs.ashbyhq.com/other"


def test_provider_registry_rejects_unknown_provider_and_url() -> None:
    registry = JobSourceProviderRegistry([GreenhouseAdapter(), AshbyAdapter()])

    assert registry.detect("https://jobs.example.com/acme") is None
    with pytest.raises(UnknownJobSourceProvider, match="unsupported"):
        registry.adapter_for("lever")


def test_provider_registry_refuses_duplicate_provider_registration() -> None:
    registry = JobSourceProviderRegistry([GreenhouseAdapter()])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(GreenhouseAdapter())


def test_job_source_registration_writes_unified_company_source(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    with engine.begin() as connection:
        company_id = int(
            connection.execute(
                insert(companies_table)
                .values(
                    name="Acme",
                    normalized_name="acme",
                    slug="acme",
                    website="https://acme.example",
                    primary_domain="acme.example",
                    identity_state="verified",
                    metadata={},
                    created_at=now,
                    updated_at=now,
                )
                .returning(companies_table.c.id)
            ).scalar_one()
        )

    registry = JobSourceRegistry(engine)
    created = registry.register_url(
        company_id=company_id,
        source_url="https://job-boards.greenhouse.io/acme/jobs/42",
        evidence={"method": "common_crawl"},
        now=now,
    )
    repeated = registry.register_url(
        company_id=company_id,
        source_url="https://boards.greenhouse.io/acme",
        now=now,
    )

    assert created.company_source_id == repeated.company_source_id
    assert created.external_id == "acme"
    assert created.created is True
    assert repeated.created is False
    with engine.connect() as connection:
        source = connection.execute(select(company_sources_table)).mappings().one()
    assert source["company_id"] == company_id
    assert source["provider"] == "greenhouse"
    assert source["source_kind"] == "ats_board"
    assert source["external_id"] == "acme"
    assert source["sync_mode"] == "complete_snapshot"
    assert source["status"] == "active"
