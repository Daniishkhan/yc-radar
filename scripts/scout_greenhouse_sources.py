#!/usr/bin/env python3
"""Verify Common Crawl Greenhouse candidates and register only safe identities."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.commoncrawl_greenhouse import CRAWL_RE, deduplicate_candidate_rows
from yc_radar.services.database import company_sources_table, companies_table, engine_from_url
from yc_radar.services.greenhouse_scout import (
    GreenhouseBoardEvidence,
    GreenhouseBoardScout,
    resolve_company,
)
from yc_radar.services.job_source_registry import JobSourceRegistry
from yc_radar.services.run_status import (
    read_status,
    stage_checkpoint,
    stage_finished,
    stage_started,
    write_status,
)

OUTPUT_FIELDS = [
    "board_token",
    "canonical_source_url",
    "example_observed_url",
    "observation_count",
    "first_observed_at",
    "last_observed_at",
    "first_seen_crawl",
    "last_seen_crawl",
    "crawl_count",
    "crawl_ids",
    "verification_status",
    "http_status",
    "board_name",
    "job_count",
    "external_job_origins",
    "board_page_origin",
    "resolution_status",
    "company_id",
    "website_candidate",
    "company_source_id",
    "registration_status",
    "cache_source",
    "attempt_count",
    "error",
    "checked_at",
]
CRAWL_PROVENANCE_FIELDS = frozenset(
    {
        "first_observed_at",
        "last_observed_at",
        "first_seen_crawl",
        "last_seen_crawl",
        "crawl_count",
        "crawl_ids",
    }
)
REQUIRED_RESUME_FIELDS = set(OUTPUT_FIELDS).difference(CRAWL_PROVENANCE_FIELDS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sequentially verify Greenhouse board tokens from an Athena CSV and optionally "
            "register sources without silently merging ambiguous company identities."
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
    parser.add_argument("--status-file", type=Path, help="Atomic local stage-status JSON output.")
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore an existing partial/final output and verify the selected tokens again.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Register exact matches, verified-domain companies, and isolated provisional "
            "companies for otherwise unresolved verified boards."
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
    ensure_checkpoint_manifest(args, selected)
    status = stage_started("greenhouse_scout")
    write_status(args.status_file, status)
    engine = engine_from_url()
    companies, existing_sources = load_registry_state(engine)
    resume_path = existing_resume_path(args.output) if not args.no_resume else None
    resume_rows = load_resume_rows(resume_path) if resume_path else {}
    rows: list[dict[str, Any]] = []
    crawl = crawl_from_path(args.input)
    resumed = 0

    with GreenhouseBoardScout(
        args.cache_dir,
        delay_seconds=args.delay_seconds,
    ) as scout:
        for index, candidate in enumerate(selected, start=1):
            token = candidate["board_token"]
            prior = resume_rows.get(token)
            if prior is not None and can_resume_row(prior, candidate=candidate, apply=args.apply):
                rows.append(prior)
                resumed += 1
                if index % args.checkpoint_every == 0:
                    checkpoint(args, status, rows, selected=len(selected), resumed=resumed)
                continue
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
                    homepage_verifier=scout.verify_homepage,
                )
            rows.append(row)
            if index % args.checkpoint_every == 0:
                checkpoint(args, status, rows, selected=len(selected), resumed=resumed)

    write_csv_atomic(args.output, rows)
    write_csv_atomic(args.output.with_suffix(".partial.csv"), rows)
    print_progress(len(selected), len(selected), rows)
    write_status(
        args.status_file,
        stage_finished(
            status,
            state="completed",
            selected=len(selected),
            processed=len(rows),
            succeeded=sum(row["verification_status"] != "failed" for row in rows),
            failed=sum(row["verification_status"] == "failed" for row in rows),
            resumed=resumed,
            output=str(args.output),
        ),
    )
    print(f"Wrote {len(rows)} verification rows to {args.output}")


def existing_resume_path(output: Path) -> Path | None:
    partial = output.with_suffix(".partial.csv")
    if partial.exists():
        return partial
    return output if output.exists() else None


def ensure_checkpoint_manifest(
    args: argparse.Namespace,
    selected: list[dict[str, str]],
) -> None:
    selected_identity = "\n".join(
        f"{row['board_token']}\t{row['canonical_source_url']}" for row in selected
    ).encode()
    payload = {
        "schema_version": 1,
        "input_path": str(args.input.resolve()),
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "selected_sha256": hashlib.sha256(selected_identity).hexdigest(),
        "selected_count": len(selected),
        "offset": args.offset,
        "limit": args.limit,
        "apply": args.apply,
    }
    manifest_path = args.output.with_suffix(".checkpoint.json")
    prior = None if args.no_resume else read_status(manifest_path)
    if prior is not None and prior != payload:
        raise SystemExit(
            f"checkpoint manifest does not match this input/scope: {manifest_path}; "
            "use a different --output or --no-resume"
        )
    write_status(manifest_path, payload)


def load_resume_rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    if rows and REQUIRED_RESUME_FIELDS - set(rows[0]):
        return {}
    return {
        row["board_token"].strip().lower(): row
        for row in rows
        if row.get("board_token", "").strip()
    }


def can_resume_row(
    row: dict[str, str],
    *,
    candidate: dict[str, str],
    apply: bool,
) -> bool:
    if row.get("canonical_source_url") != candidate.get("canonical_source_url"):
        return False
    verification = row.get("verification_status")
    if verification == "failed":
        return False
    if not apply or verification != "verified":
        return True
    registration = row.get("registration_status")
    # The checkpoint manifest binds the row to this input and scope. Resume only a
    # completed registration outcome; apply a prior dry-run row on this invocation.
    return registration not in {"not_requested", ""}


def checkpoint(
    args: argparse.Namespace,
    status: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    selected: int,
    resumed: int,
) -> None:
    write_csv_atomic(args.output.with_suffix(".partial.csv"), rows)
    print_progress(len(rows), selected, rows)
    write_status(
        args.status_file,
        stage_checkpoint(
            status,
            selected=selected,
            processed=len(rows),
            succeeded=sum(row["verification_status"] != "failed" for row in rows),
            failed=sum(row["verification_status"] == "failed" for row in rows),
            resumed=resumed,
            checkpoint=str(args.output.with_suffix(".partial.csv")),
        ),
    )


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
            str(row.external_id): int(row.company_id)
            for row in connection.execute(
                select(
                    company_sources_table.c.external_id,
                    company_sources_table.c.company_id,
                ).where(company_sources_table.c.provider == "greenhouse")
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
    homepage_verifier: Callable[[str], str | None],
) -> None:
    company_id = resolution.company_id
    website = resolution.website_candidate
    registration_status = "skipped"
    try:
        if resolution.status == "new_company_domain_candidate" and website:
            verified_website = homepage_verifier(website)
            company = (
                CompanyRegistry(engine).register_company(
                    name=evidence.company_name or "",
                    website=verified_website,
                )
                if verified_website is not None
                else CompanyRegistry(engine).register_provisional_company(
                    name=evidence.company_name or "",
                    requested_slug=evidence.board_token,
                )
            )
            company_id = company.company_id
            website = verified_website
            registration_status = (
                "company_created"
                if verified_website is not None and company.company_created
                else "company_reused"
                if verified_website is not None
                else "company_provisional"
            )
        elif resolution.status == "existing_exact_name":
            registration_status = "company_reused"
        elif evidence.company_name and resolution.status in {
            "unresolved_no_domain",
            "ambiguous_name",
            "ambiguous_domain",
            "identity_conflict",
        }:
            company = CompanyRegistry(engine).register_provisional_company(
                name=evidence.company_name,
                requested_slug=evidence.board_token,
            )
            company_id = company.company_id
            website = None
            registration_status = "company_provisional"
        else:
            return

        result = JobSourceRegistry(engine).register_url(
            company_id=int(company_id),
            provider="greenhouse",
            source_url=candidate["canonical_source_url"],
            discovered_from_url=candidate["example_observed_url"],
            evidence={
                "discovery_provider": "commoncrawl_url_index",
                "observation_count": int(candidate.get("observation_count") or 0),
                **candidate_crawl_provenance(candidate, fallback_crawl=crawl),
                "verified_company_name": evidence.company_name,
                "verified_job_count": evidence.job_count,
                "website_evidence": website,
            },
        )
        row["company_id"] = company_id
        row["website_candidate"] = website
        row["company_source_id"] = result.company_source_id
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


def result_row(candidate: dict[str, str], evidence, resolution) -> dict[str, Any]:
    return {
        "board_token": candidate["board_token"],
        "canonical_source_url": candidate["canonical_source_url"],
        "example_observed_url": candidate["example_observed_url"],
        "observation_count": candidate.get("observation_count") or "",
        "first_observed_at": candidate.get("first_observed_at") or "",
        "last_observed_at": candidate.get("last_observed_at") or "",
        "first_seen_crawl": candidate.get("first_seen_crawl") or "",
        "last_seen_crawl": candidate.get("last_seen_crawl") or "",
        "crawl_count": candidate.get("crawl_count") or "",
        "crawl_ids": candidate.get("crawl_ids") or "",
        "verification_status": evidence.verification_status,
        "http_status": evidence.http_status if evidence.http_status is not None else "",
        "board_name": evidence.company_name or "",
        "job_count": evidence.job_count,
        "external_job_origins": json.dumps(evidence.external_job_origins),
        "board_page_origin": evidence.board_page_origin or "",
        "resolution_status": resolution.status,
        "company_id": resolution.company_id or "",
        "website_candidate": resolution.website_candidate or "",
        "company_source_id": "",
        "registration_status": "not_requested",
        "cache_source": evidence.cache_source,
        "attempt_count": evidence.attempt_count,
        "error": resolution.reason or evidence.error or "",
        "checked_at": datetime.now(UTC).isoformat(),
    }


def candidate_crawl_provenance(
    candidate: dict[str, str],
    *,
    fallback_crawl: str | None,
) -> dict[str, Any]:
    raw_crawl_ids = str(candidate.get("crawl_ids") or "").strip()
    if raw_crawl_ids:
        try:
            parsed_crawl_ids = json.loads(raw_crawl_ids)
        except json.JSONDecodeError as exc:
            raise ValueError("crawl_ids must be a JSON array") from exc
        if not isinstance(parsed_crawl_ids, list) or not parsed_crawl_ids:
            raise ValueError("crawl_ids must be a non-empty JSON array")
        crawl_ids = sorted({str(value) for value in parsed_crawl_ids}, key=crawl_sort_key)
        if len(crawl_ids) != len(parsed_crawl_ids):
            raise ValueError("crawl_ids must contain unique crawl IDs")
    elif fallback_crawl:
        crawl_ids = [fallback_crawl]
    else:
        crawl_ids = []

    first_seen = str(candidate.get("first_seen_crawl") or "").strip()
    last_seen = str(candidate.get("last_seen_crawl") or "").strip()
    raw_count = str(candidate.get("crawl_count") or "").strip()
    if crawl_ids:
        expected_first = crawl_ids[0]
        expected_last = crawl_ids[-1]
        if first_seen and first_seen != expected_first:
            raise ValueError("first_seen_crawl does not match crawl_ids")
        if last_seen and last_seen != expected_last:
            raise ValueError("last_seen_crawl does not match crawl_ids")
        if raw_count:
            try:
                crawl_count = int(raw_count)
            except ValueError as exc:
                raise ValueError("crawl_count must be a positive integer") from exc
            if crawl_count != len(crawl_ids):
                raise ValueError("crawl_count does not match crawl_ids")
        else:
            crawl_count = len(crawl_ids)
    else:
        crawl_count = 0
        if first_seen or last_seen or raw_count:
            raise ValueError("crawl summary fields require crawl_ids or a single-crawl filename")

    provenance: dict[str, Any] = {}
    for field in ("first_observed_at", "last_observed_at"):
        if candidate.get(field):
            provenance[field] = candidate[field]
    if crawl_ids:
        provenance.update(
            {
                "first_seen_crawl": crawl_ids[0],
                "last_seen_crawl": crawl_ids[-1],
                "crawl_count": crawl_count,
                "crawl_ids": crawl_ids,
            }
        )
        if len(crawl_ids) == 1:
            provenance["crawl"] = crawl_ids[0]
    else:
        provenance["crawl"] = fallback_crawl
    return provenance


def crawl_sort_key(crawl_id: str) -> tuple[int, int]:
    if not CRAWL_RE.fullmatch(crawl_id):
        raise ValueError(f"invalid crawl ID: {crawl_id!r}")
    _, _, year, week = crawl_id.split("-")
    return int(year), int(week)


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
    matches = re.findall(r"CC-MAIN-\d{4}-\d{2}", path.name)
    return matches[0] if len(matches) == 1 else None


if __name__ == "__main__":
    main()
