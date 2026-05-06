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
    drop_legacy_career_surfaces_table,
    engine_from_url,
    fetch_career_page_discovery_event_rows,
    fetch_companies_for_discovery,
    fetch_company_career_page_rows,
    fetch_completed_career_discovery_slugs,
    fetch_discovered_url_rows,
    fetch_yc_job_rows,
    replace_career_page_data,
    upsert_career_page_discovery_statuses,
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
    "/podcast/",
    "/product-updates/",
    "/resources/",
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
ATS_JOB_BOARD_DOMAINS = (
    "jobs.ashbyhq.com",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "apply.workable.com",
    "jobs.workable.com",
    "workdayjobs.com",
    "bamboohr.com",
    "recruitee.com",
    "smartrecruiters.com",
    "applytojob.com",
    "app.dover.com",
    "wellfound.com",
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
DISCOVERY_EVENT_CSV_FIELDS = [
    "company_id",
    "company_slug",
    "company_name",
    "website",
    "yc_is_hiring",
    "yc_job_count",
    "url",
    "normalized_url",
    "page_type",
    "discovery_source",
    "confidence",
    "http_status",
    "evidence",
    "checked_at",
]
CAREER_PAGE_CSV_FIELDS = [
    "company_id",
    "company_slug",
    "company_name",
    "website",
    "yc_is_hiring",
    "yc_job_count",
    "career_page_url",
    "normalized_url",
    "page_type",
    "discovery_source",
    "confidence",
    "http_status",
    "evidence",
    "is_primary",
    "observed_source_count",
    "checked_at",
]
DISCOVERED_URL_CSV_FIELDS = [
    "company_id",
    "company_slug",
    "company_name",
    "website",
    "url",
    "normalized_url",
    "url_key",
    "url_kind",
    "discovery_sources",
    "source_event_count",
    "confidence",
    "fetch_priority",
    "http_status",
    "is_primary",
    "is_active",
    "first_seen_at",
    "last_seen_at",
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
        tmp_path = self.cache_path.with_suffix(f"{self.cache_path.suffix}.tmp")
        tmp_path.write_text(json.dumps(self.cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.cache_path)

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self.cache_path.exists():
            return {}
        raw_cache = self.cache_path.read_text(encoding="utf-8")
        if not raw_cache.strip():
            return {}
        try:
            return json.loads(raw_cache)
        except json.JSONDecodeError:
            corrupt_path = self.cache_path.with_suffix(f"{self.cache_path.suffix}.corrupt")
            self.cache_path.replace(corrupt_path)
            return {}


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Discover YC company career/job pages.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-sitemaps", type=int, default=6)
    parser.add_argument("--max-child-sitemaps", type=int, default=8)
    parser.add_argument("--cache-path", type=Path, default=settings.career_url_discovery_cache_path)
    parser.add_argument("--output-csv", type=Path, default=settings.company_career_pages_csv_path)
    parser.add_argument(
        "--discovered-urls-csv",
        type=Path,
        default=settings.discovered_urls_csv_path,
    )
    parser.add_argument(
        "--events-csv",
        type=Path,
        default=settings.career_page_discovery_events_csv_path,
    )
    parser.add_argument("--write-raw-json", action="store_true")
    parser.add_argument(
        "--raw-output-dir",
        type=Path,
        default=settings.local_debug_dir / "career_discovery",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess selected companies even when discovery status is already completed.",
    )
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = engine_from_url(settings.database_url)
    companies = fetch_companies_for_discovery(engine, limit=args.limit)
    selected_slugs = [str(company["slug"]) for company in companies]
    completed_slugs = (
        set()
        if args.force
        else fetch_completed_career_discovery_slugs(engine, company_slugs=selected_slugs)
    )
    pending_companies = pending_discovery_companies(
        companies,
        completed_slugs=completed_slugs,
        force=args.force,
    )
    jobs_by_slug: dict[str, list[dict[str, Any]]] = {}
    for job in fetch_yc_job_rows(engine):
        jobs_by_slug.setdefault(job["company_slug"], []).append(job)

    async with CachedHttpClient(args.cache_path, concurrency=args.concurrency) as http:
        processed_count = 0
        for batch in chunks(pending_companies, max(1, args.batch_size)):
            batch_results = await discover_company_batch(
                batch,
                jobs_by_slug,
                http,
                max_sitemaps=args.max_sitemaps,
                max_child_sitemaps=args.max_child_sitemaps,
            )
            batch_events = [
                event for result in batch_results for event in result["discovery_events"]
            ]
            batch_pages = [page for result in batch_results for page in result["career_pages"]]
            batch_slugs = [str(company["slug"]) for company in batch]
            replace_career_page_data(
                engine,
                batch_events,
                batch_pages,
                company_slugs=batch_slugs,
            )
            upsert_career_page_discovery_statuses(
                engine,
                [discovery_status(result) for result in batch_results],
            )
            processed_count += len(batch)
            http.save()
            print(
                f"Checkpointed {processed_count} / {len(pending_companies)} pending "
                f"companies ({len(completed_slugs)} already completed).",
                flush=True,
            )

    drop_legacy_career_surfaces_table(engine)
    discovery_events = fetch_career_page_discovery_event_rows(engine, company_slugs=selected_slugs)
    career_pages = fetch_company_career_page_rows(engine, company_slugs=selected_slugs)
    discovered_urls = fetch_discovered_url_rows(engine, company_slugs=selected_slugs)
    write_csv(args.output_csv, career_pages, CAREER_PAGE_CSV_FIELDS)
    write_csv(args.discovered_urls_csv, discovered_urls, DISCOVERED_URL_CSV_FIELDS)
    write_csv(args.events_csv, discovery_events, DISCOVERY_EVENT_CSV_FIELDS)
    if args.write_raw_json:
        write_json(args.raw_output_dir / "company_career_pages_raw.json", career_pages)
        write_json(args.raw_output_dir / "discovered_urls_raw.json", discovered_urls)
        write_json(
            args.raw_output_dir / "career_page_discovery_events_raw.json",
            discovery_events,
        )

    print(f"Selected {len(companies)} companies.")
    print(f"Skipped {len(completed_slugs)} already completed companies.")
    print(f"Checked {len(pending_companies)} pending companies.")
    print(f"Recorded {len(discovery_events)} career page discovery events.")
    print(f"Wrote {len(career_pages)} canonical company career pages.")
    print(f"Queued {len(discovered_urls)} discovered URLs for fetch/classification.")
    print(f"Wrote {args.output_csv}")
    print(f"Wrote {args.discovered_urls_csv}")
    print(f"Wrote {args.events_csv}")
    if args.write_raw_json:
        print(f"Wrote raw JSON debug files under {args.raw_output_dir}")
    print(f"Updated {settings.database_url}")


def pending_discovery_companies(
    companies: list[dict[str, Any]],
    *,
    completed_slugs: set[str],
    force: bool,
) -> list[dict[str, Any]]:
    if force:
        return companies
    return [company for company in companies if str(company["slug"]) not in completed_slugs]


async def discover_company_batch(
    companies: list[dict[str, Any]],
    jobs_by_slug: dict[str, list[dict[str, Any]]],
    http: CachedHttpClient,
    *,
    max_sitemaps: int,
    max_child_sitemaps: int,
) -> list[dict[str, Any]]:
    tasks = [
        discover_company_result(
            company,
            jobs_by_slug.get(company["slug"], []),
            http,
            max_sitemaps=max_sitemaps,
            max_child_sitemaps=max_child_sitemaps,
        )
        for company in companies
    ]
    return await asyncio.gather(*tasks)


async def discover_company_result(
    company: dict[str, Any],
    yc_jobs: list[dict[str, Any]],
    http: CachedHttpClient,
    *,
    max_sitemaps: int,
    max_child_sitemaps: int,
) -> dict[str, Any]:
    try:
        result = await discover_company_career_data(
            company,
            yc_jobs,
            http,
            max_sitemaps=max_sitemaps,
            max_child_sitemaps=max_child_sitemaps,
        )
        return {
            "company": company,
            "discovery_events": result["discovery_events"],
            "career_pages": result["career_pages"],
            "error": None,
        }
    except Exception as exc:
        return {
            "company": company,
            "discovery_events": [],
            "career_pages": [],
            "error": str(exc),
        }


def discovery_status(result: dict[str, Any]) -> dict[str, Any]:
    company = result["company"]
    error = result.get("error")
    return {
        "company_id": company.get("id"),
        "company_slug": company.get("slug"),
        "company_name": company.get("name"),
        "website": company.get("website"),
        "status": "failed" if error else "completed",
        "discovery_event_count": len(result["discovery_events"]),
        "career_page_count": len(result["career_pages"]),
        "error": error,
        "checked_at": datetime.now(UTC).isoformat(),
    }


async def discover_company_career_data(
    company: dict[str, Any],
    yc_jobs: list[dict[str, Any]],
    http: CachedHttpClient,
    *,
    max_sitemaps: int,
    max_child_sitemaps: int,
) -> dict[str, list[dict[str, Any]]]:
    checked_at = datetime.now(UTC)
    discovery_events: list[dict[str, Any]] = []

    for job in yc_jobs:
        absolute_url = job.get("absolute_url") or urljoin(
            "https://www.ycombinator.com", job.get("url") or ""
        )
        add_discovery_event(
            discovery_events,
            company,
            url=absolute_url,
            page_type="yc_job",
            discovery_source="yc_job_posting",
            confidence=1.0,
            http_status=None,
            evidence=f"{job.get('title', '')} | {job.get('location', '')} | {job.get('visa', '')}",
            checked_at=checked_at,
            raw_json=job,
        )

    website = canonical_homepage(company.get("website"))
    if not website:
        return {
            "discovery_events": sorted_discovery_events(discovery_events),
            "career_pages": build_company_career_pages(discovery_events),
        }

    homepage = await http.get(website)
    for href, text in extract_homepage_links(homepage.text):
        url = normalize_url(website, href)
        if not url:
            continue
        score = career_link_score(website, url, text)
        if score <= 0:
            continue
        add_discovery_event(
            discovery_events,
            company,
            url=url,
            page_type=page_type_for(url),
            discovery_source="homepage_link",
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
        add_discovery_event(
            discovery_events,
            company,
            url=url,
            page_type=page_type_for(url),
            discovery_source="sitemap",
            confidence=0.78,
            http_status=status_code,
            evidence="career-like URL in sitemap",
            checked_at=checked_at,
        )

    if not has_external_career_event(discovery_events):
        for path in COMMON_PATHS:
            probe_url = urljoin(website.rstrip("/") + "/", path.lstrip("/"))
            result = await http.get(probe_url)
            if is_valid_probe_hit(result):
                add_discovery_event(
                    discovery_events,
                    company,
                    url=result.final_url,
                    page_type=page_type_for(result.final_url),
                    discovery_source="common_path_probe",
                    confidence=0.65,
                    http_status=result.status_code,
                    evidence=path,
                    checked_at=checked_at,
                )

    return {
        "discovery_events": sorted_discovery_events(discovery_events),
        "career_pages": build_company_career_pages(discovery_events),
    }


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
    if not is_allowed_career_destination(result.url, result.final_url):
        return False
    text = strip_html(result.text[:30_000]).lower()
    return is_career_url(result.final_url) and has_career_text_signal(text)


def add_discovery_event(
    discovery_events: list[dict[str, Any]],
    company: dict[str, Any],
    *,
    url: str,
    page_type: str,
    discovery_source: str,
    confidence: float,
    http_status: int | None,
    evidence: str,
    checked_at: datetime,
    raw_json: dict[str, Any] | None = None,
) -> None:
    normalized_url = normalize_url(url, url)
    if not normalized_url:
        return
    event = {
        "company_id": company.get("id"),
        "company_slug": company.get("slug"),
        "company_name": company.get("name"),
        "website": company.get("website"),
        "yc_is_hiring": bool(company.get("is_hiring")),
        "yc_job_count": len(company.get("raw_json", {}).get("jobPostings") or []),
        "url": url,
        "normalized_url": normalized_url,
        "page_type": page_type,
        "discovery_source": discovery_source,
        "confidence": confidence,
        "http_status": http_status,
        "evidence": evidence[:500],
        "checked_at": checked_at.isoformat(),
        "raw_json": raw_json or {},
    }
    if discovery_event_key(event) in {
        discovery_event_key(existing) for existing in discovery_events
    }:
        return
    discovery_events.append(event)


def build_company_career_pages(discovery_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in discovery_events:
        if not is_external_career_event(event):
            continue
        grouped.setdefault(career_page_dedupe_key(str(event["normalized_url"])), []).append(event)

    pages: list[dict[str, Any]] = []
    for events in grouped.values():
        best = max(events, key=lambda event: float(event["confidence"]))
        sources = sorted({str(event["discovery_source"]) for event in events})
        observed_urls = sorted({str(event["normalized_url"]) for event in events})
        pages.append(
            {
                "company_id": best.get("company_id"),
                "company_slug": best.get("company_slug"),
                "company_name": best.get("company_name"),
                "website": best.get("website"),
                "yc_is_hiring": best.get("yc_is_hiring"),
                "yc_job_count": best.get("yc_job_count"),
                "career_page_url": best.get("url"),
                "normalized_url": best.get("normalized_url"),
                "page_type": best.get("page_type"),
                "discovery_source": best.get("discovery_source"),
                "confidence": best.get("confidence"),
                "http_status": best.get("http_status"),
                "evidence": best.get("evidence"),
                "is_primary": False,
                "observed_source_count": len(sources),
                "checked_at": best.get("checked_at"),
                "raw_json": {
                    "event_count": len(events),
                    "discovery_sources": sources,
                    "observed_urls": observed_urls,
                },
            }
        )

    pages = sorted(
        pages,
        key=lambda page: (
            -float(page["confidence"]),
            0 if page["page_type"] == "ats" else 1,
            str(page["normalized_url"]),
        ),
    )
    if pages:
        pages[0]["is_primary"] = True
    return pages


def is_external_career_event(event: dict[str, Any]) -> bool:
    if event.get("page_type") == "yc_job":
        return False
    domain = clean_domain(urlparse(str(event.get("normalized_url") or "")).netloc)
    return "ycombinator.com" not in domain


def discovery_event_key(event: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(event.get("normalized_url") or ""),
        str(event.get("page_type") or ""),
        str(event.get("discovery_source") or ""),
        re.sub(r"\s+", " ", str(event.get("evidence") or "")).strip().lower(),
    )


def has_external_career_event(events: list[dict[str, Any]]) -> bool:
    return any(is_external_career_event(event) for event in events)


def career_link_score(homepage: str, url: str, text: str) -> float:
    parsed_home = urlparse(homepage)
    parsed_url = urlparse(url)
    home_domain = clean_domain(parsed_home.netloc)
    link_domain = clean_domain(parsed_url.netloc)
    combined = f"{url} {text}".lower()
    same_domain = link_domain == home_domain or link_domain.endswith(f".{home_domain}")
    ats = is_ats_job_board_url(url)
    career_signal = is_career_url(url)

    if not same_domain and not ats:
        return 0
    if any(term in combined for term in LOW_VALUE_TERMS):
        return 0
    if ats and (career_signal or has_career_text_signal(text)):
        return 0.92
    if ats:
        return 0.86
    if career_signal:
        return 0.84
    return 0


def is_allowed_career_destination(source_url: str, destination_url: str) -> bool:
    source_domain = clean_domain(urlparse(source_url).netloc)
    destination_domain = clean_domain(urlparse(destination_url).netloc)
    if destination_domain == source_domain or destination_domain.endswith(f".{source_domain}"):
        return True
    return is_ats_job_board_url(destination_url)


def career_page_dedupe_key(url: str) -> str:
    parsed = urlparse(url)
    domain = clean_domain(parsed.netloc)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(("https", domain, path, "", parsed.query, ""))


def page_type_for(url: str) -> str:
    domain = clean_domain(urlparse(url).netloc)
    if "ycombinator.com" in domain and "/jobs/" in urlparse(url).path:
        return "yc_job"
    if is_ats_job_board_url(url):
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
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", parsed.query, "")
    )


def is_career_like(value: str) -> bool:
    normalized = value.lower().replace("_", "-")
    return any(term in normalized for term in CAREER_TERMS)


def is_career_url(url: str) -> bool:
    parsed = urlparse(url)
    if is_ats_job_board_url(url):
        return True
    path = parsed.path.lower().replace("_", "-")
    if any(term in path for term in LOW_VALUE_TERMS):
        return False
    return bool(CAREER_PATH_PATTERN.search(path))


def is_ats_job_board_url(url: str) -> bool:
    domain = clean_domain(urlparse(url).netloc)
    return any(
        domain == ats_domain or domain.endswith(f".{ats_domain}")
        for ats_domain in ATS_JOB_BOARD_DOMAINS
    )


def has_career_text_signal(value: str) -> bool:
    normalized = value.lower().replace("_", " ").replace("-", " ")
    return any(term.replace("-", " ") in normalized for term in CAREER_TERMS)


def clean_domain(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def strip_html(html: str) -> str:
    without_scripts = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    return re.sub(r"\s+", " ", without_tags).strip()


def sorted_discovery_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda item: (
            str(item["company_slug"]),
            -float(item["confidence"]),
            str(item["normalized_url"]),
            str(item["discovery_source"]),
        ),
    )


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def chunks(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def write_json(path: Path, payload: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    tmp_path.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    tmp_path.replace(path)


if __name__ == "__main__":
    main()
