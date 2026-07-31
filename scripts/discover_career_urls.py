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
from yc_radar.services.http_cache import DiskHttpCache
from yc_radar.services.run_status import (
    read_status,
    stage_checkpoint,
    stage_finished,
    stage_started,
    write_status,
)
from yc_radar.services.source_providers import is_ats_domain, is_company_ats_url
from yc_radar.services.url_quality import canonical_url_key, normalize_url
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
    url_inventory_writer_lock,
)

MAX_STORED_TEXT_CHARS = 500_000
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

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
MAX_LINKED_CAREER_PAGES = 3
DISCOVERY_USER_AGENT = (
    "yc-radar/0.2 (+https://github.com/Daniishkhan/yc-radar; "
    "read-only-career-discovery)"
)
DISCOVERY_EVENT_CSV_FIELDS = [
    "company_id",
    "company_slug",
    "company_name",
    "website",
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
    error_class: str | None = None
    attempt_count: int = 0
    retryable: bool = False
    cache_source: str = "network"


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
    """Discovery request policy over the shared bounded disk cache."""

    def __init__(
        self,
        cache_path: Path | None,
        *,
        concurrency: int,
        host_concurrency: int = 2,
        max_attempts: int = 3,
        cache_dir: Path | None = None,
        legacy_cache_path: Path | None = None,
        bypass_cache: bool = False,
    ) -> None:
        if cache_path and cache_path.suffix == ".json":
            cache_dir = cache_path.with_suffix("")
            legacy_cache_path = cache_path
        elif cache_path:
            cache_dir = cache_path
        if cache_dir is None:
            raise ValueError("cache_dir or cache_path is required")
        self.cache = DiskHttpCache(cache_dir, legacy_path=legacy_cache_path)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.host_concurrency = host_concurrency
        self.max_attempts = max(1, max_attempts)
        self.bypass_cache = bypass_cache
        self._host_semaphores = [asyncio.Semaphore(host_concurrency) for _ in range(64)]
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": DISCOVERY_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.8",
            },
            follow_redirects=True,
            timeout=10,
        )

    async def __aenter__(self) -> "CachedHttpClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.client.aclose()

    @property
    def cache_metrics(self) -> dict[str, int]:
        return dict(self.cache.metrics)

    async def get(self, url: str, *, bypass_cache: bool = False) -> HttpResult:
        if not bypass_cache and not self.bypass_cache:
            cached = self.cache.load(url)
            if cached is not None:
                return HttpResult(
                    url=url,
                    final_url=str(cached.get("final_url") or url),
                    status_code=cached.get("status_code"),
                    content_type=str(cached.get("content_type") or ""),
                    text=str(cached.get("text") or ""),
                    error=cached.get("error"),
                    error_class=cached.get("error_class"),
                    attempt_count=int(cached.get("attempt_count") or 0),
                    retryable=bool(cached.get("retryable")),
                    cache_source="disk",
                )
        host_index = self.host_semaphore_index(url, len(self._host_semaphores))
        async with self.semaphore, self._host_semaphores[host_index]:
            return await self._get_uncached(url)

    @staticmethod
    def host_semaphore_index(url: str, stripe_count: int) -> int:
        """Return a bounded semaphore stripe from a normalized origin, not a path."""
        parsed = urlparse(url)
        scheme = parsed.scheme.lower() or "https"
        hostname = (parsed.hostname or "").lower().removeprefix("www.")
        try:
            port = parsed.port
        except ValueError:
            port = None
        default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
        origin = f"{scheme}://{hostname}" + (f":{port}" if port and port != default_port else "")
        return int(DiskHttpCache.key_for_url(origin)[:2], 16) % stripe_count

    async def _get_uncached(self, url: str) -> HttpResult:
        result = HttpResult(url, url, None, "", "")
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self.client.get(url)
                retryable = response.status_code in RETRYABLE_STATUS_CODES
                result = HttpResult(
                    url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    text=response.text[:MAX_STORED_TEXT_CHARS],
                    error_class=(
                        "RetryableHttpStatus"
                        if retryable
                        else "HttpStatusError" if response.status_code >= 400 else None
                    ),
                    attempt_count=attempt,
                    retryable=retryable,
                )
            except httpx.RequestError as exc:
                retryable = not isinstance(exc, httpx.TooManyRedirects)
                result = HttpResult(
                    url=url,
                    final_url=url,
                    status_code=None,
                    content_type="",
                    text="",
                    error=str(exc),
                    error_class=type(exc).__name__,
                    attempt_count=attempt,
                    retryable=retryable,
                )
                if retryable and attempt < self.max_attempts:
                    await self._sleep_before_retry(None, attempt)
                    continue
                break
            if not result.retryable or attempt == self.max_attempts:
                break
            await self._sleep_before_retry(response, attempt)
        self.cache.store(url, metadata=result.__dict__, text=result.text)
        return result

    async def get_many(self, urls: list[str]) -> list[HttpResult]:
        return await asyncio.gather(*(self.get(url) for url in urls))

    async def _sleep_before_retry(self, response: httpx.Response | None, attempt: int) -> None:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        try:
            delay = float(retry_after) if retry_after else 0.0
        except ValueError:
            delay = 0.0
        if delay <= 0 or delay > 30:
            delay = min(2.0 ** (attempt - 1), 8.0)
        await asyncio.sleep(delay)

    def save(self) -> None:
        """Compatibility no-op; every cache entry is atomically persisted at fetch time."""


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Discover career/job pages for neutral companies.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--company-slug",
        action="append",
        default=[],
        help="Process only this company slug; repeat the option for multiple companies.",
    )
    parser.add_argument(
        "--hiring-only",
        action="store_true",
        help="Only inspect companies currently marked as hiring in their YC profile.",
    )
    parser.add_argument(
        "--source-provider",
        help="Optionally limit companies to a directory source such as yc; defaults to all.",
    )
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--host-concurrency", type=int, default=2)
    parser.add_argument("--max-http-attempts", type=int, default=3)
    parser.add_argument("--max-sitemaps", type=int, default=6)
    parser.add_argument("--max-child-sitemaps", type=int, default=8)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=settings.career_url_discovery_cache_dir,
        help="Directory for atomic per-URL cache entries.",
    )
    parser.add_argument(
        "--legacy-cache-path",
        type=Path,
        default=settings.career_url_discovery_cache_path,
        help="Read-only legacy JSON cache fallback; it is never removed automatically.",
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        default=None,
        help="Deprecated compatibility path; a .json path is used as legacy input.",
    )
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
    parser.add_argument("--status-file", type=Path, help="Atomic local stage-status JSON output.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except BaseException as exc:
        status_file = getattr(args, "status_file", None)
        write_status(
            status_file,
            stage_finished(
                read_status(status_file) or stage_started("discovery"),
                state="failed",
                error=exc,
            ),
        )
        raise


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    status = stage_started("discovery")
    write_status(getattr(args, "status_file", None), status)
    engine = engine_from_url(settings.database_url)
    with url_inventory_writer_lock(engine):
        await _run_with_inventory_lock(args, settings, status, engine)


async def _run_with_inventory_lock(
    args: argparse.Namespace,
    settings: Any,
    status: dict[str, Any],
    engine: Any,
) -> None:
    requested_slugs = sorted(
        {
            str(slug).strip().lower()
            for slug in getattr(args, "company_slug", [])
            if slug
        }
    )
    if requested_slugs:
        known = fetch_companies_for_discovery(
            engine,
            company_slugs=requested_slugs,
            hiring_only=getattr(args, "hiring_only", False),
            source_provider=getattr(args, "source_provider", None),
        )
        missing_slugs = sorted(set(requested_slugs) - {str(row["slug"]) for row in known})
        if missing_slugs:
            raise SystemExit(f"Unknown company slug(s): {', '.join(missing_slugs)}")
    companies = fetch_companies_for_discovery(
        engine,
        limit=args.limit,
        company_slugs=requested_slugs or None,
        only_pending=not args.force,
        hiring_only=getattr(args, "hiring_only", False),
        source_provider=getattr(args, "source_provider", None),
    )
    selected_slugs = [str(company["slug"]) for company in companies]
    completed_slugs = set() if args.force else fetch_completed_career_discovery_slugs(engine)
    pending_companies = companies
    if not pending_companies:
        print("No pending companies need career URL discovery; existing snapshots were preserved.")
        write_status(
            getattr(args, "status_file", None),
            stage_finished(
                status,
                state="completed",
                selected=0,
                processed=0,
                succeeded=0,
                failed=0,
            ),
        )
        return

    jobs_by_slug: dict[str, list[dict[str, Any]]] = {}
    for job in fetch_yc_job_rows(engine):
        jobs_by_slug.setdefault(job["company_slug"], []).append(job)

    cache_path = getattr(args, "cache_path", None)
    cache_dir = getattr(args, "cache_dir", None)
    legacy_cache_path = getattr(args, "legacy_cache_path", None)
    success_count = 0
    failure_count = 0
    error_classes: dict[str, int] = {}
    retry_count = 0
    cache_metrics: dict[str, int] = {}
    processed_count = 0
    write_status(
        getattr(args, "status_file", None),
        stage_checkpoint(
            status,
            selected=len(companies),
            processed=0,
            succeeded=0,
            failed=0,
            error_classes=error_classes,
            retry_count=0,
        ),
    )
    async with CachedHttpClient(
        cache_path or cache_dir,
        concurrency=args.concurrency,
        host_concurrency=args.host_concurrency,
        max_attempts=args.max_http_attempts,
        cache_dir=None if cache_path else cache_dir,
        legacy_cache_path=None if cache_path else legacy_cache_path,
        bypass_cache=bool(args.force),
    ) as http:
        for batch in chunks(pending_companies, max(1, args.batch_size)):
            batch_results = await discover_company_batch(
                batch,
                jobs_by_slug,
                http,
                max_sitemaps=args.max_sitemaps,
                max_child_sitemaps=args.max_child_sitemaps,
            )
            successful = [result for result in batch_results if result["applicable"]]
            failed = [result for result in batch_results if not result["applicable"]]
            if successful:
                replace_career_page_data(
                    engine,
                    [event for result in successful for event in result["discovery_events"]],
                    [page for result in successful for page in result["career_pages"]],
                    company_slugs=[str(result["company"]["slug"]) for result in successful],
                    statuses=[discovery_status(result) for result in successful],
                )
            if failed:
                # Failure checkpoints intentionally retain prior events/pages/queue rows.
                upsert_career_page_discovery_statuses(
                    engine, [discovery_status(result) for result in failed]
                )
            success_count += len(successful)
            failure_count += len(failed)
            for result in batch_results:
                if result.get("error_class"):
                    key = str(result["error_class"])
                    error_classes[key] = error_classes.get(key, 0) + 1
                retry_count += int(result.get("retry_count") or 0)
                for warning in result.get("warnings", []):
                    if warning.get("error_class"):
                        key = str(warning["error_class"])
                        error_classes[key] = error_classes.get(key, 0) + 1
                    retry_count += max(0, int(warning.get("attempt_count") or 0) - 1)
            processed_count += len(batch)
            write_status(
                getattr(args, "status_file", None),
                stage_checkpoint(
                    status,
                    cache=http.cache_metrics,
                    selected=len(companies),
                    processed=processed_count,
                    succeeded=success_count,
                    failed=failure_count,
                    error_classes=error_classes,
                    retry_count=retry_count,
                ),
            )
            print(
                f"Checkpointed {processed_count} / {len(pending_companies)} pending "
                f"companies ({len(completed_slugs)} already completed).",
                flush=True,
            )
        cache_metrics = http.cache_metrics

    drop_legacy_career_surfaces_table(engine)
    discovery_events = (
        fetch_career_page_discovery_event_rows(engine, company_slugs=selected_slugs)
        if selected_slugs
        else []
    )
    career_pages = (
        fetch_company_career_page_rows(engine, company_slugs=selected_slugs) if selected_slugs else []
    )
    discovered_urls = (
        fetch_discovered_url_rows(engine, company_slugs=selected_slugs) if selected_slugs else []
    )
    write_csv(args.output_csv, career_pages, CAREER_PAGE_CSV_FIELDS)
    write_csv(args.discovered_urls_csv, discovered_urls, DISCOVERED_URL_CSV_FIELDS)
    write_csv(args.events_csv, discovery_events, DISCOVERY_EVENT_CSV_FIELDS)
    if args.write_raw_json:
        write_json(args.raw_output_dir / "company_career_pages_raw.json", career_pages)
        write_json(args.raw_output_dir / "discovered_urls_raw.json", discovered_urls)
        write_json(args.raw_output_dir / "career_page_discovery_events_raw.json", discovery_events)

    print(f"Selected {len(companies)} pending companies.")
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
    write_status(
        getattr(args, "status_file", None),
        stage_finished(
            status,
            state="completed" if failure_count == 0 else "partial",
            cache=cache_metrics,
            selected=len(companies),
            processed=processed_count,
            succeeded=success_count,
            failed=failure_count,
            error_classes=error_classes,
            retry_count=retry_count,
        ),
    )


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
        failure = result.get("failure")
        return {
            "company": company,
            "discovery_events": result["discovery_events"] if not failure else [],
            "career_pages": result["career_pages"] if not failure else [],
            "applicable": failure is None,
            "error": failure.get("message") if failure else None,
            "error_class": failure.get("class") if failure else None,
            "retry_count": max(0, int(failure.get("attempt_count") or 0) - 1) if failure else 0,
            "warnings": result.get("warnings", []),
        }
    except Exception as exc:
        return {
            "company": company,
            "discovery_events": [],
            "career_pages": [],
            "applicable": False,
            "error": str(exc),
            "error_class": type(exc).__name__,
            "retry_count": 0,
            "warnings": [],
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
        "raw_json": {
            "error_class": result.get("error_class"),
            "warnings": result.get("warnings", []),
        },
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
    discovery_event_keys: set[tuple[str, str, str, str]] = set()

    for job in yc_jobs:
        absolute_url = job.get("absolute_url") or urljoin(
            "https://www.ycombinator.com", job.get("url") or ""
        )
        add_discovery_event(
            discovery_events,
            discovery_event_keys,
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
    if (
        homepage.error
        or homepage.status_code is None
        or homepage.retryable
        or homepage.status_code >= 400
    ):
        return {
            "discovery_events": [],
            "career_pages": [],
            "warnings": [],
            "failure": {
                "class": homepage.error_class or "RetryableHttpStatus",
                "message": homepage.error or f"HTTP {homepage.status_code}",
                "attempt_count": homepage.attempt_count,
            },
        }
    warnings: list[dict[str, Any]] = []
    homepage_url = homepage.final_url or website
    for href, text in extract_homepage_links(homepage.text):
        url = normalize_url(homepage_url, href)
        if not url:
            continue
        score = career_link_score(homepage_url, url, text)
        if score <= 0:
            continue
        add_discovery_event(
            discovery_events,
            discovery_event_keys,
            company,
            url=url,
            page_type=page_type_for(url),
            discovery_source="homepage_link",
            confidence=score,
            http_status=homepage.status_code,
            evidence=text or href,
            checked_at=checked_at,
        )

    has_high_confidence_ats = any(
        event.get("page_type") == "ats" and float(event.get("confidence") or 0) >= 0.86
        for event in discovery_events
    )
    if not has_high_confidence_ats:
        robots_task = asyncio.create_task(
            discover_robots_sitemap_urls(website, http, warnings=warnings)
        )
        candidate_sitemap_results = await http.get_many(
            [urljoin(website.rstrip("/") + "/", path.lstrip("/")) for path in SITEMAP_CANDIDATES]
        )
        robots_sitemap_urls = await robots_task
        sitemap_urls = dedupe(robots_sitemap_urls)[:max_sitemaps]
        warnings.extend(
            http_warning(result)
            for result in candidate_sitemap_results
            if result.error or result.retryable
        )
        successful_candidate_sitemaps = [
            result
            for result in candidate_sitemap_results
            if result.status_code and result.status_code < 400
        ]
        sitemap_hits = await discover_sitemap_hits(
            sitemap_urls,
            http,
            successful_results=successful_candidate_sitemaps,
            max_child_sitemaps=max_child_sitemaps,
            warnings=warnings,
        )
        for url, status_code in sitemap_hits:
            add_discovery_event(
                discovery_events,
                discovery_event_keys,
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
        probe_results = await http.get_many(
            [
                urljoin(website.rstrip("/") + "/", path.lstrip("/"))
                for path in COMMON_PATHS
            ]
        )
        homepage_signature = page_content_signature(homepage.text)
        seen_probe_signatures: set[str] = set()
        for path, result in zip(COMMON_PATHS, probe_results, strict=True):
            if result.error or result.retryable:
                warnings.append(http_warning(result))
            if not is_valid_probe_hit(result):
                continue
            signature = page_content_signature(result.text)
            if signature and (
                signature == homepage_signature or signature in seen_probe_signatures
            ):
                continue
            if signature:
                seen_probe_signatures.add(signature)
            add_discovery_event(
                discovery_events,
                discovery_event_keys,
                company,
                url=result.final_url,
                page_type=page_type_for(result.final_url),
                discovery_source="common_path_probe",
                confidence=0.65,
                http_status=result.status_code,
                evidence=path,
                checked_at=checked_at,
            )

    await discover_linked_ats_events(
        company,
        discovery_events,
        discovery_event_keys,
        http,
        checked_at=checked_at,
        max_pages=MAX_LINKED_CAREER_PAGES,
    )

    return {
        "discovery_events": sorted_discovery_events(discovery_events),
        "career_pages": build_company_career_pages(discovery_events),
        "warnings": warnings,
        "failure": None,
    }


async def discover_linked_ats_events(
    company: dict[str, Any],
    discovery_events: list[dict[str, Any]],
    discovery_event_keys: set[tuple[str, str, str, str]],
    http: CachedHttpClient,
    *,
    checked_at: datetime,
    max_pages: int,
) -> None:
    """Follow a few known career pages once to capture linked public ATS boards."""
    candidates: dict[str, dict[str, Any]] = {}
    for event in discovery_events:
        if not is_external_career_event(event) or event.get("page_type") == "ats":
            continue
        normalized_url = str(event.get("normalized_url") or "")
        if not normalized_url:
            continue
        existing = candidates.get(normalized_url)
        if existing is None or float(event["confidence"]) > float(existing["confidence"]):
            candidates[normalized_url] = event

    ordered = sorted(
        candidates.values(),
        key=lambda event: (
            0 if event.get("page_type") == "careers_page" else 1,
            -float(event.get("confidence") or 0),
            str(event.get("normalized_url") or ""),
        ),
    )
    for event in ordered[:max_pages]:
        result = await http.get(str(event["normalized_url"]))
        if not result.status_code or result.status_code >= 400:
            continue
        for href, text in extract_homepage_links(result.text):
            url = normalize_url(result.final_url, href)
            if not url or page_type_for(url) != "ats":
                continue
            add_discovery_event(
                discovery_events,
                discovery_event_keys,
                company,
                url=url,
                page_type="ats",
                discovery_source="career_page_link",
                confidence=0.9,
                http_status=result.status_code,
                evidence=text or href,
                checked_at=checked_at,
            )


def extract_homepage_links(html: str) -> list[tuple[str, str]]:
    parser = AnchorParser()
    parser.feed(html or "")
    return parser.anchors


def http_warning(result: HttpResult) -> dict[str, Any]:
    return {
        "url": result.url,
        "status_code": result.status_code,
        "error_class": result.error_class,
        "message": result.error,
        "retryable": result.retryable,
        "attempt_count": result.attempt_count,
    }


async def discover_robots_sitemap_urls(
    homepage: str,
    http: CachedHttpClient,
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> list[str]:
    robots = await http.get(urljoin(homepage.rstrip("/") + "/", "robots.txt"))
    if (robots.error or robots.retryable) and warnings is not None:
        warnings.append(http_warning(robots))
    if not robots.status_code or robots.status_code >= 400:
        return []
    urls: list[str] = []
    for line in robots.text.splitlines():
        if line.lower().startswith("sitemap:"):
            sitemap_url = line.split(":", 1)[1].strip()
            normalized = normalize_url(robots.final_url or homepage, sitemap_url)
            if normalized:
                urls.append(normalized)
    return urls


async def discover_sitemap_urls(
    homepage: str,
    http: CachedHttpClient,
    *,
    max_sitemaps: int,
) -> list[str]:
    urls = await discover_robots_sitemap_urls(homepage, http)
    for path in SITEMAP_CANDIDATES:
        urls.append(urljoin(homepage.rstrip("/") + "/", path.lstrip("/")))
    return dedupe(urls)[:max_sitemaps]


async def discover_sitemap_hits(
    sitemap_urls: list[str],
    http: CachedHttpClient,
    *,
    successful_results: list[HttpResult] | None = None,
    max_child_sitemaps: int,
    warnings: list[dict[str, Any]] | None = None,
) -> list[tuple[str, int | None]]:
    hits: list[tuple[str, int | None]] = []
    child_sitemaps: list[str] = []
    parent_results = list(successful_results or [])
    already_fetched = {
        normalized
        for result in parent_results
        for value in (result.url, result.final_url)
        if (normalized := normalize_url(value, value))
    }
    unfetched_sitemap_urls = [
        url
        for url in sitemap_urls
        if (normalized := normalize_url(url, url)) and normalized not in already_fetched
    ]
    fetched_parent_results = await http.get_many(unfetched_sitemap_urls)
    if warnings is not None:
        warnings.extend(
            http_warning(result)
            for result in fetched_parent_results
            if result.error or result.retryable
        )
    parent_results.extend(
        result
        for result in fetched_parent_results
        if result.status_code and result.status_code < 400
    )
    for result in parent_results:
        if (result.error or result.retryable) and warnings is not None:
            warnings.append(http_warning(result))
        locs = [
            normalized
            for loc in extract_sitemap_locs(result.text)
            if (normalized := normalize_url(result.final_url or result.url, loc))
        ]
        hits.extend((loc, result.status_code) for loc in locs if is_career_url(loc))
        child_sitemaps.extend(loc for loc in locs if urlparse(loc).path.lower().endswith(".xml"))

    child_results = await http.get_many(dedupe(child_sitemaps)[:max_child_sitemaps])
    for result in child_results:
        if (result.error or result.retryable) and warnings is not None:
            warnings.append(http_warning(result))
        if not result.status_code or result.status_code >= 400:
            continue
        locs = [
            normalized
            for loc in extract_sitemap_locs(result.text)
            if (normalized := normalize_url(result.final_url or result.url, loc))
        ]
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
    discovery_event_keys: set[tuple[str, str, str, str]],
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
    event_key = discovery_event_key(event)
    if event_key in discovery_event_keys:
        return
    discovery_event_keys.add(event_key)
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
    homepage_is_ats_vendor = is_ats_domain(home_domain) and not is_company_ats_url(homepage)
    same_domain = (
        not homepage_is_ats_vendor
        and (link_domain == home_domain or link_domain.endswith(f".{home_domain}"))
    )
    ats = is_company_ats_url(url)
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
    return is_company_ats_url(destination_url)


def career_page_dedupe_key(url: str) -> str:
    return canonical_url_key(url) or url


def page_type_for(url: str) -> str:
    domain = clean_domain(urlparse(url).netloc)
    if "ycombinator.com" in domain and "/jobs/" in urlparse(url).path:
        return "yc_job"
    if is_ats_domain(domain):
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


def is_career_like(value: str) -> bool:
    normalized = value.lower().replace("_", "-")
    return any(term in normalized for term in CAREER_TERMS)


def is_career_url(url: str) -> bool:
    parsed = urlparse(url)
    if is_company_ats_url(url):
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


def page_content_signature(html: str) -> str:
    return strip_html((html or "")[:30_000]).lower()


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
        writer = csv.DictWriter(file, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    tmp_path.replace(path)


if __name__ == "__main__":
    main()
