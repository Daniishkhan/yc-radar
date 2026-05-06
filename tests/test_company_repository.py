from yc_radar.services.company_repository import CompanyRepository


def test_repository_loads_yc_companies() -> None:
    companies = CompanyRepository().list()
    assert len(companies) > 5000
    assert any(company.slug == "accessowl" for company in companies)


def test_search_returns_small_hiring_targets() -> None:
    companies = CompanyRepository().search(hiring=True, max_team_size=10)
    assert companies
    assert all(company.is_hiring for company in companies)
    assert all(company.team_size is not None and company.team_size <= 10 for company in companies)


def test_repository_falls_back_to_snapshot_csv_when_database_is_empty(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty.db'}"
    csv_path = tmp_path / "yc_companies.csv"
    csv_path.write_text(
        "\n".join(
            [
                "id,name,slug,yc_url,website,one_liner,batch,status,stage,team_size,"
                "isHiring,all_locations,regions,industry,subindustry,industries,tags,job_count",
                "1,Snapshot Co,snapshot-co,https://www.ycombinator.com/companies/snapshot-co,"
                "https://snapshot.example,Backend observability,S26,Active,,4,true,,Remote,"
                "B2B,B2B -> Infrastructure,B2B; Infrastructure,Developer Tools,0",
            ]
        ),
        encoding="utf-8",
    )

    companies = CompanyRepository(csv_path=csv_path, database_url=database_url).list()

    assert [company.slug for company in companies] == ["snapshot-co"]
    assert companies[0].is_hiring is True
    assert companies[0].is_remote_friendly is True
