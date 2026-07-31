from yc_radar.services.company_repository import CompanyRepository
from yc_radar.services.database import engine_from_url, upsert_yc_companies


def test_repository_loads_companies_from_postgres(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "AccessOwl",
                "slug": "accessowl",
                "website": "https://accessowl.example",
                "one_liner": "Access automation for modern IT teams",
                "batch": "Winter 2022",
                "status": "Active",
                "team_size": 10,
                "isHiring": True,
                "regions": ["Remote", "Pakistan"],
                "industry": "B2B",
                "subindustry": "B2B -> Security",
                "industries": ["B2B", "Security"],
                "tags": ["Developer Tools", "Security"],
                "prototype_score": 31,
                "prototype_angle": "Build an access review workflow.",
            }
        ],
    )

    companies = CompanyRepository(database_url=postgres_database_url).list()

    assert len(companies) == 1
    assert companies[0].slug == "accessowl"


def test_search_returns_small_hiring_targets(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "Tiny Infra",
                "slug": "tiny-infra",
                "website": "https://tiny.example",
                "one_liner": "Backend observability for API teams",
                "status": "Active",
                "team_size": 4,
                "isHiring": True,
                "regions": ["Remote"],
                "industry": "B2B",
                "tags": ["Infrastructure"],
                "prototype_score": 40,
            },
            {
                "id": 2,
                "name": "Big Ops",
                "slug": "big-ops",
                "website": "https://big.example",
                "one_liner": "Operations software",
                "status": "Active",
                "team_size": 80,
                "isHiring": True,
                "regions": ["US"],
                "industry": "B2B",
                "tags": [],
                "prototype_score": 12,
            },
        ],
    )

    companies = CompanyRepository(database_url=postgres_database_url).search(
        hiring=True,
        max_team_size=10,
    )

    assert [company.slug for company in companies] == ["tiny-infra"]
    assert all(company.is_hiring for company in companies)
    assert all(company.team_size is not None and company.team_size <= 10 for company in companies)
