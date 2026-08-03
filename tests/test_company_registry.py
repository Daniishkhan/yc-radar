from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from yc_radar.services.company_registry import CompanyIdentityConflict, CompanyRegistry
from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    engine_from_url,
    upsert_yc_companies,
)


def test_company_can_exist_without_a_provider_source(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    result = CompanyRegistry(engine).register_company(
        name="Independent Employer",
        website="https://independent.example",
    )

    with engine.connect() as connection:
        company = connection.execute(select(companies_table)).mappings().one()
        source_count = connection.scalar(select(func.count()).select_from(company_sources_table))

    assert result.company_created is True
    assert company["id"] == result.company_id
    assert company["primary_domain"] == "independent.example"
    assert company["identity_state"] == "verified"
    assert source_count == 0


def test_directory_and_job_sources_share_one_company_source_registry(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    company = registry.register_company(
        name="Registry Example",
        website="https://registry.example",
    )
    registry.register_source_identity(
        company_id=company.company_id,
        provider="curated_list",
        external_id="registry-example",
        source_kind="directory",
        source_url="https://directory.example/registry-example",
        metadata={"list": "global-remote"},
    )
    registry.register_source_identity(
        company_id=company.company_id,
        provider="ashby",
        external_id="registry-example",
        source_kind="ats",
        source_url="https://jobs.ashbyhq.com/registry-example",
        sync_mode="complete_snapshot",
    )

    with engine.connect() as connection:
        sources = list(
            connection.execute(
                select(company_sources_table).order_by(company_sources_table.c.provider)
            ).mappings()
        )

    assert [row["provider"] for row in sources] == ["ashby", "curated_list"]
    assert [row["source_kind"] for row in sources] == ["ats", "directory"]
    assert [row["sync_mode"] for row in sources] == ["complete_snapshot", "none"]
    assert {row["company_id"] for row in sources} == {company.company_id}


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


def test_provisional_registration_never_merges_on_name_alone(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)

    first = registry.register_provisional_company(
        name="Unresolved Employer",
        requested_slug="unresolved-employer",
    )
    second = registry.register_provisional_company(
        name="Unresolved Employer",
        requested_slug="unresolved-employer",
    )

    with engine.connect() as connection:
        companies = list(
            connection.execute(select(companies_table).order_by(companies_table.c.id)).mappings()
        )

    assert first.company_id != second.company_id
    assert first.matched_by == "provisional_company"
    assert [row["slug"] for row in companies] == [
        "unresolved-employer",
        "unresolved-employer-2",
    ]
    assert {row["identity_state"] for row in companies} == {"provisional"}
    assert all(row["website"] is None and row["primary_domain"] is None for row in companies)


def test_yc_profile_data_lives_on_the_company_source(
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
        source = connection.execute(select(company_sources_table)).mappings().one()

    assert company["normalized_name"] == "yc example"
    assert company["primary_domain"] == "example.com"
    assert source["company_id"] == company["id"]
    assert source["provider"] == "yc"
    assert source["external_id"] == "42"
    assert source["source_kind"] == "directory"
    assert source["sync_mode"] == "complete_snapshot"
    assert source["metadata"]["batch"] == "S24"
    assert source["metadata"]["is_hiring"] is True
    assert source["metadata"]["regions"] == ["Remote"]


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
        yc_source = connection.execute(
            select(company_sources_table).where(company_sources_table.c.provider == "yc")
        ).mappings().one()

    assert standalone_row["name"] == "Independent Employer"
    assert yc_source["external_id"] == "1"
    assert yc_source["company_id"] != standalone.company_id


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
        source = connection.execute(
            select(company_sources_table).where(company_sources_table.c.provider == "yc")
        ).mappings().one()

    assert source["company_id"] == existing.company_id


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
        external_id="shared-id",
        now=now,
    )

    with pytest.raises(CompanyIdentityConflict, match="belongs"):
        registry.register_source_identity(
            company_id=second.company_id,
            provider="directory",
            external_id="shared-id",
            now=now,
        )


def test_source_identity_refreshes_metadata_without_moving_ownership(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    company = registry.register_company(name="Refresh", website="https://refresh.example")
    created = registry.register_source_identity(
        company_id=company.company_id,
        provider="directory",
        external_id="refresh",
        metadata={"version": 1},
    )
    refreshed = registry.register_source_identity(
        company_id=company.company_id,
        provider="directory",
        external_id="refresh",
        metadata={"version": 2},
    )

    with engine.connect() as connection:
        source = connection.execute(select(company_sources_table)).mappings().one()

    assert created is True
    assert refreshed is False
    assert source["company_id"] == company.company_id
    assert source["metadata"] == {"version": 2}


def test_registration_requires_valid_website(postgres_database_url: str) -> None:
    registry = CompanyRegistry(engine_from_url(postgres_database_url))

    with pytest.raises(ValueError, match="website"):
        registry.register_company(name="Invalid", website="not-a-url")
    with pytest.raises(ValueError, match="website"):
        registry.register_company(
            name="Multiple",
            website="https://one.example, https://two.example",
        )


def test_provisional_registration_requires_a_name(postgres_database_url: str) -> None:
    registry = CompanyRegistry(engine_from_url(postgres_database_url))

    with pytest.raises(ValueError, match="company name"):
        registry.register_provisional_company(name="   ")
