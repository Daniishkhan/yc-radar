from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from yc_radar.core.config import get_settings
from yc_radar.services.database import (
    engine_from_url,
    fetch_companies_for_discovery,
    fetch_yc_job_rows,
    replace_career_surfaces,
)

CAREER_TERMS = (
    "career",
    "careers",
    "job",
    "jobs",
    "join",
    "hiring",
    "openings",
    "open-positions",
    "open_positions",
    "open roles",
    "work-with-us",
    "work_with_us",
    "work with us",
)
ATS_DOMAINS = (
    "ashbyhq.com",
    "greenhouse.io",
    "lever.co",
    "workable.com",
    "workdayjobs.com",
    "bamboohr.com",
    "recruitee.com",
    "smartrecruiters.com",
    "applytojob.com",
    "app.dover.com",
    "wellfound.com",
)
LOW_VALUE_TERMS = (
    "/a/",
    "/academy/",
    "/blog/",
    "/news/",
    "/templates/",
    "blog",
    "docs",
    "documentation",
    "pricing",
    "login",
    "signin",
    "signup",
    "privacy",
    "terms",
    "twitter",
    "linkedin",
    "facebook",
    "instagram",
)
CAREER_PATH_PATTERN = re.compile(
    r"(^|/)(careers?|jobs?|job-openings?|open-positions?|open-roles?|"
    r"join-us|join-our-team|work-with-us)(/|$)",
    re.I,
)
COMMON_PATHS = (
    "/careers",
    "/jobs",
    "/join-us",
    "/join",
    "/work-with-us",
    "/open-positions",
)
SITEMAP_CANDIDATES = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
)
CSV_FIELDS = [
    "company_id",
    "company_slug",
    "company_name",
    "website",
    "yc_is_hiring",
    "yc_job_count",
    "url",
    "normalized_url",
    "url_type",
    "source",
    "confidence",
    "http_status",
    "evidence",
    "checked_at",
]


@dataclass
class HttpResult:
    url: str
    final_url: str
    status_code: int | None
    content_type: str
    text: str
    error: str | None = None


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text = " ".join(part.strip() for part in self._text if part.strip())
            self.anchors.append((self._href, re.sub(r"\s+", " ", text)))
            self._href = None
            self._text = []


class CachedHttpClient:
    def __init__(self, cache_path: Path, *, concurrency: int) -> None:
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = self._load_cache()
        self.semaphore = asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(
            headers={"User-Agent": "yc-radar-career-discovery/0.1"},
            follow_redirects=True,
            timeout=10,
        )

    async def __aenter__(self) -> "CachedHttpClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.client.aclose()
        self.save()

    async def get(self, url: str) -> HttpResult:
        if url in self.cache:
            cached = self.cache[url]
            return HttpResult(**cached)

        async with self.semaphore:
            try:
                response = await self.client.get(url)
                result = HttpResult(
                    url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    text=response.text[:500_000],
                )
            except Exception as exc:
                result = HttpResult(
                    url=url,
                    final_url=url,
                    status_code=None,
                    content_type="",
                    text="",
                    error=str(exc),
                )
            self.cache[url] = result.__dict__
            return result

    def save(self) -> None:
        self.cache_path.write_text(json.dumps(self.cache, indent=2, sort_keys=True), encoding="utf-8")

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self.cache_path.exists():
            return {}
        return json.loads(self.cache_path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover YC company career/job URL surfaces.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--max-sitemaps", type=int, default=6)
    parser.add_argument("--max-child-sitemaps", type=int, default=8)
    parser.add_argument("--cache-path", type=Path, default=Path("data/cache/career_url_discovery.json"))
    parser.add_argument("--output-json", type=Path, default=Path("data/yc_career_surfaces_raw.json"))
    parser.add_argument("--output-csv", type=Path, default=Path("data/yc_career_surfaces.csv"))
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = engine_from_url(settings.database_url)
    companies = fetch_companies_for_discovery(engine, limit=args.limit)
    jobs_by_slug: dict[str, list[dict[str, Any]]] = {}
    for job in fetch_yc_job_rows(engine):
        jobs_by_slug.setdefault(job["company_slug"], []).append(job)

    async with CachedHttpClient(args.cache_path, concurrency=args.concurrency) as http:
        tasks = [
            discover_company_surfaces(
                company,
                jobs_by_slug.get(company["slug"], []),
                http,
                max_sitemaps=args.max_sitemaps,
                max_child_sitemaps=args.max_child_sitemaps,
            )
            for company in companies
        ]
        surface_groups = await asyncio.gather(*tasks)

    surfaces = [surface for group in surface_groups for surface in group]
    company_slugs = [company["slug"] for company in companies]
    replace_career_surfaces(engine, surfaces, company_slugs=company_slugs)
    write_json(args.output_json, surfaces)
    write_csv(args.output_csv, surfaces)

    print(f"Checked {len(companies)} companies.")
    print(f"Discovered {len(surfaces)} career surfaces.")
    print(f"Wrote {args.output_json}")
    print(f"Wrote {args.output_csv}")
    print(f"Updated {settings.database_url}")


async def discover_company_surfaces(
    company: dict[str, Any],
    yc_jobs: list[dict[str, Any]],
    http: CachedHttpClient,
    *,
    max_sitemaps: int,
    max_child_sitemaps: int,
) -> list[dict[str, Any]]:
    checked_at = datetime.now(UTC)
    surfaces: dict[str, dict[str, Any]] = {}

    for job in yc_jobs:
        absolute_url = job.get("absolute_url") or urljoin("https://www.ycombinator.com", job.get("url") or "")
        add_surface(
            surfaces,
            company,
            url=absolute_url,
            url_type="yc_job",
            source="yc_job_posting",
            confidence=1.0,
            http_status=None,
            evidence=f"{job.get('title', '')} | {job.get('location', '')} | {job.get('visa', '')}",
            checked_at=checked_at,
            raw_json=job,
        )

    website = canonical_homepage(company.get("website"))
    if not website:
        return sorted_surfaces(surfaces)

    homepage = await http.get(website)
    for href, text in extract_homepage_links(homepage.text):
        url = normalize_url(website, href)
        if not url:
            continue
        score = career_link_score(website, url, text)
        if score <= 0:
            continue
        add_surface(
            surfaces,
            company,
            url=url,
            url_type=url_type_for(url),
            source="homepage_link",
            confidence=score,
            http_status=homepage.status_code,
            evidence=text or href,
            checked_at=checked_at,
        )

    sitemap_urls = await discover_sitemap_urls(website, http, max_sitemaps=max_sitemaps)
    sitemap_hits = await discover_sitemap_hits(
        sitemap_urls,
        http,
        max_child_sitemaps=max_child_sitemaps,
    )
    for url, status_code in sitemap_hits:
        add_surface(
            surfaces,
            company,
            url=url,
            url_type=url_type_for(url),
            source="sitemap",
            confidence=0.78,
            http_status=status_code,
            evidence="career-like URL in sitemap",
            checked_at=checked_at,
        )

    if not any(surface["source"] != "yc_job_posting" for surface in surfaces.values()):
        for path in COMMON_PATHS:
            probe_url = urljoin(website.rstrip("/") + "/", path.lstrip("/"))
            result = await http.get(probe_url)
            if is_valid_probe_hit(result):
                add_surface(
                    surfaces,
                    company,
                    url=result.final_url,
                    url_type=url_type_for(result.final_url),
                    source="common_path_probe",
                    confidence=0.65,
                    http_status=result.status_code,
                    evidence=path,
                    checked_at=checked_at,
                )

    return sorted_surfaces(surfaces)


def extract_homepage_links(html: str) -> list[tuple[str, str]]:
    parser = AnchorParser()
    parser.feed(html or "")
    return parser.anchors


async def discover_sitemap_urls(
    homepage: str,
    http: CachedHttpClient,
    *,
    max_sitemaps: int,
) -> list[str]:
    urls: list[str] = []
    robots = await http.get(urljoin(homepage.rstrip("/") + "/", "robots.txt"))
    if robots.status_code and robots.status_code < 400:
        for line in robots.text.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap_url = line.split(":", 1)[1].strip()
                if sitemap_url:
                    urls.append(sitemap_url)

    for path in SITEMAP_CANDIDATES:
        urls.append(urljoin(homepage.rstrip("/") + "/", path.lstrip("/")))

    return dedupe(urls)[:max_sitemaps]


async def discover_sitemap_hits(
    sitemap_urls: list[str],
    http: CachedHttpClient,
    *,
    max_child_sitemaps: int,
) -> list[tuple[str, int | None]]:
    hits: list[tuple[str, int | None]] = []
    child_sitemaps: list[str] = []
    for sitemap_url in sitemap_urls:
        result = await http.get(sitemap_url)
        if not result.status_code or result.status_code >= 400:
            continue
        locs = extract_sitemap_locs(result.text)
        hits.extend((loc, result.status_code) for loc in locs if is_career_url(loc))
        child_sitemaps.extend(loc for loc in locs if loc.lower().endswith(".xml"))

    for sitemap_url in dedupe(child_sitemaps)[:max_child_sitemaps]:
        result = await http.get(sitemap_url)
        if not result.status_code or result.status_code >= 400:
            continue
        locs = extract_sitemap_locs(result.text)
        hits.extend((loc, result.status_code) for loc in locs if is_career_url(loc))

    deduped: dict[str, int | None] = {}
    for url, status_code in hits:
        normalized = normalize_url(url, url)
        if normalized:
            deduped[normalized] = status_code
    return list(deduped.items())


def extract_sitemap_locs(xml: str) -> list[str]:
    return [
        re.sub(r"\s+", "", match)
        for match in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml or "", flags=re.I | re.S)
    ]


def is_valid_probe_hit(result: HttpResult) -> bool:
    if not result.status_code or result.status_code >= 400:
        return False
    if re.search(r"\b(page not found|not found|404)\b", result.text[:5000], flags=re.I):
        return False
    text = strip_html(result.text[:30_000]).lower()
    return is_career_url(result.final_url) and has_career_text_signal(text)


def add_surface(
    surfaces: dict[str, dict[str, Any]],
    company: dict[str, Any],
    *,
    url: str,
    url_type: str,
    source: str,
    confidence: float,
    http_status: int | None,
    evidence: str,
    checked_at: datetime,
    raw_json: dict[str, Any] | None = None,
) -> None:
    normalized_url = normalize_url(url, url)
    if not normalized_url:
        return
    existing = surfaces.get(normalized_url)
    if existing and existing["confidence"] >= confidence:
        return
    surfaces[normalized_url] = {
        "company_id": company.get("id"),
        "company_slug": company.get("slug"),
        "company_name": company.get("name"),
        "website": company.get("website"),
        "yc_is_hiring": bool(company.get("is_hiring")),
        "yc_job_count": len(company.get("raw_json", {}).get("jobPostings") or []),
        "url": url,
        "normalized_url": normalized_url,
        "url_type": url_type,
        "source": source,
        "confidence": confidence,
        "http_status": http_status,
        "evidence": evidence[:500],
        "checked_at": checked_at.isoformat(),
        "raw_json": raw_json or {},
    }


def career_link_score(homepage: str, url: str, text: str) -> float:
    parsed_home = urlparse(homepage)
    parsed_url = urlparse(url)
    home_domain = clean_domain(parsed_home.netloc)
    link_domain = clean_domain(parsed_url.netloc)
    combined = f"{url} {text}".lower()
    same_domain = link_domain == home_domain or link_domain.endswith(f".{home_domain}")
    ats = any(domain in link_domain for domain in ATS_DOMAINS)
    career_signal = is_career_url(url)

    if not same_domain and not ats:
        return 0
    if any(term in combined for term in LOW_VALUE_TERMS) and not ats:
        return 0
    if ats and (career_signal or has_career_text_signal(text)):
        return 0.92
    if ats:
        return 0.86
    if career_signal:
        return 0.84
    return 0


def url_type_for(url: str) -> str:
    domain = clean_domain(urlparse(url).netloc)
    if "ycombinator.com" in domain and "/jobs/" in urlparse(url).path:
        return "yc_job"
    if any(ats_domain in domain for ats_domain in ATS_DOMAINS):
        return "ats"
    if "/jobs/" in urlparse(url).path.lower():
        return "jobs_page"
    return "careers_page"


def canonical_homepage(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def normalize_url(base_url: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    parsed = urlparse(urljoin(base_url, href))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", parsed.query, ""))


def is_career_like(value: str) -> bool:
    normalized = value.lower().replace("_", "-")
    return any(term in normalized for term in CAREER_TERMS)


def is_career_url(url: str) -> bool:
    parsed = urlparse(url)
    domain = clean_domain(parsed.netloc)
    if any(ats_domain in domain for ats_domain in ATS_DOMAINS):
        return True
    path = parsed.path.lower().replace("_", "-")
    if any(term in path for term in LOW_VALUE_TERMS):
        return False
    return bool(CAREER_PATH_PATTERN.search(path))


def has_career_text_signal(value: str) -> bool:
    normalized = value.lower().replace("_", " ").replace("-", " ")
    return any(term.replace("-", " ") in normalized for term in CAREER_TERMS)


def clean_domain(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def strip_html(html: str) -> str:
    without_scripts = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    return re.sub(r"\s+", " ", without_tags).strip()


def sorted_surfaces(surfaces: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(surfaces.values(), key=lambda item: (-float(item["confidence"]), item["normalized_url"]))


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def write_json(path: Path, payload: list[dict[str, Any]]) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp_path.replace(path)


def write_csv(path: Path, surfaces: list[dict[str, Any]]) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for surface in surfaces:
            writer.writerow({field: surface.get(field) for field in CSV_FIELDS})
    tmp_path.replace(path)


if __name__ == "__main__":
    main()
