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
