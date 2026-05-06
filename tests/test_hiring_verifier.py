import json

from yc_radar.domain.models import Company
from yc_radar.services.candidate_fit import DEFAULT_CANDIDATE_PROFILE
from yc_radar.services.hiring_verifier import (
    ScrapedPage,
    detect_career_links,
    load_hiring_cache,
    save_hiring_cache,
    verify_company_hiring,
)


class FakeScraper:
    def __init__(self, pages: dict[str, ScrapedPage]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def scrape(self, url: str) -> ScrapedPage:
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError(f"missing fake page: {url}")
        return self.pages[url]


class FailingScraper:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def scrape(self, url: str) -> ScrapedPage:
        self.calls.append(url)
        raise RuntimeError("rate limit")


def company() -> Company:
    return Company(
        name="Example",
        slug="example",
        yc_url="https://www.ycombinator.com/companies/example",
        website="https://example.com",
        one_liner="AI infrastructure",
        status="Active",
        team_size=5,
        isHiring=True,
    )


def test_detect_career_links_prefers_exact_careers_pages() -> None:
    page = ScrapedPage(
        url="https://example.com",
        html='<a href="/careers">Careers</a><a href="/blog">Blog</a>',
        links=["https://jobs.ashbyhq.com/example", "https://example.com/pricing"],
    )

    links = detect_career_links("https://example.com", page)

    assert "https://jobs.ashbyhq.com/example" in links
    assert "https://example.com/careers" in links
    assert "https://example.com/pricing" not in links


def test_detect_career_links_ignores_generic_same_domain_links() -> None:
    page = ScrapedPage(
        url="https://example.com",
        html='<a href="https://platform.example.com">Platform</a><a href="/docs">Docs</a>',
        links=["https://example.com/product"],
    )

    links = detect_career_links("https://example.com", page)

    assert links == []


def test_verify_hiring_from_homepage_and_careers_page() -> None:
    scraper = FakeScraper(
        {
            "https://example.com": ScrapedPage(
                url="https://example.com",
                html='<a href="/careers">Careers</a>',
            ),
            "https://example.com/careers": ScrapedPage(
                url="https://example.com/careers",
                markdown="## Open Roles\nSenior Backend Engineer\nFounding AI Engineer",
            ),
        }
    )

    verification = verify_company_hiring(company(), scraper, DEFAULT_CANDIDATE_PROFILE)

    assert verification.verified_hiring_status == "hiring"
    assert verification.career_page_url == "https://example.com/careers"
    assert "Senior Backend Engineer" in verification.verified_roles
    assert verification.role_fit == "strong"
    assert verification.firecrawl_pages_used == 2
    assert scraper.calls == ["https://example.com", "https://example.com/careers"]


def test_verify_unknown_when_no_careers_page_is_found() -> None:
    scraper = FakeScraper({"https://example.com": ScrapedPage(url="https://example.com", html="Home")})

    verification = verify_company_hiring(company(), scraper, DEFAULT_CANDIDATE_PROFILE)

    assert verification.verified_hiring_status == "unknown"
    assert verification.firecrawl_pages_used == 1
    assert scraper.calls == ["https://example.com"]


def test_verify_unknown_when_firecrawl_fails() -> None:
    scraper = FailingScraper()

    verification = verify_company_hiring(company(), scraper, DEFAULT_CANDIDATE_PROFILE)

    assert verification.verified_hiring_status == "unknown"
    assert verification.verification_error == "rate limit"
    assert verification.firecrawl_pages_used == 1
    assert scraper.calls == ["https://example.com"]


def test_verify_not_hiring_on_explicit_no_open_roles() -> None:
    scraper = FakeScraper(
        {
            "https://example.com": ScrapedPage(
                url="https://example.com",
                html='<a href="/careers">Jobs</a>',
            ),
            "https://example.com/careers": ScrapedPage(
                url="https://example.com/careers",
                markdown="No open roles right now, but check back later.",
            ),
        }
    )

    verification = verify_company_hiring(company(), scraper, DEFAULT_CANDIDATE_PROFILE)

    assert verification.verified_hiring_status == "not_hiring"
    assert verification.verified_roles == []
    assert verification.verification_source_url == "https://example.com/careers"


def test_max_pages_per_company_is_enforced() -> None:
    scraper = FakeScraper(
        {
            "https://example.com": ScrapedPage(
                url="https://example.com",
                html=(
                    '<a href="/careers">Careers</a>'
                    '<a href="/jobs">Jobs</a>'
                    '<a href="/join">Join us</a>'
                ),
            ),
            "https://example.com/careers": ScrapedPage(
                url="https://example.com/careers",
                markdown="Senior Software Engineer",
            ),
            "https://example.com/jobs": ScrapedPage(
                url="https://example.com/jobs",
                markdown="Data Engineer",
            ),
        }
    )

    verification = verify_company_hiring(
        company(),
        scraper,
        DEFAULT_CANDIDATE_PROFILE,
        max_pages_per_company=2,
    )

    assert verification.firecrawl_pages_used == 2
    assert len(scraper.calls) == 2


def test_hiring_cache_round_trip(tmp_path) -> None:
    path = tmp_path / "hiring_verifications.json"
    cache = {"example:https://example.com": {"verified_hiring_status": "hiring"}}

    save_hiring_cache(path, cache)

    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert load_hiring_cache(path) == cache
