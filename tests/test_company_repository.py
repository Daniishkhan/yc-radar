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

