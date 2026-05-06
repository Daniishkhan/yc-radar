import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "discover_career_urls.py"
SPEC = importlib.util.spec_from_file_location("discover_career_urls", SCRIPT_PATH)
assert SPEC and SPEC.loader
discover_career_urls = importlib.util.module_from_spec(SPEC)
sys.modules["discover_career_urls"] = discover_career_urls
SPEC.loader.exec_module(discover_career_urls)

HttpResult = discover_career_urls.HttpResult
CachedHttpClient = discover_career_urls.CachedHttpClient


class FakeHttp:
    def __init__(self, pages: dict[str, HttpResult]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def get(self, url: str) -> HttpResult:
        self.calls.append(url)
        return self.pages.get(
            url,
            HttpResult(url=url, final_url=url, status_code=404, content_type="text/html", text=""),
        )


def test_homepage_anchor_extraction_catches_careers_and_ats_links() -> None:
    html = """
    <a href="/careers">Careers</a>
    <a href="https://jobs.ashbyhq.com/example">Open roles</a>
    <a href="/pricing">Pricing</a>
    """

    anchors = discover_career_urls.extract_homepage_links(html)
    scored = []
    for href, text in anchors:
        normalized = discover_career_urls.normalize_url("https://example.com", href)
        if (
            normalized
            and discover_career_urls.career_link_score(
                "https://example.com",
                normalized,
                text,
            )
            > 0
        ):
            scored.append(normalized)

    assert "https://example.com/careers" in scored
    assert "https://jobs.ashbyhq.com/example" in scored
    assert "https://example.com/pricing" not in scored


def test_vendor_marketing_pages_are_not_treated_as_ats_career_urls() -> None:
    assert discover_career_urls.is_career_url(
        "https://www.ashbyhq.com/product-updates/ai-assisted-application-review"
    ) is False
    assert discover_career_urls.is_career_url(
        "https://www.ashbyhq.com/podcast/episodes/recruiting-team-productivity"
    ) is False
    assert discover_career_urls.page_type_for(
        "https://www.ashbyhq.com/product-updates/ai-assisted-application-review"
    ) == "careers_page"
    assert discover_career_urls.is_career_url("https://jobs.ashbyhq.com/example") is True
    assert discover_career_urls.page_type_for("https://jobs.ashbyhq.com/example") == "ats"


def test_sitemap_hits_ignore_vendor_marketing_noise() -> None:
    async def run() -> list[tuple[str, int | None]]:
        http = FakeHttp(
            {
                "https://www.ashbyhq.com/sitemap.xml": HttpResult(
                    url="https://www.ashbyhq.com/sitemap.xml",
                    final_url="https://www.ashbyhq.com/sitemap.xml",
                    status_code=200,
                    content_type="application/xml",
                    text=(
                        "<url><loc>https://www.ashbyhq.com/product-updates/ai</loc></url>"
                        "<url><loc>https://www.ashbyhq.com/podcast/episodes/foo</loc></url>"
                        "<url><loc>https://www.ashbyhq.com/careers</loc></url>"
                    ),
                ),
            }
        )
        return await discover_career_urls.discover_sitemap_hits(
            ["https://www.ashbyhq.com/sitemap.xml"],
            http,
            max_child_sitemaps=0,
        )

    import asyncio

    hits = asyncio.run(run())

    assert hits == [("https://www.ashbyhq.com/careers", 200)]


def test_one_level_sitemap_index_expansion_finds_career_urls() -> None:
    async def run() -> list[tuple[str, int | None]]:
        http = FakeHttp(
            {
                "https://example.com/sitemap.xml": HttpResult(
                    url="https://example.com/sitemap.xml",
                    final_url="https://example.com/sitemap.xml",
                    status_code=200,
                    content_type="application/xml",
                    text="<sitemap><loc>https://example.com/pages.xml</loc></sitemap>",
                ),
                "https://example.com/pages.xml": HttpResult(
                    url="https://example.com/pages.xml",
                    final_url="https://example.com/pages.xml",
                    status_code=200,
                    content_type="application/xml",
                    text=(
                        "<url><loc>https://example.com/careers/open-roles</loc></url>"
                        "<url><loc>https://example.com/blog/best-job-boards</loc></url>"
                    ),
                ),
            }
        )
        return await discover_career_urls.discover_sitemap_hits(
            ["https://example.com/sitemap.xml"],
            http,
            max_child_sitemaps=4,
        )

    import asyncio

    hits = asyncio.run(run())

    assert hits == [("https://example.com/careers/open-roles", 200)]


def test_common_path_probes_are_used_only_when_no_external_career_page_exists() -> None:
    async def run() -> tuple[dict[str, list[dict]], list[str]]:
        company = {
            "id": 1,
            "slug": "example",
            "name": "Example",
            "website": "https://example.com",
            "is_hiring": False,
            "raw_json": {"jobPostings": []},
        }
        http = FakeHttp(
            {
                "https://example.com/": HttpResult(
                    url="https://example.com/",
                    final_url="https://example.com/",
                    status_code=200,
                    content_type="text/html",
                    text="<html>No career links here</html>",
                ),
                "https://example.com/robots.txt": HttpResult(
                    url="https://example.com/robots.txt",
                    final_url="https://example.com/robots.txt",
                    status_code=404,
                    content_type="text/plain",
                    text="",
                ),
                "https://example.com/careers": HttpResult(
                    url="https://example.com/careers",
                    final_url="https://example.com/careers",
                    status_code=200,
                    content_type="text/html",
                    text="<h1>Careers</h1><p>Open positions</p>",
                ),
            }
        )

        result = await discover_career_urls.discover_company_career_data(
            company,
            [],
            http,
            max_sitemaps=0,
            max_child_sitemaps=0,
        )
        return result, http.calls

    import asyncio

    result, calls = asyncio.run(run())

    assert any(
        event["discovery_source"] == "common_path_probe" for event in result["discovery_events"]
    )
    assert result["career_pages"][0]["career_page_url"] == "https://example.com/careers"
    assert "https://example.com/careers" in calls


def test_common_path_probe_rejects_non_ats_cross_domain_redirects() -> None:
    result = HttpResult(
        url="https://example.com/careers",
        final_url="https://acquirer.example/jobs",
        status_code=200,
        content_type="text/html",
        text="<h1>Careers</h1><p>Open positions</p>",
    )

    assert discover_career_urls.is_valid_probe_hit(result) is False


def test_resume_skips_only_completed_companies_unless_forced() -> None:
    companies = [
        {"slug": "done"},
        {"slug": "pending"},
    ]

    pending = discover_career_urls.pending_discovery_companies(
        companies,
        completed_slugs={"done"},
        force=False,
    )
    forced = discover_career_urls.pending_discovery_companies(
        companies,
        completed_slugs={"done"},
        force=True,
    )

    assert [company["slug"] for company in pending] == ["pending"]
    assert [company["slug"] for company in forced] == ["done", "pending"]


def test_discovery_status_marks_success_and_failure() -> None:
    success = discover_career_urls.discovery_status(
        {
            "company": {"id": 1, "slug": "example", "name": "Example"},
            "discovery_events": [{"url": "https://example.com/careers"}],
            "career_pages": [{"career_page_url": "https://example.com/careers"}],
            "error": None,
        }
    )
    failure = discover_career_urls.discovery_status(
        {
            "company": {"id": 2, "slug": "broken", "name": "Broken"},
            "discovery_events": [],
            "career_pages": [],
            "error": "boom",
        }
    )

    assert success["status"] == "completed"
    assert success["discovery_event_count"] == 1
    assert success["career_page_count"] == 1
    assert failure["status"] == "failed"
    assert failure["error"] == "boom"


def test_discovery_events_are_non_lossy_but_career_pages_are_canonical() -> None:
    events: list[dict] = []
    company = {"id": 1, "slug": "example", "name": "Example", "raw_json": {}}
    checked_at = discover_career_urls.datetime.now(discover_career_urls.UTC)

    discover_career_urls.add_discovery_event(
        events,
        company,
        url="https://example.com/careers/",
        page_type="careers_page",
        discovery_source="sitemap",
        confidence=0.78,
        http_status=200,
        evidence="sitemap",
        checked_at=checked_at,
    )
    discover_career_urls.add_discovery_event(
        events,
        company,
        url="https://example.com/careers",
        page_type="careers_page",
        discovery_source="homepage_link",
        confidence=0.84,
        http_status=200,
        evidence="Careers",
        checked_at=checked_at,
    )

    pages = discover_career_urls.build_company_career_pages(events)

    assert len(events) == 2
    assert len(pages) == 1
    assert pages[0]["discovery_source"] == "homepage_link"
    assert pages[0]["observed_source_count"] == 2


def test_exact_duplicate_homepage_events_are_collapsed() -> None:
    events: list[dict] = []
    company = {"id": 1, "slug": "example", "name": "Example", "raw_json": {}}
    checked_at = discover_career_urls.datetime.now(discover_career_urls.UTC)

    for _ in range(2):
        discover_career_urls.add_discovery_event(
            events,
            company,
            url="https://example.com/careers",
            page_type="careers_page",
            discovery_source="homepage_link",
            confidence=0.84,
            http_status=200,
            evidence="Careers",
            checked_at=checked_at,
        )

    assert len(events) == 1


def test_canonical_pages_collapse_scheme_and_www_variants() -> None:
    events: list[dict] = []
    company = {"id": 1, "slug": "example", "name": "Example", "raw_json": {}}
    checked_at = discover_career_urls.datetime.now(discover_career_urls.UTC)

    discover_career_urls.add_discovery_event(
        events,
        company,
        url="http://www.example.com/careers",
        page_type="careers_page",
        discovery_source="homepage_link",
        confidence=0.84,
        http_status=200,
        evidence="Careers",
        checked_at=checked_at,
    )
    discover_career_urls.add_discovery_event(
        events,
        company,
        url="https://example.com/careers",
        page_type="careers_page",
        discovery_source="sitemap",
        confidence=0.78,
        http_status=200,
        evidence="career-like URL in sitemap",
        checked_at=checked_at,
    )

    pages = discover_career_urls.build_company_career_pages(events)

    assert len(pages) == 1
    assert pages[0]["observed_source_count"] == 2
    assert pages[0]["raw_json"]["observed_urls"] == [
        "http://www.example.com/careers",
        "https://example.com/careers",
    ]


def test_yc_jobs_stay_as_events_and_do_not_become_company_career_pages() -> None:
    events: list[dict] = []
    company = {"id": 1, "slug": "example", "name": "Example", "raw_json": {}}
    checked_at = discover_career_urls.datetime.now(discover_career_urls.UTC)

    discover_career_urls.add_discovery_event(
        events,
        company,
        url="https://www.ycombinator.com/companies/example/jobs/abc-engineer",
        page_type="yc_job",
        discovery_source="yc_job_posting",
        confidence=1.0,
        http_status=None,
        evidence="Engineer | Remote | Will sponsor",
        checked_at=checked_at,
    )

    assert len(events) == 1
    assert discover_career_urls.build_company_career_pages(events) == []


def test_empty_or_corrupt_http_cache_starts_fresh(tmp_path: Path) -> None:
    empty_cache = tmp_path / "empty.json"
    empty_cache.write_text("", encoding="utf-8")

    empty_client = CachedHttpClient(empty_cache, concurrency=1)
    assert empty_client.cache == {}

    corrupt_cache = tmp_path / "corrupt.json"
    corrupt_cache.write_text("{not-json", encoding="utf-8")

    corrupt_client = CachedHttpClient(corrupt_cache, concurrency=1)

    assert corrupt_client.cache == {}
    assert not corrupt_cache.exists()
    assert (tmp_path / "corrupt.json.corrupt").exists()


def test_http_cache_save_is_atomic(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    client = CachedHttpClient(cache_path, concurrency=1)
    client.cache["https://example.com"] = {
        "url": "https://example.com",
        "final_url": "https://example.com",
        "status_code": 200,
        "content_type": "text/html",
        "text": "ok",
        "error": None,
    }

    client.save()

    assert cache_path.exists()
    assert not (tmp_path / "cache.json.tmp").exists()
