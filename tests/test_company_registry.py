from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from yc_radar.services.company_registry import CompanyIdentityConflict, CompanyRegistry
from yc_radar.services.database import (
    career_sources_table,
    companies_table,
    company_sources_table,
    engine_from_url,
    fetch_companies_for_discovery,
    upsert_yc_companies,
    yc_company_profiles_table,
)
from yc_radar.services.job_source_registry import JobSourceRegistry


def test_company_can_exist_without_yc_or_job_source(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    result = CompanyRegistry(engine).register_company(
        name="Independent Employer",
        website="https://independent.example",
    )

    with engine.connect() as connection:
        company = connection.execute(select(companies_table)).mappings().one()
        assert connection.scalar(select(func.count()).select_from(company_sources_table)) == 0
        assert connection.scalar(select(func.count()).select_from(career_sources_table)) == 0
        assert connection.scalar(select(func.count()).select_from(yc_company_profiles_table)) == 0
    assert result.company_created is True
    assert company["id"] == result.company_id
    assert company["primary_domain"] == "independent.example"
    discovery_companies = fetch_companies_for_discovery(engine)
    assert [row["id"] for row in discovery_companies] == [result.company_id]
    assert discovery_companies[0]["yc_company_id"] is None


def test_company_and_job_source_registries_are_independent(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    company_registry = CompanyRegistry(engine)
    company = company_registry.register_company(
        name="Registry Example",
        website="https://registry.example",
    )
    company_registry.register_source_identity(
        company_id=company.company_id,
        provider="curated_list",
        external_company_id="registry-example",
        source_url="https://directory.example/registry-example",
    )
    source = JobSourceRegistry(engine).register_url(
        company_id=company.company_id,
        source_url="https://jobs.ashbyhq.com/registry-example",
    )

    with engine.connect() as connection:
        company_sources = list(connection.execute(select(company_sources_table)).mappings())
        career_sources = list(connection.execute(select(career_sources_table)).mappings())
    assert [row["provider"] for row in company_sources] == ["curated_list"]
    assert [row["provider"] for row in career_sources] == ["ashby"]
    assert source.external_source_id == "registry-example"


def test_company_registration_is_idempotent_on_exact_verified_identity(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    first = registry.register_company(
        name="Stable Employer",
        website="https://www.stable.example/about",
    )
    second = registry.register_company(
        name="Stable Employer",
        website="https://stable.example",
    )

    assert first.company_id == second.company_id
    assert first.company_created is True
    assert second.company_created is False
    assert second.matched_by == "primary_domain"


def test_company_registration_rejects_conflicting_identity_evidence(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    registry.register_company(name="First Employer", website="https://first.example")

    with pytest.raises(CompanyIdentityConflict, match="different normalized name"):
        registry.register_company(
            name="Different Employer",
            website="https://first.example/careers",
        )


def test_company_registration_rejects_shared_directory_identity_website(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)

    with pytest.raises(ValueError, match="safe company-owned identity evidence"):
        CompanyRegistry(engine).register_company(
            name="Descope",
            website="https://github.com",
        )


def test_company_registration_allows_shared_root_for_its_exact_brand(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)

    result = CompanyRegistry(engine).register_company(
        name="GitHub",
        website="https://github.com",
    )

    assert result.company_created is True


def test_yc_ingestion_keeps_profile_data_out_of_neutral_company(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 42,
                "name": "YC Example",
                "slug": "yc-example",
                "website": "https://www.example.com/about",
                "batch": "S24",
                "isHiring": True,
                "regions": ["Remote"],
                "industries": ["B2B"],
                "tags": ["Developer Tools"],
            }
        ],
    )

    with engine.connect() as connection:
        company = connection.execute(select(companies_table)).mappings().one()
        profile = connection.execute(select(yc_company_profiles_table)).mappings().one()
        source = connection.execute(select(company_sources_table)).mappings().one()
    assert company["normalized_name"] == "yc example"
    assert company["primary_domain"] == "example.com"
    assert profile["company_id"] == company["id"]
    assert profile["yc_company_id"] == 42
    assert source["provider"] == "yc"


def test_yc_external_id_cannot_overwrite_standalone_company(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    standalone = CompanyRegistry(engine).register_company(
        name="Independent Employer",
        website="https://independent.example",
    )
    assert standalone.company_id == 1

    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "YC Employer",
                "slug": "yc-employer",
                "website": "https://yc-employer.example",
                "regions": [],
                "industries": [],
                "tags": [],
            }
        ],
    )

    with engine.connect() as connection:
        standalone_row = (
            connection.execute(
                select(companies_table).where(companies_table.c.id == standalone.company_id)
            )
            .mappings()
            .one()
        )
        profile = connection.execute(select(yc_company_profiles_table)).mappings().one()
    assert standalone_row["name"] == "Independent Employer"
    assert profile["yc_company_id"] == 1
    assert profile["company_id"] != standalone.company_id


def test_yc_ingestion_reuses_exact_standalone_identity(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    existing = CompanyRegistry(engine).register_company(
        name="Shared Employer",
        website="https://shared.example",
    )
    upsert_yc_companies(
        engine,
        [
            {
                "id": 99,
                "name": "Shared Employer",
                "slug": "shared-employer-yc",
                "website": "https://shared.example/about",
                "regions": [],
                "industries": [],
                "tags": [],
            }
        ],
    )

    with engine.connect() as connection:
        profile = connection.execute(select(yc_company_profiles_table)).mappings().one()
    assert profile["company_id"] == existing.company_id


def test_company_source_identity_cannot_move_between_companies(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    first = registry.register_company(name="First", website="https://first.example")
    second = registry.register_company(name="Second", website="https://second.example")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registry.register_source_identity(
        company_id=first.company_id,
        provider="directory",
        external_company_id="shared-id",
        now=now,
    )

    with pytest.raises(CompanyIdentityConflict, match="belongs"):
        registry.register_source_identity(
            company_id=second.company_id,
            provider="directory",
            external_company_id="shared-id",
            now=now,
        )


def test_registration_requires_valid_website(postgres_database_url: str) -> None:
    registry = CompanyRegistry(engine_from_url(postgres_database_url))

    with pytest.raises(ValueError, match="website"):
        registry.register_company(name="Invalid", website="not-a-url")
    with pytest.raises(ValueError, match="website"):
        registry.register_company(
            name="Multiple",
            website="https://one.example, https://two.example",
        )
