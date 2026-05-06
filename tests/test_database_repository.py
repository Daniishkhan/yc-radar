from yc_radar.services.company_repository import CompanyRepository
from yc_radar.services.database import (
    engine_from_url,
    fetch_yc_job_rows,
    upsert_companies,
    upsert_yc_job_postings,
)


def test_company_repository_reads_from_sqlite(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'yc_radar.db'}"
    engine = engine_from_url(database_url)
    upsert_companies(
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


def test_upsert_yc_job_postings_round_trips_structured_fields(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'yc_radar.db'}"
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
