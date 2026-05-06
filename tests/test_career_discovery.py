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
        if normalized and discover_career_urls.career_link_score(
            "https://example.com",
            normalized,
            text,
        ) > 0:
            scored.append(normalized)

    assert "https://example.com/careers" in scored
    assert "https://jobs.ashbyhq.com/example" in scored
    assert "https://example.com/pricing" not in scored


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


def test_common_path_probes_are_used_only_when_no_discovered_surface_exists() -> None:
    async def run() -> tuple[list[dict], list[str]]:
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

        surfaces = await discover_career_urls.discover_company_surfaces(
            company,
            [],
            http,
            max_sitemaps=0,
            max_child_sitemaps=0,
        )
        return surfaces, http.calls

    import asyncio

    surfaces, calls = asyncio.run(run())

    assert any(surface["source"] == "common_path_probe" for surface in surfaces)
    assert "https://example.com/careers" in calls


def test_duplicate_surfaces_keep_highest_confidence() -> None:
    surfaces: dict[str, dict] = {}
    company = {"id": 1, "slug": "example", "name": "Example", "raw_json": {}}
    checked_at = discover_career_urls.datetime.now(discover_career_urls.UTC)

    discover_career_urls.add_surface(
        surfaces,
        company,
        url="https://example.com/careers/",
        url_type="careers_page",
        source="sitemap",
        confidence=0.78,
        http_status=200,
        evidence="sitemap",
        checked_at=checked_at,
    )
    discover_career_urls.add_surface(
        surfaces,
        company,
        url="https://example.com/careers",
        url_type="careers_page",
        source="homepage_link",
        confidence=0.84,
        http_status=200,
        evidence="Careers",
        checked_at=checked_at,
    )

    assert len(surfaces) == 1
    assert next(iter(surfaces.values()))["source"] == "homepage_link"
