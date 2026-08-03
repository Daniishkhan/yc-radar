import pytest
from sqlalchemy import select

from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    engine_from_url,
    sanitized_yc_company_website,
    sanitized_yc_company_payloads,
    upsert_yc_companies,
)


@pytest.mark.parametrize(
    ("name", "website", "expected"),
    [
        ("Example", "  example.com\n", "https://example.com"),
        ("Example", "\thttps://www.example.com/about  ", "https://www.example.com/about"),
        ("Product Hunt", "http://producthunt.com", "http://producthunt.com"),
        ("PitchBook", "https://www.pitchbook.com/", "https://www.pitchbook.com/"),
        ("Substack", "https://substack.com", "https://substack.com"),
        ("Y Combinator", "https://www.ycombinator.com", "https://www.ycombinator.com"),
        ("GitHub", "https://github.com", "https://github.com"),
    ],
)
def test_sanitized_yc_company_website_accepts_valid_company_sites(
    name: str,
    website: str,
    expected: str,
) -> None:
    assert sanitized_yc_company_website({"name": name, "website": website}) == expected


@pytest.mark.parametrize(
    ("name", "website"),
    [
        ("Storyline", "https://www.producthunt.com/posts/storyline-7"),
        ("Uiflow", "https://www.ycombinator.com/companies/uiflow"),
        ("TypeLess", "https://apps.apple.com/us/app/typeless-ai-messenger/id6478489620"),
        ("OrderAhead", "https://www.crunchbase.com/organization/orderahead"),
        ("Vidpresso", "http://facebook.com/live/producer"),
        ("Descope", "https://github.com"),
        ("RecordBook", "https://play.google.com/store/apps/details?id=com.foodmonk.rekordapp"),
        ("SlidePay", "https://angel.co/slidepay"),
        ("Zenter", "https://googleblog.blogspot.com/2007/06/more-sharing.html"),
        ("FameGame", "https://itunes.apple.com/us/app/famegame/id1343049421"),
        ("Zen", "https://lnkd.in/example"),
        ("Goosebump", "http://m.me/higoosebump"),
    ],
)
def test_sanitized_yc_company_website_rejects_third_party_pages(
    name: str,
    website: str,
) -> None:
    assert sanitized_yc_company_website({"name": name, "website": website}) is None


@pytest.mark.parametrize(
    "website",
    [
        "",
        "not a website",
        "ftp://example.com",
        "https://one.example https://two.example",
        "https://one.example,https://two.example",
        "https://example.com:invalid",
        "https://example.com:",
        "https://example.com..",
        "https://user:password@example.com",
    ],
)
def test_sanitized_yc_company_website_rejects_malformed_or_multiple_urls(
    website: str,
) -> None:
    assert sanitized_yc_company_website({"name": "Example", "website": website}) is None


def test_sanitized_yc_company_website_does_not_require_brand_match_for_ordinary_hosts() -> None:
    assert (
        sanitized_yc_company_website(
            {"name": "Renamed Company", "website": "https://original-brand.example/careers"}
        )
        == "https://original-brand.example/careers"
    )


def test_sanitized_yc_company_payloads_deconflicts_shared_domain_claims() -> None:
    original = [
        {"id": 1, "name": "Leafpress", "website": "https://trycardinal.ai"},
        {"id": 2, "name": "Cardinal", "website": "https://trycardinal.ai/"},
    ]

    sanitized = sanitized_yc_company_payloads(original)

    assert sanitized[0]["website"] is None
    assert sanitized[1]["website"] == "https://trycardinal.ai/"
    assert sanitized[0]["_identity_conflict_evidence"] == {
        "kind": "website_domain_conflict",
        "claimed_website": "https://trycardinal.ai",
        "claimed_domain": "trycardinal.ai",
        "conflicting_incoming_companies": [
            {"external_id": "2", "name": "Cardinal"}
        ],
    }
    assert original[0]["website"] == "https://trycardinal.ai"


def test_sanitized_yc_company_payloads_clears_ambiguous_shared_domain_claims() -> None:
    sanitized = sanitized_yc_company_payloads(
        [
            {"id": 1, "name": "First Division", "website": "https://shared.example"},
            {"id": 2, "name": "Second Division", "website": "https://shared.example"},
        ]
    )

    assert [company["website"] for company in sanitized] == [None, None]


@pytest.mark.parametrize(
    ("name", "domain"),
    [
        ("Zinc", "zinc.com"),
        ("NanoCorp", "nanocorp.com"),
        ("Acme, Inc.", "acme.com"),
    ],
)
def test_sanitized_yc_company_payloads_does_not_strip_brand_suffix_text(
    name: str,
    domain: str,
) -> None:
    sanitized = sanitized_yc_company_payloads(
        [
            {"id": 1, "name": "Wrong Claimant", "website": f"https://{domain}"},
            {"id": 2, "name": name, "website": f"https://{domain}"},
        ]
    )

    assert sanitized[0]["website"] is None
    assert sanitized[1]["website"] == f"https://{domain}"


def test_yc_ingestion_sanitizes_neutral_website_but_preserves_raw_profile_json(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    raw_website = "  https://www.producthunt.com/posts/storyline-7  "

    upsert_yc_companies(
        engine,
        [
            {
                "id": 1813,
                "name": "Storyline",
                "slug": "storyline",
                "website": raw_website,
            }
        ],
    )

    with engine.connect() as connection:
        company = connection.execute(select(companies_table)).mappings().one()
        source = connection.execute(select(company_sources_table)).mappings().one()

    assert company["website"] is None
    assert company["primary_domain"] is None
    assert source["metadata"]["raw_payload"]["website"] == raw_website


def test_yc_bare_domain_matches_and_updates_the_same_neutral_company(
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
                "website": "  shared.example  ",
            }
        ],
    )

    with engine.connect() as connection:
        companies = list(connection.execute(select(companies_table)).mappings())
        source = connection.execute(select(company_sources_table)).mappings().one()

    assert len(companies) == 1
    assert source["company_id"] == existing.company_id
    assert companies[0]["website"] == "https://shared.example"
    assert companies[0]["primary_domain"] == "shared.example"


@pytest.mark.parametrize(
    "replacement_website",
    [None, "https://www.producthunt.com/posts/not-acme"],
)
def test_yc_refresh_preserves_existing_safe_neutral_website_when_source_is_unusable(
    postgres_database_url: str,
    replacement_website: str | None,
) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [{"id": 1, "name": "Acme", "slug": "acme", "website": "https://acme.test"}],
    )

    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "Acme",
                "slug": "acme",
                "website": replacement_website,
            }
        ],
    )

    with engine.connect() as connection:
        company = connection.execute(select(companies_table)).mappings().one()
    assert company["website"] == "https://acme.test"
    assert company["primary_domain"] == "acme.test"


def test_yc_refresh_clears_matching_legacy_unsafe_website(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    raw_website = "https://www.producthunt.com/posts/storyline-7"
    payload = {"id": 1, "name": "Storyline", "slug": "storyline", "website": raw_website}
    upsert_yc_companies(engine, [payload])
    with engine.begin() as connection:
        connection.execute(
            companies_table.update().values(
                website=raw_website,
                primary_domain="producthunt.com",
            )
        )

    upsert_yc_companies(engine, [payload])

    with engine.connect() as connection:
        company = connection.execute(select(companies_table)).mappings().one()
    assert company["website"] is None
    assert company["primary_domain"] is None


def test_partial_yc_upserts_deconflict_against_persisted_claims(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "Leafpress",
                "slug": "leafpress",
                "website": "https://trycardinal.ai",
            }
        ],
    )

    upsert_yc_companies(
        engine,
        [
            {
                "id": 2,
                "name": "Cardinal",
                "slug": "cardinal",
                "website": "https://trycardinal.ai",
            }
        ],
    )

    with engine.connect() as connection:
        companies = {
            row["name"]: row for row in connection.execute(select(companies_table)).mappings()
        }
        sources = {
            row["external_id"]: row
            for row in connection.execute(select(company_sources_table)).mappings()
        }
    assert companies["Leafpress"]["website"] == "https://trycardinal.ai"
    assert companies["Leafpress"]["primary_domain"] == "trycardinal.ai"
    assert companies["Cardinal"]["website"] is None
    assert companies["Cardinal"]["primary_domain"] is None
    assert sources["2"]["metadata"]["identity_conflict_evidence"] == {
        "kind": "website_domain_conflict",
        "claimed_website": "https://trycardinal.ai",
        "claimed_domain": "trycardinal.ai",
        "conflicting_companies": [
            {
                "company_id": companies["Leafpress"]["id"],
                "name": "Leafpress",
            }
        ],
    }


def test_yc_upsert_does_not_duplicate_mismatched_standalone_domain_owner(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    CompanyRegistry(engine).register_company(
        name="Cardinal",
        website="https://trycardinal.ai",
    )

    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "Leafpress",
                "slug": "leafpress",
                "website": "https://trycardinal.ai",
            }
        ],
    )

    with engine.connect() as connection:
        companies = {
            row["name"]: row for row in connection.execute(select(companies_table)).mappings()
        }
        source = connection.execute(select(company_sources_table)).mappings().one()
    assert companies["Cardinal"]["primary_domain"] == "trycardinal.ai"
    assert companies["Leafpress"]["website"] is None
    assert companies["Leafpress"]["primary_domain"] is None
    assert source["metadata"]["identity_conflict_evidence"]["claimed_domain"] == (
        "trycardinal.ai"
    )


def test_yc_refresh_never_takes_or_clears_another_company_domain(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [{"id": 1, "name": "Acme", "slug": "acme", "website": "https://acme.test"}],
    )
    owner = CompanyRegistry(engine).register_company(
        name="Other Company",
        website="https://other.test",
    )

    upsert_yc_companies(
        engine,
        [{"id": 1, "name": "Acme", "slug": "acme", "website": "https://other.test"}],
    )

    with engine.connect() as connection:
        companies = {
            row["name"]: row for row in connection.execute(select(companies_table)).mappings()
        }
        source = connection.execute(
            select(company_sources_table).where(company_sources_table.c.external_id == "1")
        ).mappings().one()

    assert companies["Acme"]["website"] == "https://acme.test"
    assert companies["Acme"]["primary_domain"] == "acme.test"
    assert companies["Other Company"]["website"] == "https://other.test"
    assert companies["Other Company"]["primary_domain"] == "other.test"
    assert source["metadata"]["identity_conflict_evidence"] == {
        "kind": "website_domain_conflict",
        "claimed_website": "https://other.test",
        "claimed_domain": "other.test",
        "conflicting_companies": [
            {"company_id": owner.company_id, "name": "Other Company"}
        ],
    }
