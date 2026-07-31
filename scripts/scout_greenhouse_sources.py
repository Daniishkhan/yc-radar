#!/usr/bin/env python3
"""Verify Common Crawl Greenhouse candidates and register only safe identities."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from sqlalchemy import select

from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.commoncrawl_greenhouse import deduplicate_candidate_rows
from yc_radar.services.database import career_sources_table, companies_table, engine_from_url
from yc_radar.services.greenhouse_scout import (
    SCOUT_USER_AGENT,
    GreenhouseBoardEvidence,
    GreenhouseBoardScout,
    domains_compatible,
    resolve_company,
)
from yc_radar.services.job_source_registry import JobSourceRegistry
from yc_radar.services.source_providers import is_ats_domain

OUTPUT_FIELDS = [
    "board_token",
    "canonical_source_url",
    "example_observed_url",
    "observation_count",
    "verification_status",
    "http_status",
    "board_name",
    "job_count",
    "external_job_origins",
    "board_page_origin",
    "resolution_status",
    "company_id",
    "website_candidate",
    "career_source_id",
    "registration_status",
    "cache_source",
    "attempt_count",
    "error",
    "checked_at",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sequentially verify Greenhouse board tokens from an Athena CSV and optionally "
            "register only unambiguous source/company identities."
        )
    )
    parser.add_argument("--input", type=Path, required=True, help="Athena candidate CSV.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/local/debug/greenhouse_board_verification.csv"),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("data/local/cache/greenhouse_source_scout"),
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--delay-seconds",
        type=non_negative_float,
        default=1.0,
        help="Minimum delay between network requests. Cached reads do not wait.",
    )
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Register exact existing-company matches and new companies backed by a verified "
            "custom job domain. Ambiguous evidence is always left unresolved."
        ),
    )
    return parser.parse_args()


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def main() -> None:
    args = parse_args()
    if args.offset < 0 or (args.limit is not None and args.limit < 1):
        raise SystemExit("--offset must be non-negative and --limit must be positive")
    if args.checkpoint_every < 1:
        raise SystemExit("--checkpoint-every must be positive")

    candidates = load_candidates(args.input)
    selected = candidates[args.offset :]
    if args.limit is not None:
        selected = selected[: args.limit]
    engine = engine_from_url()
    companies, existing_sources = load_registry_state(engine)
    rows: list[dict[str, Any]] = []
    crawl = crawl_from_path(args.input)

    with GreenhouseBoardScout(
        args.cache_dir,
        delay_seconds=args.delay_seconds,
    ) as scout:
        for index, candidate in enumerate(selected, start=1):
            token = candidate["board_token"]
            existing_company_id = existing_sources.get(token)
            if existing_company_id is not None:
                evidence = GreenhouseBoardEvidence(
                    board_token=token,
                    verification_status="already_registered",
                    http_status=None,
                    company_name=None,
                    job_count=0,
                    external_job_origins=(),
                    cache_source="registry",
                )
            else:
                evidence = scout.verify(token)
            resolution = resolve_company(
                evidence,
                companies=companies,
                existing_source_company_id=existing_company_id,
            )
            if (
                evidence.verification_status == "verified"
                and resolution.status == "unresolved_no_domain"
            ):
                evidence = scout.enrich_from_board_page(evidence)
                resolution = resolve_company(
                    evidence,
                    companies=companies,
                    existing_source_company_id=existing_company_id,
                )
            row = result_row(candidate, evidence, resolution)
            if args.apply and evidence.verification_status == "verified":
                apply_registration(
                    row,
                    evidence=evidence,
                    resolution=resolution,
                    candidate=candidate,
                    companies=companies,
                    existing_sources=existing_sources,
                    crawl=crawl,
                    engine=engine,
                )
            rows.append(row)
            if index % args.checkpoint_every == 0:
                write_csv_atomic(args.output.with_suffix(".partial.csv"), rows)
                print_progress(index, len(selected), rows)

    write_csv_atomic(args.output, rows)
    print_progress(len(selected), len(selected), rows)
    print(f"Wrote {len(rows)} verification rows to {args.output}")


def load_candidates(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    required = {"board_token", "canonical_source_url", "example_observed_url"}
    missing = required - set(rows[0] if rows else ())
    if missing:
        raise ValueError(f"candidate CSV is missing columns: {', '.join(sorted(missing))}")
    return deduplicate_candidate_rows(rows)


def load_registry_state(engine) -> tuple[list[dict[str, Any]], dict[str, int]]:
    with engine.connect() as connection:
        companies = [dict(row) for row in connection.execute(select(companies_table)).mappings()]
        sources = {
            str(row.external_source_id): int(row.company_id)
            for row in connection.execute(
                select(
                    career_sources_table.c.external_source_id,
                    career_sources_table.c.company_id,
                ).where(career_sources_table.c.provider == "greenhouse")
            )
        }
    return companies, sources


def apply_registration(
    row: dict[str, Any],
    *,
    evidence: GreenhouseBoardEvidence,
    resolution,
    candidate: dict[str, str],
    companies: list[dict[str, Any]],
    existing_sources: dict[str, int],
    crawl: str | None,
    engine,
) -> None:
    company_id = resolution.company_id
    website = resolution.website_candidate
    registration_status = "skipped"
    try:
        if resolution.status == "new_company_domain_candidate" and website:
            verified_website = verify_homepage(website)
            if verified_website is None:
                row["registration_status"] = "homepage_unverified"
                return
            company = CompanyRegistry(engine).register_company(
                name=evidence.company_name or "",
                website=verified_website,
            )
            company_id = company.company_id
            website = verified_website
            registration_status = "company_created" if company.company_created else "company_reused"
        elif resolution.status == "existing_exact_name":
            registration_status = "company_reused"
        else:
            return

        result = JobSourceRegistry(engine).register_url(
            company_id=int(company_id),
            provider="greenhouse",
            source_url=candidate["canonical_source_url"],
            discovered_from_url=candidate["example_observed_url"],
            evidence={
                "discovery_provider": "commoncrawl_url_index",
                "crawl": crawl,
                "observation_count": int(candidate.get("observation_count") or 0),
                "verified_company_name": evidence.company_name,
                "verified_job_count": evidence.job_count,
                "website_evidence": website,
            },
        )
        row["company_id"] = company_id
        row["website_candidate"] = website
        row["career_source_id"] = result.career_source_id
        row["registration_status"] = (
            f"{registration_status}_source_created" if result.created else "source_existing"
        )
        existing_sources[evidence.board_token] = int(company_id)
        if not any(int(company["id"]) == int(company_id) for company in companies):
            companies.append(
                {
                    "id": int(company_id),
                    "name": evidence.company_name,
                    "primary_domain": (urlparse(website or "").hostname or "").removeprefix("www."),
                }
            )
    except (ValueError, httpx.HTTPError) as exc:
        row["registration_status"] = "conflict"
        row["error"] = str(exc)[:500]


def verify_homepage(url: str) -> str | None:
    headers = {
        "User-Agent": SCOUT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.8",
    }
    try:
        with httpx.Client(timeout=15, follow_redirects=True, headers=headers) as client:
            with client.stream("GET", url, headers=headers) as response:
                if not 200 <= response.status_code < 400:
                    return None
                final = urlparse(str(response.url))
    except httpx.HTTPError:
        return None
    host = (final.hostname or "").lower().rstrip(".")
    if (
        final.scheme not in {"http", "https"}
        or not host
        or is_ats_domain(host)
        or not domains_compatible(url, host)
    ):
        return None
    return urlunparse((final.scheme, host, "", "", "", ""))


def result_row(candidate: dict[str, str], evidence, resolution) -> dict[str, Any]:
    return {
        "board_token": candidate["board_token"],
        "canonical_source_url": candidate["canonical_source_url"],
        "example_observed_url": candidate["example_observed_url"],
        "observation_count": candidate.get("observation_count") or "",
        "verification_status": evidence.verification_status,
        "http_status": evidence.http_status if evidence.http_status is not None else "",
        "board_name": evidence.company_name or "",
        "job_count": evidence.job_count,
        "external_job_origins": json.dumps(evidence.external_job_origins),
        "board_page_origin": evidence.board_page_origin or "",
        "resolution_status": resolution.status,
        "company_id": resolution.company_id or "",
        "website_candidate": resolution.website_candidate or "",
        "career_source_id": "",
        "registration_status": "not_requested",
        "cache_source": evidence.cache_source,
        "attempt_count": evidence.attempt_count,
        "error": resolution.reason or evidence.error or "",
        "checked_at": datetime.now(UTC).isoformat(),
    }


def write_csv_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def print_progress(processed: int, selected: int, rows: list[dict[str, Any]]) -> None:
    verification = Counter(str(row["verification_status"]) for row in rows)
    resolutions = Counter(str(row["resolution_status"]) for row in rows)
    registrations = Counter(str(row["registration_status"]) for row in rows)
    print(
        f"processed={processed}/{selected} verification={dict(verification)} "
        f"resolution={dict(resolutions)} registration={dict(registrations)}",
        flush=True,
    )


def crawl_from_path(path: Path) -> str | None:
    match = re.search(r"CC-MAIN-\d{4}-\d{2}", path.name)
    return match.group(0) if match else None


if __name__ == "__main__":
    main()
