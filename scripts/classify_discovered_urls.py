#!/usr/bin/env python3
"""Fetch discovered URLs, persist source documents, and classify page intent."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import html
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from yc_radar.core.config import get_settings
from yc_radar.services.candidate_fit import classify_role_text
from yc_radar.services.http_cache import DiskHttpCache
from yc_radar.services.run_status import (
    read_status,
    stage_checkpoint,
    stage_finished,
    stage_started,
    write_status,
)
from yc_radar.services.source_providers import is_ats_domain
from yc_radar.services.database import (
    create_schema,
    engine_from_url,
    fetch_discovered_url_rows,
    fetch_page_classification_rows,
    fetch_source_document_rows_connection,
    upsert_external_job_postings_connection,
    upsert_page_classifications_connection,
    upsert_source_documents_connection,
    url_inventory_writer_lock,
)

SOURCE_TYPE = "career_url"
PARSER_NAME = "deterministic_page_classifier"
PARSER_VERSION = "2026-05-07"
MAX_STORED_TEXT_CHARS = 500_000

GENERIC_CAREER_SEGMENTS = {
    "career",
    "careers",
    "job",
    "jobs",
    "join",
    "join-us",
    "join-our-team",
    "work-with-us",
    "open-positions",
    "open-roles",
    "openings",
    "positions",
}
CAREER_MARKERS = (
    "careers",
    "jobs",
    "join our team",
    "open roles",
    "open positions",
    "current openings",
    "we're hiring",
    "we are hiring",
)
LISTING_MARKERS = (
    "view all jobs",
    "all open roles",
    "all open positions",
    "current openings",
    "job openings",
    "open positions",
    "open roles",
    "filter by department",
)
DETAIL_MARKERS = (
    "apply now",
    "apply for this job",
    "submit application",
    "about the role",
    "about this role",
    "responsibilities",
    "requirements",
    "qualifications",
    "compensation",
    "salary range",
)
ROLE_TERMINALS = (
    "engineer",
    "developer",
    "architect",
    "scientist",
    "designer",
    "manager",
    "lead",
    "analyst",
    "specialist",
    "representative",
    "executive",
    "operator",
    "recruiter",
)
ROLE_URL_TERMS = ROLE_TERMINALS + (
    "backend",
    "frontend",
    "fullstack",
    "full-stack",
    "software",
    "platform",
    "infrastructure",
    "devops",
    "data",
    "machine-learning",
    "ml",
    "ai",
    "founding",
    "senior",
    "staff",
    "principal",
)
ROLE_TITLE_PATTERN = re.compile(
    r"\b("
    r"(?:(?:founding|senior|staff|principal|lead|full[- ]stack|backend|frontend|"
    r"software|ai|ml|machine learning|data|platform|infrastructure|product|"
    r"devops|site reliability|applied ai|solutions|forward deployed)\s+){0,4}"
    r"(?:engineer|developer|architect|scientist|designer|manager|lead|analyst|"
    r"specialist|representative|executive|operator|recruiter)"
    r")\b",
    re.IGNORECASE,
)
PAGE_CLASSIFICATION_CSV_FIELDS = [
    "company_slug",
    "company_name",
    "url",
    "page_kind",
    "confidence",
    "job_title",
    "job_count",
    "role_titles",
    "http_status",
    "parser_name",
    "parser_version",
    "classified_at",
    "evidence",
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
    attempt_count: int = 1
    retryable: bool = False
    cache_source: str = "network"


@dataclass(frozen=True)
class PageClassification:
    page_kind: str
    confidence: float
    job_title: str | None
    role_titles: list[str]
    evidence: dict[str, Any]


class CachedHttpClient:
    """Classification request policy over the shared bounded disk cache."""

    def __init__(
        self,
        cache_path: Path | None,
        *,
        concurrency: int,
        cache_dir: Path | None = None,
        legacy_cache_path: Path | None = None,
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
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": "yc-radar/0.2 (+https://github.com/Daniishkhan/yc-radar; read-only research)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            },
            follow_redirects=True,
            timeout=15,
        )

    async def __aenter__(self) -> "CachedHttpClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.client.aclose()

    @property
    def cache_metrics(self) -> dict[str, int]:
        return dict(self.cache.metrics)

    async def get(self, url: str, *, bypass_cache: bool = False) -> HttpResult:
        if not bypass_cache:
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
                    attempt_count=int(cached.get("attempt_count") or 1),
                    retryable=bool(cached.get("retryable")),
                    cache_source="disk",
                )
        async with self.semaphore:
            try:
                response = await self.client.get(url)
                retryable = response.status_code in {408, 425, 429, 500, 502, 503, 504}
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
                    retryable=retryable,
                )
            except httpx.RequestError as exc:
                result = HttpResult(
                    url=url,
                    final_url=url,
                    status_code=None,
                    content_type="",
                    text="",
                    error=str(exc),
                    error_class=type(exc).__name__,
                    retryable=not isinstance(exc, httpx.TooManyRedirects),
                )
            self.cache.store(url, metadata=result.__dict__, text=result.text)
            return result

    def save(self) -> None:
        """Compatibility no-op; every cache entry is atomically persisted at fetch time."""


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Fetch discovered career/job URLs and classify page intent."
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Persist classifications after each batch so interrupted runs can resume.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--force",
        action="store_true",
        help="Reclassify all active URLs and bypass cache/retry-budget restrictions.",
    )
    selection.add_argument(
        "--retry-fetch-errors",
        action="store_true",
        help="Retry only explicitly retryable fetch errors below the attempt budget.",
    )
    parser.add_argument("--max-fetch-attempts", type=int, default=3)
    parser.add_argument("--cache-dir", type=Path, default=settings.page_fetch_cache_dir)
    parser.add_argument("--legacy-cache-path", type=Path, default=settings.page_fetch_cache_path)
    parser.add_argument("--cache-path", type=Path, default=None, help="Deprecated legacy cache path.")
    parser.add_argument("--output-csv", type=Path, default=settings.page_classifications_csv_path)
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
                read_status(status_file) or stage_started("classification"),
                state="failed",
                error=exc,
            ),
        )
        raise


async def run(args: argparse.Namespace) -> None:
    status = stage_started("classification")
    write_status(getattr(args, "status_file", None), status)
    engine = engine_from_url()
    with url_inventory_writer_lock(engine):
        await _run_with_inventory_lock(args, status, engine)


async def _run_with_inventory_lock(
    args: argparse.Namespace,
    status: dict[str, Any],
    engine: Any,
) -> None:
    force = bool(getattr(args, "force", False))
    retry_fetch_errors = bool(getattr(args, "retry_fetch_errors", False))
    max_fetch_attempts = max(1, int(getattr(args, "max_fetch_attempts", 3)))
    discovered_urls = fetch_discovered_url_rows(
        engine,
        limit=args.limit,
        only_unclassified=not force and not retry_fetch_errors,
        retry_fetch_errors=retry_fetch_errors,
        max_fetch_attempts=max_fetch_attempts,
    )
    if not discovered_urls:
        print(
            "No discovered URLs match this selection. Default mode selects unclassified URLs; "
            "use --retry-fetch-errors for eligible transient failures or --force to reclassify."
        )
        write_status(
            getattr(args, "status_file", None),
            stage_finished(status, state="completed", selected=0, processed=0, succeeded=0, failed=0),
        )
        return

    batch_size = max(1, args.batch_size)
    page_counts: Counter[str] = Counter()
    external_job_count = 0
    processed_count = 0
    error_classes: Counter[str] = Counter()
    retry_count = 0
    cache_path = getattr(args, "cache_path", None)
    cache_dir = getattr(args, "cache_dir", None)
    legacy_cache_path = getattr(args, "legacy_cache_path", None)
    cache_metrics: dict[str, int] = {}
    write_status(
        getattr(args, "status_file", None),
        stage_checkpoint(
            status,
            selected=len(discovered_urls),
            processed=0,
            succeeded=0,
            failed=0,
            retry_mode="force" if force else "fetch_error" if retry_fetch_errors else "pending",
            error_classes={},
            retry_count=0,
        ),
    )
    async with CachedHttpClient(
        cache_path or cache_dir,
        concurrency=args.concurrency,
        cache_dir=None if cache_path else cache_dir,
        legacy_cache_path=None if cache_path else legacy_cache_path,
    ) as http:
        for start in range(0, len(discovered_urls), batch_size):
            batch = discovered_urls[start : start + batch_size]
            results = await asyncio.gather(
                *(
                    fetch_and_classify(
                        row,
                        http,
                        bypass_cache=force or retry_fetch_errors,
                        max_fetch_attempts=max_fetch_attempts,
                    )
                    for row in batch
                )
            )
            batch_counts, batch_external_jobs = persist_results(engine, results)
            page_counts.update(batch_counts)
            external_job_count += batch_external_jobs
            for result in results:
                fetch = result["classification"]["evidence"].get("fetch", {})
                if fetch.get("error_class"):
                    error_classes[str(fetch["error_class"])] += 1
                retry_count += max(0, int(fetch.get("attempt_count") or 0) - 1)
            processed_count += len(batch)
            write_status(
                getattr(args, "status_file", None),
                stage_checkpoint(
                    status,
                    cache=http.cache_metrics,
                    selected=len(discovered_urls),
                    processed=processed_count,
                    succeeded=processed_count - page_counts.get("fetch_error", 0),
                    failed=page_counts.get("fetch_error", 0),
                    retry_mode="force" if force else "fetch_error" if retry_fetch_errors else "pending",
                    error_classes=dict(error_classes),
                    retry_count=retry_count,
                ),
            )
            print(
                f"Checkpointed {processed_count} / {len(discovered_urls)} discovered URLs.",
                flush=True,
            )
        cache_metrics = http.cache_metrics

    recent_rows = fetch_page_classification_rows(engine, limit=max(args.limit, processed_count))
    write_csv(args.output_csv, recent_rows, PAGE_CLASSIFICATION_CSV_FIELDS)

    fetch_errors = page_counts.get("fetch_error", 0)
    print(f"Selected {len(discovered_urls)} discovered URLs.")
    print(f"Fetched and stored {processed_count} source documents.")
    print(f"Classified page kinds: {format_counts(page_counts)}")
    print(f"Upserted {external_job_count} external job detail postings.")
    print(f"Wrote {args.output_csv}")
    print(f"Updated {engine.url.database}")
    write_status(
        getattr(args, "status_file", None),
        stage_finished(
            status,
            state="completed" if not fetch_errors else "partial",
            cache=cache_metrics,
            selected=len(discovered_urls),
            processed=processed_count,
            succeeded=processed_count - fetch_errors,
            failed=fetch_errors,
            retry_mode="force" if force else "fetch_error" if retry_fetch_errors else "pending",
            error_classes=dict(error_classes),
            retry_count=retry_count,
        ),
    )


def persist_results(engine: Any, results: list[dict[str, Any]]) -> tuple[Counter[str], int]:
    """Atomically persist every resumable classification consequence in one batch."""
    documents = [result["document"] for result in results]
    create_schema(engine)
    with engine.begin() as connection:
        upsert_source_documents_connection(connection, documents)
        document_rows = fetch_source_document_rows_connection(
            connection,
            source_type=SOURCE_TYPE,
            source_keys=[document["source_key"] for document in documents],
        )
        documents_by_key = {str(row["source_key"]): row for row in document_rows}

        classifications: list[dict[str, Any]] = []
        external_jobs: list[dict[str, Any]] = []
        for result in results:
            source_key = result["document"]["source_key"]
            source_document = documents_by_key[source_key]
            classification = {
                **result["classification"],
                "source_document_id": source_document["id"],
            }
            classifications.append(classification)
            if classification["page_kind"] == "job_detail" and classification.get("job_title"):
                external_jobs.append(external_job_row(classification, source_document))

        upsert_page_classifications_connection(connection, classifications)
        upsert_external_job_postings_connection(connection, external_jobs)
    return Counter(classification["page_kind"] for classification in classifications), len(external_jobs)


async def fetch_and_classify(
    row: dict[str, Any],
    http: CachedHttpClient,
    *,
    bypass_cache: bool = False,
    max_fetch_attempts: int = 3,
) -> dict[str, Any]:
    requested_url = str(row.get("normalized_url") or row.get("url") or "")
    result = await http.get(requested_url, bypass_cache=bypass_cache)
    fetched_at = datetime.now(UTC)
    raw_text = strip_nul_bytes(result.text)
    title = extract_title(raw_text)
    clean_text = strip_html(raw_text)
    classification = classify_page(
        url=result.final_url or requested_url,
        title=title,
        text=clean_text,
        http_status=result.status_code,
        url_kind=str(row.get("url_kind") or ""),
    )
    previous_attempts = int(row.get("fetch_attempt_count") or 0)
    fetch_attempt = previous_attempts + 1 if classification.page_kind == "fetch_error" else 0
    retryable = bool(result.retryable) and fetch_attempt < max(1, max_fetch_attempts)
    terminal_reason = None
    if classification.page_kind == "fetch_error" and not retryable:
        terminal_reason = (
            "retry_budget_exhausted"
            if result.retryable
            else "terminal_request_error_or_http_status"
        )
    fetch_evidence = {
        "attempt_count": fetch_attempt,
        "retryable": retryable,
        "terminal_reason": terminal_reason,
        "error_class": result.error_class,
        "error": result.error,
        "requested_url": requested_url,
        "final_url": result.final_url or requested_url,
        "cache_source": result.cache_source,
    }
    source_key = source_key_for(row)
    document = {
        "discovered_url_id": row.get("id"),
        "company_id": row.get("company_id"),
        "company_slug": row.get("company_slug"),
        "company_name": row.get("company_name"),
        "source_type": SOURCE_TYPE,
        "source_key": source_key,
        "url": result.final_url or requested_url,
        "normalized_url": result.final_url or requested_url,
        "title": title,
        "raw_text": raw_text[:MAX_STORED_TEXT_CHARS],
        "clean_text": clean_text[:MAX_STORED_TEXT_CHARS],
        "content_hash": content_hash(result, clean_text),
        "http_status": result.status_code,
        "fetched_at": fetched_at.isoformat(),
        "observed_at": fetched_at.isoformat(),
        "raw_json": {
            "requested_url": requested_url,
            "discovered_url_id": row.get("id"),
            "url_key": row.get("url_key"),
            "url_kind": row.get("url_kind"),
            "content_type": result.content_type,
            "error": result.error,
            "error_class": result.error_class,
            "fetch": fetch_evidence,
            "discovery_sources": row.get("discovery_sources") or [],
        },
    }
    classification_row = {
        "discovered_url_id": row.get("id"),
        "company_id": row.get("company_id"),
        "company_slug": row.get("company_slug"),
        "company_name": row.get("company_name"),
        "url": result.final_url or requested_url,
        "normalized_url": result.final_url or requested_url,
        "page_kind": classification.page_kind,
        "confidence": classification.confidence,
        "parser_name": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "http_status": result.status_code,
        "job_title": classification.job_title,
        "role_titles": classification.role_titles,
        "job_count": len(classification.role_titles),
        "evidence": {**classification.evidence, "fetch": fetch_evidence},
        "classified_at": fetched_at.isoformat(),
        "raw_json": {
            "requested_url": requested_url,
            "url_kind": row.get("url_kind"),
            "source_key": source_key,
            "fetch": fetch_evidence,
        },
    }
    return {
        "document": document,
        "classification": classification_row,
    }


def classify_page(
    *,
    url: str,
    title: str,
    text: str,
    http_status: int | None,
    url_kind: str = "",
) -> PageClassification:
    parsed = urlparse(url)
    domain = clean_domain(parsed.netloc)
    path = parsed.path.lower()
    normalized_text = text.lower()
    roles = extract_role_titles(title, text, url)
    is_ats = is_ats_domain(domain)
    generic_path = is_generic_career_path(path)
    role_like_path = is_role_like_path(path)
    listing_hits = marker_hits(normalized_text, LISTING_MARKERS)
    detail_hits = marker_hits(normalized_text, DETAIL_MARKERS)
    career_hits = marker_hits(normalized_text, CAREER_MARKERS)

    evidence = {
        "domain": domain,
        "path": path or "/",
        "url_kind": url_kind,
        "is_ats": is_ats,
        "generic_path": generic_path,
        "role_like_path": role_like_path,
        "listing_marker_hits": listing_hits,
        "detail_marker_hits": detail_hits,
        "career_marker_hits": career_hits,
        "role_titles": roles[:20],
        "title": title,
    }

    if not http_status or http_status >= 400:
        return PageClassification("fetch_error", 0.98, None, roles, evidence)
    if not text.strip():
        return PageClassification("unknown", 0.4, None, roles, evidence)

    if is_ats and ((role_like_path and roles) or (len(roles) == 1 and detail_hits >= 2)):
        return PageClassification("job_detail", 0.88, roles[0] if roles else None, roles, evidence)
    if is_ats:
        confidence = 0.92 if len(roles) >= 2 or listing_hits else 0.72
        return PageClassification("ats_listing", confidence, None, roles, evidence)

    if role_like_path and roles and detail_hits >= 1:
        return PageClassification("job_detail", 0.86, roles[0] if roles else None, roles, evidence)
    if len(roles) == 1 and detail_hits >= 2 and not generic_path:
        return PageClassification("job_detail", 0.78, roles[0], roles, evidence)
    if generic_path and (len(roles) >= 2 or listing_hits):
        return PageClassification("job_listing", 0.84, None, roles, evidence)
    if len(roles) >= 3 and listing_hits:
        return PageClassification("job_listing", 0.78, None, roles, evidence)
    if generic_path or career_hits:
        return PageClassification("career_home", 0.74, None, roles, evidence)
    if roles and detail_hits:
        return PageClassification("job_detail", 0.62, roles[0], roles, evidence)
    return PageClassification("irrelevant", 0.55, None, roles, evidence)


def external_job_row(
    classification: dict[str, Any],
    source_document: dict[str, Any],
) -> dict[str, Any]:
    title = str(classification.get("job_title") or "")
    role_fit = classify_role_text(title, str(source_document.get("clean_text") or "")[:4000])
    observed_at = classification.get("classified_at") or datetime.now(UTC).isoformat()
    return {
        "company_id": classification.get("company_id"),
        "company_slug": classification.get("company_slug"),
        "company_name": classification.get("company_name"),
        "source_document_id": source_document.get("id"),
        "source": SOURCE_TYPE,
        "source_job_id": source_document.get("source_key"),
        "posting_url": classification.get("url"),
        "normalized_url": classification.get("normalized_url"),
        "title": title,
        "description_text": source_document.get("clean_text"),
        "role_fit": role_fit.status,
        "extraction_confidence": classification.get("confidence"),
        "observed_at": observed_at,
        "raw_json": {
            "parser_name": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "role_fit_reasons": role_fit.reasons,
            "classification_evidence": classification.get("evidence"),
        },
    }


def source_key_for(row: dict[str, Any]) -> str:
    return f"{row.get('company_slug')}:{row.get('url_key')}"


def extract_title(html_text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text or "", flags=re.I | re.S)
    if not match:
        return ""
    title = strip_html(match.group(1))
    return clean_text(html.unescape(title))[:300]


def strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    without_scripts = re.sub(
        r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>",
        " ",
        html_text,
        flags=re.I | re.S,
    )
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    return clean_text(html.unescape(without_tags))


def extract_role_titles(title: str, text: str, url: str, *, limit: int = 25) -> list[str]:
    candidates: list[str] = []
    title_candidate = role_title_from_title(title)
    if title_candidate:
        candidates.append(title_candidate)
    url_candidate = role_title_from_url(url)
    if url_candidate:
        candidates.append(url_candidate)
    search_text = clean_text(text[:150_000])
    candidates.extend(match.group(1) for match in ROLE_TITLE_PATTERN.finditer(search_text))
    return dedupe([candidate for candidate in map(normalize_role_title, candidates) if candidate])[
        :limit
    ]


def role_title_from_title(title: str) -> str | None:
    title = clean_text(title)
    if not title:
        return None
    for separator in (" | ", " - ", " at "):
        if separator in title:
            title = title.split(separator, 1)[0]
    return title if is_role_title(title) else None


def role_title_from_url(url: str) -> str | None:
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    if not path_parts:
        return None
    slug = re.sub(r"\d+", " ", path_parts[-1])
    title = clean_text(slug.replace("-", " ").replace("_", " "))
    return title if is_role_title(title) else None


def normalize_role_title(value: str) -> str | None:
    value = clean_text(value)
    value = re.sub(r"\b(?:new|remote|hybrid|full[- ]time|part[- ]time)\b", " ", value, flags=re.I)
    value = clean_text(value)
    words = value.split()
    if len(words) < 2 or len(words) > 9:
        return None
    if not is_role_title(value):
        return None
    return " ".join(word.capitalize() if word.islower() else word for word in words)


def is_role_title(value: str) -> bool:
    normalized = value.lower()
    if any(marker in normalized for marker in LISTING_MARKERS + CAREER_MARKERS):
        return False
    return any(re.search(rf"\b{re.escape(term)}s?\b", normalized) for term in ROLE_TERMINALS)


def is_generic_career_path(path: str) -> bool:
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return False
    return parts[-1].replace("_", "-") in GENERIC_CAREER_SEGMENTS


def is_role_like_path(path: str) -> bool:
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return False
    last = parts[-1].lower().replace("_", "-")
    if last in GENERIC_CAREER_SEGMENTS:
        return False
    tokens = {token for token in re.split(r"[^a-z0-9]+", last) if token}
    for term in ROLE_URL_TERMS:
        if len(term) <= 3:
            if term in tokens:
                return True
            continue
        if term in last:
            return True
    return False


def marker_hits(text: str, markers: tuple[str, ...]) -> int:
    return sum(1 for marker in markers if marker in text)


def content_hash(result: HttpResult, clean_text_value: str) -> str:
    payload = "\n".join(
        [
            result.final_url,
            str(result.status_code),
            result.error or "",
            clean_text_value or result.text,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def clean_domain(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_nul_bytes(value: str) -> str:
    # Postgres text/jsonb columns reject NUL (0x00) bytes.
    return (value or "").replace("\x00", "")


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def format_counts(counts: Counter[str]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fieldnames})
    tmp_path.replace(path)


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


if __name__ == "__main__":
    main()
