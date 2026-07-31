import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "discover_career_urls.py"
SPEC = importlib.util.spec_from_file_location("discover_career_urls", SCRIPT_PATH)
assert SPEC and SPEC.loader
discover_career_urls = importlib.util.module_from_spec(SPEC)
sys.modules["discover_career_urls"] = discover_career_urls
SPEC.loader.exec_module(discover_career_urls)

HttpResult = discover_career_urls.HttpResult


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

    async def get_many(self, urls: list[str]) -> list[HttpResult]:
        return await asyncio.gather(*(self.get(url) for url in urls))


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


def test_ats_matching_rejects_vendor_navigation_and_domain_substrings() -> None:
    assert (
        discover_career_urls.career_link_score(
            "https://clever.com",
            "https://clever.com/login?student",
            "Log in",
        )
        == 0
    )
    assert (
        discover_career_urls.career_link_score(
            "https://cspa.io",
            "https://wellfound.com/privacy",
            "Privacy",
        )
        == 0
    )
    assert (
        discover_career_urls.career_link_score(
            "https://example.com",
            "https://jobs.ashbyhq.com/example",
            "Open roles",
        )
        > 0
    )
    assert (
        discover_career_urls.career_link_score(
            "https://example.com", "https://apply.workable.com/", "Open roles"
        )
        == 0
    )
    assert (
        discover_career_urls.career_link_score(
            "https://example.com",
            "https://apply.workable.com/example",
            "Open roles",
        )
        > 0
    )


def test_normalize_url_drops_tracking_and_generic_listing_filters() -> None:
    assert discover_career_urls.normalize_url(
        "https://example.com",
        "/careers?utm_source=footer&location=USA",
    ) == "https://example.com/careers"
    assert discover_career_urls.normalize_url(
        "https://boards.greenhouse.io",
        "/embed/job_board?for=example",
    ) == "https://boards.greenhouse.io/embed/job_board?for=example"
    assert discover_career_urls.normalize_url(
        "https://example.com", "/roles?b=2&a=1"
    ) == "https://example.com/roles?a=1&b=2"
    assert discover_career_urls.normalize_url(
        "https://placement.example.com", "/jobs?filter=ALL"
    ) == "https://placement.example.com/jobs"
    assert discover_career_urls.normalize_url(
        "https://jobs.ashbyhq.com",
        "/example?utm_source=footer&ashby_jid=job-123",
    ) == "https://jobs.ashbyhq.com/example"
    assert discover_career_urls.normalize_url(
        "https://job-boards.greenhouse.io", "/example?gh_src=campaign"
    ) == "https://job-boards.greenhouse.io/example"
    assert discover_career_urls.normalize_url(
        "https://example.com", "https://user:secret@example.com/careers"
    ) is None


def test_linked_career_page_discovers_public_ats_board() -> None:
    async def run() -> dict[str, list[dict]]:
        company = {
            "id": 1,
            "slug": "example",
            "name": "Example",
            "website": "https://example.com",
            "is_hiring": True,
            "raw_json": {"jobPostings": []},
        }
        http = FakeHttp(
            {
                "https://example.com/": HttpResult(
                    url="https://example.com/",
                    final_url="https://example.com/",
                    status_code=200,
                    content_type="text/html",
                    text='<a href="/careers">Careers</a>',
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
                    text=(
                        '<a href="https://job-boards.greenhouse.io/example">'
                        "See open positions</a>"
                    ),
                ),
            }
        )
        return await discover_career_urls.discover_company_career_data(
            company,
            [],
            http,
            max_sitemaps=0,
            max_child_sitemaps=0,
        )

    import asyncio

    result = asyncio.run(run())

    assert any(
        event["normalized_url"] == "https://job-boards.greenhouse.io/example"
        and event["discovery_source"] == "career_page_link"
        for event in result["discovery_events"]
    )
    assert any(page["page_type"] == "ats" for page in result["career_pages"])


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


def test_duplicate_soft_404_probe_content_is_collapsed() -> None:
    async def run() -> dict[str, list[dict]]:
        company = {
            "id": 1,
            "slug": "example",
            "name": "Example",
            "website": "https://example.com",
            "is_hiring": False,
            "raw_json": {"jobPostings": []},
        }
        soft_404 = "<h1>Careers</h1><p>Open positions</p>"
        pages = {
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
        }
        for path in discover_career_urls.COMMON_PATHS:
            url = f"https://example.com{path}"
            pages[url] = HttpResult(
                url=url,
                final_url=url,
                status_code=200,
                content_type="text/html",
                text=soft_404,
            )
        return await discover_career_urls.discover_company_career_data(
            company,
            [],
            FakeHttp(pages),
            max_sitemaps=0,
            max_child_sitemaps=0,
        )

    result = asyncio.run(run())

    assert len(result["career_pages"]) == 1
    assert result["career_pages"][0]["normalized_url"] == "https://example.com/careers"


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
        set(),
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
        set(),
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

    event_keys = set()
    for _ in range(2):
        discover_career_urls.add_discovery_event(
            events,
            event_keys,
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
        set(),
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
        set(),
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
        set(),
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


def test_high_confidence_ats_homepage_link_skips_sitemap_fetches() -> None:
    async def run() -> tuple[dict[str, list[dict]], list[str]]:
        company = {
            "id": 1,
            "slug": "example",
            "name": "Example",
            "website": "https://example.com",
            "is_hiring": True,
            "raw_json": {"jobPostings": []},
        }
        http = FakeHttp(
            {
                "https://example.com/": HttpResult(
                    url="https://example.com/",
                    final_url="https://example.com/",
                    status_code=200,
                    content_type="text/html",
                    text='<a href="https://boards.greenhouse.io/example">Jobs</a>',
                )
            }
        )
        result = await discover_career_urls.discover_company_career_data(
            company,
            [],
            http,
            max_sitemaps=3,
            max_child_sitemaps=3,
        )
        return result, http.calls

    result, calls = asyncio.run(run())

    assert result["career_pages"][0]["normalized_url"] == (
        "https://boards.greenhouse.io/example"
    )
    assert calls == ["https://example.com/"]


def test_integrated_discovery_fetches_custom_robots_declared_sitemap() -> None:
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
                    status_code=200,
                    content_type="text/plain",
                    text="Sitemap: /custom-career-map.xml",
                ),
                "https://example.com/custom-career-map.xml": HttpResult(
                    url="https://example.com/custom-career-map.xml",
                    final_url="https://example.com/custom-career-map.xml",
                    status_code=200,
                    content_type="application/xml",
                    text="<url><loc>/careers/backend</loc></url>",
                ),
            }
        )

        result = await discover_career_urls.discover_company_career_data(
            company,
            [],
            http,
            max_sitemaps=3,
            max_child_sitemaps=0,
        )
        return result, http.calls

    result, calls = asyncio.run(run())

    assert "https://example.com/custom-career-map.xml" in calls
    assert [page["normalized_url"] for page in result["career_pages"]] == [
        "https://example.com/careers/backend"
    ]


def test_relative_robots_and_nested_sitemap_locations_resolve_against_final_response_urls() -> None:
    async def run() -> tuple[list[str], list[tuple[str, int | None]]]:
        http = FakeHttp(
            {
                "https://example.com/robots.txt": HttpResult(
                    url="https://example.com/robots.txt",
                    final_url="https://cdn.example.net/robots/robots.txt",
                    status_code=200,
                    content_type="text/plain",
                    text="Sitemap: maps/root.xml",
                ),
                "https://cdn.example.net/robots/maps/root.xml": HttpResult(
                    url="https://cdn.example.net/robots/maps/root.xml",
                    final_url="https://cdn.example.net/sitemaps/root.xml",
                    status_code=200,
                    content_type="application/xml",
                    text="<sitemap><loc>children/jobs.xml</loc></sitemap>",
                ),
                "https://cdn.example.net/sitemaps/children/jobs.xml": HttpResult(
                    url="https://cdn.example.net/sitemaps/children/jobs.xml",
                    final_url="https://cdn.example.net/sitemaps/children/jobs.xml",
                    status_code=200,
                    content_type="application/xml",
                    text="<url><loc>../careers/open-roles</loc></url>",
                ),
            }
        )
        robots = await discover_career_urls.discover_robots_sitemap_urls("https://example.com", http)
        hits = await discover_career_urls.discover_sitemap_hits(
            robots,
            http,
            max_child_sitemaps=2,
        )
        return robots, hits

    robots, hits = asyncio.run(run())

    assert robots == ["https://cdn.example.net/robots/maps/root.xml"]
    assert hits == [("https://cdn.example.net/sitemaps/careers/open-roles", 200)]


def test_terminal_homepage_http_error_preserves_prior_inventory() -> None:
    async def run() -> dict:
        company = {
            "id": 1,
            "slug": "example",
            "name": "Example",
            "website": "https://example.com",
            "is_hiring": False,
            "raw_json": {"jobPostings": []},
        }
        homepage = "https://example.com/"
        return await discover_career_urls.discover_company_career_data(
            company,
            [],
            FakeHttp(
                {
                    homepage: HttpResult(
                        url=homepage,
                        final_url=homepage,
                        status_code=403,
                        content_type="text/html",
                        text="blocked",
                        error_class="HttpStatusError",
                    )
                }
            ),
            max_sitemaps=3,
            max_child_sitemaps=3,
        )

    result = asyncio.run(run())

    assert result["failure"]["class"] == "HttpStatusError"
    assert result["failure"]["message"] == "HTTP 403"
    assert result["discovery_events"] == []
    assert result["career_pages"] == []


def test_too_many_redirects_is_a_structured_terminal_request_error(monkeypatch, tmp_path: Path) -> None:
    request = httpx.Request("GET", "https://example.com/careers")

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.TooManyRedirects("redirect loop", request=request)

    real_async_client = discover_career_urls.httpx.AsyncClient
    monkeypatch.setattr(
        discover_career_urls.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler)),
    )

    async def run() -> HttpResult:
        async with discover_career_urls.CachedHttpClient(
            tmp_path / "cache.json", concurrency=1, max_attempts=3
        ) as http:
            return await http.get("https://example.com/careers")

    result = asyncio.run(run())

    assert result.error_class == "TooManyRedirects"
    assert result.retryable is False
    assert result.attempt_count == 1


def test_discovery_writes_progress_status_after_each_persisted_batch(monkeypatch, tmp_path: Path) -> None:
    statuses: list[dict] = []
    companies = [
        {"id": 1, "slug": "one", "name": "One"},
        {"id": 2, "slug": "two", "name": "Two"},
    ]

    class FakeCachedHttpClient:
        def __init__(self, *args, **kwargs) -> None:
            self.cache_metrics = {"stores": 2}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def fake_discover_batch(batch, jobs_by_slug, http, **kwargs):
        del jobs_by_slug, http, kwargs
        return [
            {
                "company": company,
                "discovery_events": [],
                "career_pages": [],
                "applicable": True,
                "error": None,
                "error_class": None,
                "retry_count": 0,
                "warnings": [],
            }
            for company in batch
        ]

    monkeypatch.setattr(discover_career_urls, "engine_from_url", lambda *args: object())
    monkeypatch.setattr(discover_career_urls, "fetch_companies_for_discovery", lambda *args, **kwargs: companies)
    monkeypatch.setattr(discover_career_urls, "fetch_completed_career_discovery_slugs", lambda *args, **kwargs: set())
    monkeypatch.setattr(discover_career_urls, "fetch_yc_job_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(discover_career_urls, "CachedHttpClient", FakeCachedHttpClient)
    monkeypatch.setattr(discover_career_urls, "discover_company_batch", fake_discover_batch)
    monkeypatch.setattr(discover_career_urls, "replace_career_page_data", lambda *args, **kwargs: None)
    monkeypatch.setattr(discover_career_urls, "drop_legacy_career_surfaces_table", lambda *args: None)
    monkeypatch.setattr(discover_career_urls, "fetch_career_page_discovery_event_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(discover_career_urls, "fetch_company_career_page_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(discover_career_urls, "fetch_discovered_url_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(discover_career_urls, "write_csv", lambda *args: None)
    monkeypatch.setattr(discover_career_urls, "write_status", lambda _path, payload: statuses.append(payload))

    args = type(
        "Args",
        (),
        {
            "company_slug": [],
            "force": False,
            "limit": 2,
            "cache_path": tmp_path / "cache.json",
            "cache_dir": None,
            "legacy_cache_path": None,
            "concurrency": 2,
            "host_concurrency": 2,
            "max_http_attempts": 1,
            "max_sitemaps": 0,
            "max_child_sitemaps": 0,
            "batch_size": 1,
            "output_csv": tmp_path / "pages.csv",
            "discovered_urls_csv": tmp_path / "urls.csv",
            "events_csv": tmp_path / "events.csv",
            "write_raw_json": False,
            "raw_output_dir": tmp_path,
            "status_file": tmp_path / "status.json",
        },
    )()

    asyncio.run(discover_career_urls.run(args))

    assert [status["processed"] for status in statuses if status["state"] == "running"] == [0, 0, 1, 2]


def test_no_pending_discovery_preserves_existing_snapshot_files(
    monkeypatch, tmp_path: Path
) -> None:
    outputs = [tmp_path / name for name in ("pages.csv", "urls.csv", "events.csv")]
    for output in outputs:
        output.write_text("existing\n", encoding="utf-8")

    monkeypatch.setattr(discover_career_urls, "engine_from_url", lambda *args: object())
    monkeypatch.setattr(
        discover_career_urls,
        "fetch_companies_for_discovery",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        discover_career_urls,
        "fetch_completed_career_discovery_slugs",
        lambda *args, **kwargs: {"already-done"},
    )

    args = type(
        "Args",
        (),
        {
            "company_slug": [],
            "force": False,
            "limit": 10,
            "output_csv": outputs[0],
            "discovered_urls_csv": outputs[1],
            "events_csv": outputs[2],
            "status_file": tmp_path / "status.json",
        },
    )()

    asyncio.run(discover_career_urls.run(args))

    assert [output.read_text(encoding="utf-8") for output in outputs] == [
        "existing\n",
        "existing\n",
        "existing\n",
    ]
    status = __import__("json").loads(args.status_file.read_text(encoding="utf-8"))
    assert status["state"] == "completed"
    assert status["selected"] == status["processed"] == 0


def test_host_concurrency_is_bounded_by_origin_not_full_url(monkeypatch, tmp_path: Path) -> None:
    in_flight: dict[str, int] = {}
    peaks: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        in_flight[host] = in_flight.get(host, 0) + 1
        peaks[host] = max(peaks.get(host, 0), in_flight[host])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        in_flight[host] -= 1
        return httpx.Response(200, text="ok")

    real_async_client = discover_career_urls.httpx.AsyncClient
    monkeypatch.setattr(
        discover_career_urls.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler)),
    )

    async def run() -> None:
        async with discover_career_urls.CachedHttpClient(
            tmp_path / "cache.json", concurrency=8, host_concurrency=2, max_attempts=1
        ) as client:
            await client.get_many(
                [f"https://example.com/path-{index}" for index in range(8)]
                + [f"https://other.example/path-{index}" for index in range(4)]
            )

    asyncio.run(run())

    assert peaks["example.com"] == 2
    assert peaks["other.example"] == 2
    assert discover_career_urls.CachedHttpClient.host_semaphore_index(
        "https://example.com/a", 64
    ) == discover_career_urls.CachedHttpClient.host_semaphore_index(
        "https://example.com/b?x=1", 64
    )


def test_http_client_retries_retryable_responses_and_caches_terminal_result(
    monkeypatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    responses = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, text="ok")]

    async def fake_sleep(delay: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    real_async_client = discover_career_urls.httpx.AsyncClient
    monkeypatch.setattr(
        discover_career_urls.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(discover_career_urls.asyncio, "sleep", fake_sleep)

    async def run() -> tuple[HttpResult, HttpResult]:
        async with discover_career_urls.CachedHttpClient(
            tmp_path / "cache.json",
            concurrency=2,
            max_attempts=2,
        ) as http:
            first = await http.get("https://example.com/careers")
            second = await http.get("https://example.com/careers")
            return first, second

    first, second = asyncio.run(run())

    assert len(requests) == 2
    assert first.status_code == 200
    assert second.status_code == 200
