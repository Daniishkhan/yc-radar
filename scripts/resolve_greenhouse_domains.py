#!/usr/bin/env python3
"""Resolve domain-less verified Greenhouse boards with grounded Google Search."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import signal
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from yc_radar.adapters.greenhouse import GreenhouseAdapter
from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.commoncrawl_greenhouse import CRAWL_RE
from yc_radar.services.database import career_sources_table, companies_table, engine_from_url
from yc_radar.services.google_domain_resolver import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL,
    EVIDENCE_VERSION,
    PROMPT_VERSION,
    DomainResolutionResult,
    GoogleDomainResolver,
    acceptable_company_domain,
    citations_json,
    find_company_domain_matches,
    result_evidence_json,
)
from yc_radar.services.greenhouse_scout import (
    GreenhouseBoardEvidence,
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
    "board_name",
    "job_count",
    "resolution_status",
    "domain_resolution_status",
    "accepted_domain",
    "website_candidate",
    "generated_text",
    "search_query_count",
    "search_queries",
    "citation_count",
    "citations",
    "grounding_metadata",
    "candidate_domain_count",
    "passing_domain_count",
    "candidate_evidence",
    "model",
    "location",
    "prompt_token_count",
    "candidates_token_count",
    "total_token_count",
    "thoughts_token_count",
    "cached_content_token_count",
    "cache_source",
    "request_attempt_count",
    "retryable",
    "quota_exhausted",
    "registry_resolution_status",
    "company_id",
    "career_source_id",
    "registration_status",
    "error",
    "checked_at",
]
CRAWL_PROVENANCE_FIELDS = (
    "first_observed_at",
    "last_observed_at",
    "first_seen_crawl",
    "last_seen_crawl",
    "crawl_count",
    "crawl_ids",
)
REQUIRED_RESUME_FIELDS = set(OUTPUT_FIELDS).difference(CRAWL_PROVENANCE_FIELDS)
RETRYABLE_RESULT_STATUSES = frozenset({"request_failed", "quota_exhausted"})
SUCCESSFUL_REGISTRATION_STATUSES = frozenset(
    {
        "source_existing",
        "company_created_source_created",
        "company_reused_source_created",
    }
)
DEFAULT_COMPANY_TIMEOUT_SECONDS = 120.0
GREENHOUSE_ADAPTER = GreenhouseAdapter()


class CompanyResolutionDeadlineExceeded(BaseException):
    """Escape third-party retry loops when one company exceeds its wall-clock budget."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use one Vertex AI Google Search request per verified domain-less Greenhouse "
            "company, then independently require exact reciprocal board-link evidence."
        )
    )
    parser.add_argument("--input", type=Path, required=True, help="Greenhouse scout CSV.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/local/debug/greenhouse_domain_resolution.csv"),
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=Path("data/local/debug/greenhouse_domain_resolution.status.json"),
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=Path("data/local/cache/greenhouse_domain_resolver.json"),
        help="Atomic raw google-genai response cache; safe to share across run scopes.",
    )
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument(
        "--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION)
    )
    parser.add_argument("--model", default=os.environ.get("YC_RADAR_VERTEX_MODEL", DEFAULT_MODEL))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument(
        "--delay-seconds",
        type=non_negative_float,
        default=1.0,
        help="Minimum delay between Vertex network attempts; cache hits do not wait.",
    )
    parser.add_argument("--retry-delay-seconds", type=non_negative_float, default=2.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-pages-per-domain", type=int, default=3)
    parser.add_argument(
        "--company-timeout-seconds",
        type=positive_float,
        default=DEFAULT_COMPANY_TIMEOUT_SECONDS,
        help=(
            "Hard wall-clock budget for one company. A timeout is checkpointed as a "
            "retryable request failure and the run continues."
        ),
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore prior output rows; the independent raw-response cache remains enabled.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Register only uniquely accepted domains through the existing company/source gates.",
    )
    return parser.parse_args()


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


@contextmanager
def company_resolution_deadline(seconds: float) -> Iterator[None]:
    """Bound a synchronous resolver call even across nested SDK and HTTP retries."""

    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        raise RuntimeError("per-company deadlines require POSIX interval timers")

    def deadline_reached(_signum: int, _frame: Any) -> None:
        # BaseException is deliberate: broad third-party `except Exception` retry loops
        # must not swallow the job-level wall-clock deadline.
        raise CompanyResolutionDeadlineExceeded

    previous_handler = signal.signal(signal.SIGALRM, deadline_reached)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def main() -> None:
    args = parse_args()
    exit_code = run(args)
    if exit_code:
        raise SystemExit(exit_code)


def run(args: argparse.Namespace) -> int:
    validate_args(args)
    eligible = load_candidates(args.input)
    selected = eligible[args.offset :]
    if args.limit is not None:
        selected = selected[: args.limit]
    ensure_checkpoint_manifest(args, selected)
    resume_path = existing_resume_path(args.output) if not args.no_resume else None
    resume_rows = load_resume_rows(resume_path) if resume_path else {}
    status = stage_started("greenhouse_domain_resolver")
    status.update(
        {
            "eligible": len(eligible),
            "model": args.model,
            "location": args.location,
            "project": args.project,
            "prompt_version": PROMPT_VERSION,
            "evidence_version": EVIDENCE_VERSION,
            "company_timeout_seconds": args.company_timeout_seconds,
            "dry_run": not args.apply,
        }
    )
    write_status(args.status_file, status)

    companies: list[dict[str, Any]] = []
    existing_sources: dict[str, int] = {}
    engine = None
    if args.apply:
        engine = engine_from_url()
        companies, existing_sources = load_registry_state(engine)

    rows: list[dict[str, Any]] = []
    resumed = 0
    quota_exhausted = False
    resolver = GoogleDomainResolver(
        args.cache_file,
        project=args.project,
        location=args.location,
        model=args.model,
        delay_seconds=args.delay_seconds,
        retry_delay_seconds=args.retry_delay_seconds,
        max_attempts=args.max_attempts,
        max_pages_per_domain=args.max_pages_per_domain,
    )
    try:
        with resolver:
            for index, candidate in enumerate(selected, start=1):
                token = candidate["board_token"]
                timed_out = False
                prior = resume_rows.get(token)
                if prior is not None and can_resume_row(
                    prior,
                    candidate=candidate,
                    apply=args.apply,
                    existing_source_company_id=existing_sources.get(token),
                ):
                    rows.append(prior)
                    resumed += 1
                else:
                    cache_hits_before = resolver.cache.hits
                    cache_stores_before = resolver.cache.stores
                    try:
                        with company_resolution_deadline(args.company_timeout_seconds):
                            result = resolver.resolve(
                                company_name=candidate["board_name"], board_token=token
                            )
                    except CompanyResolutionDeadlineExceeded:
                        timed_out = True
                        cache_source = "unknown"
                        if resolver.cache.stores > cache_stores_before:
                            cache_source = "network"
                        elif resolver.cache.hits > cache_hits_before:
                            cache_source = "disk"
                        resolver.close()
                        result = DomainResolutionResult(
                            status="request_failed",
                            model=args.model,
                            location=args.location,
                            cache_source=cache_source,
                            error=(f"company_timeout:{args.company_timeout_seconds:g}s"),
                            retryable=True,
                        )
                    row = result_row(candidate, result)
                    if args.apply and result.status == "accepted":
                        assert engine is not None
                        apply_registration(
                            row,
                            result=result,
                            candidate=candidate,
                            companies=companies,
                            existing_sources=existing_sources,
                            engine=engine,
                        )
                    rows.append(row)
                    quota_exhausted = result.quota_exhausted

                if index % args.checkpoint_every == 0 or quota_exhausted or timed_out:
                    checkpoint(
                        args,
                        status,
                        rows,
                        selected_candidates=selected,
                        resumed=resumed,
                        preserved_rows=resume_rows,
                    )
                if quota_exhausted:
                    break
    except BaseException as exc:
        checkpoint(
            args,
            status,
            rows,
            selected_candidates=selected,
            resumed=resumed,
            preserved_rows=resume_rows,
        )
        durable_rows = merge_checkpoint_rows(selected, rows, resume_rows)
        write_status(
            args.status_file,
            stage_finished(
                status,
                state="failed",
                error=exc,
                **summary_counts(durable_rows, selected=len(selected), resumed=resumed),
            ),
        )
        raise

    if quota_exhausted:
        durable_rows = merge_checkpoint_rows(selected, rows, resume_rows)
        write_status(
            args.status_file,
            stage_finished(
                status,
                state="quota_exhausted",
                **summary_counts(durable_rows, selected=len(selected), resumed=resumed),
                checkpoint=str(args.output.with_suffix(".partial.csv")),
            ),
        )
        print_progress(len(durable_rows), len(selected), durable_rows)
        print("Vertex quota exhausted; checkpoint saved for a later resume.", flush=True)
        # Quota is a durable, resumable terminal state for this invocation. Returning
        # success prevents systemd Restart=on-failure from immediately hammering the
        # same exhausted quota; a later explicit invocation retries this row.
        return 0

    write_csv_atomic(args.output, rows)
    write_csv_atomic(args.output.with_suffix(".partial.csv"), rows)
    summary = summary_counts(rows, selected=len(selected), resumed=resumed)
    write_status(
        args.status_file,
        stage_finished(
            status,
            state="partial" if summary["failed"] else "completed",
            **summary,
            output=str(args.output),
        ),
    )
    print_progress(len(rows), len(selected), rows)
    print(f"Wrote {len(rows)} domain-resolution rows to {args.output}")
    return 0


def validate_args(args: argparse.Namespace) -> None:
    if args.offset < 0 or (args.limit is not None and args.limit < 1):
        raise SystemExit("--offset must be non-negative and --limit must be positive")
    if args.checkpoint_every < 1:
        raise SystemExit("--checkpoint-every must be positive")
    if args.max_attempts < 1 or args.max_pages_per_domain < 1:
        raise SystemExit("--max-attempts and --max-pages-per-domain must be positive")
    if args.company_timeout_seconds <= 0:
        raise SystemExit("--company-timeout-seconds must be positive")
    if not args.location:
        raise SystemExit("--location must not be empty")
    if not args.model:
        raise SystemExit("--model must not be empty")
    if not args.project:
        raise SystemExit("--project or GOOGLE_CLOUD_PROJECT is required")


def load_candidates(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        fieldnames = set(reader.fieldnames or ())
        required = {
            "board_token",
            "canonical_source_url",
            "verification_status",
            "board_name",
            "resolution_status",
        }
        missing = required - fieldnames
        if missing:
            raise ValueError(f"scout CSV is missing columns: {', '.join(sorted(missing))}")
        rows = list(reader)

    selected: list[dict[str, str]] = []
    by_token: dict[str, dict[str, str]] = {}
    for raw in rows:
        row = {key: str(value or "").strip() for key, value in raw.items()}
        if row["verification_status"].lower() != "verified":
            continue
        if row["resolution_status"].lower() != "unresolved_no_domain":
            continue
        token = row["board_token"].lower()
        if not row["board_name"]:
            continue
        try:
            expected_url = GREENHOUSE_ADAPTER.canonical_source_url(token)
        except ValueError as exc:
            raise ValueError(f"invalid eligible Greenhouse board token: {token!r}") from exc
        source_token = GREENHOUSE_ADAPTER.extract_board_token(row["canonical_source_url"])
        if source_token != token:
            raise ValueError(
                f"eligible scout canonical source URL does not match board token: {token}"
            )
        row["board_token"] = token
        row["canonical_source_url"] = expected_url
        row.update(
            normalized_crawl_provenance(
                row,
                fallback_crawl=crawl_from_path(path),
            )
        )
        prior = by_token.get(token)
        if prior is not None:
            identity_fields = (
                "canonical_source_url",
                "board_name",
                *CRAWL_PROVENANCE_FIELDS,
            )
            if any(prior[field] != row[field] for field in identity_fields):
                raise ValueError(f"conflicting eligible scout rows for board token: {token}")
            continue
        by_token[token] = row
        selected.append(row)
    return selected


def ensure_checkpoint_manifest(args: argparse.Namespace, selected: list[dict[str, str]]) -> None:
    selected_identity = "\n".join(
        "\t".join(
            str(row.get(field) or "")
            for field in (
                "board_token",
                "board_name",
                "canonical_source_url",
                *CRAWL_PROVENANCE_FIELDS,
            )
        )
        for row in selected
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
        "model": args.model,
        "location": args.location,
        "project": args.project,
        "prompt_version": PROMPT_VERSION,
        "evidence_version": EVIDENCE_VERSION,
        "max_pages_per_domain": args.max_pages_per_domain,
    }
    manifest_path = args.output.with_suffix(".checkpoint.json")
    prior = None if args.no_resume else read_status(manifest_path)
    if prior is not None and prior != payload:
        raise SystemExit(
            f"checkpoint manifest does not match this input/scope: {manifest_path}; "
            "use a different --output or --no-resume"
        )
    write_status(manifest_path, payload)


def existing_resume_path(output: Path) -> Path | None:
    partial = output.with_suffix(".partial.csv")
    if partial.exists():
        return partial
    return output if output.exists() else None


def load_resume_rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        if REQUIRED_RESUME_FIELDS - set(reader.fieldnames or ()):
            return {}
    by_token: dict[str, dict[str, str]] = {}
    for row in rows:
        token = row.get("board_token", "").strip().lower()
        if not token:
            continue
        if token in by_token:
            raise ValueError(f"duplicate resume row for board token: {token}")
        by_token[token] = row
    return by_token


def can_resume_row(
    row: dict[str, str],
    *,
    candidate: dict[str, str],
    apply: bool,
    existing_source_company_id: int | None = None,
) -> bool:
    if any(
        row.get(field, "") != candidate.get(field, "")
        for field in ("board_token", "canonical_source_url", "board_name")
    ):
        return False
    try:
        row_provenance = normalized_crawl_provenance(row, fallback_crawl=None)
        candidate_provenance = normalized_crawl_provenance(
            candidate,
            fallback_crawl=None,
        )
    except ValueError:
        return False
    if row_provenance != candidate_provenance:
        return False
    if str(row.get("retryable") or "").strip().lower() in {"1", "true", "yes"}:
        return False
    if row.get("domain_resolution_status") in RETRYABLE_RESULT_STATUSES:
        return False
    if not apply or row.get("domain_resolution_status") != "accepted":
        return True
    if row.get("registration_status") not in SUCCESSFUL_REGISTRATION_STATUSES:
        return False
    try:
        checkpoint_company_id = int(row.get("company_id") or 0)
    except (TypeError, ValueError):
        return False
    return (
        existing_source_company_id is not None
        and checkpoint_company_id == existing_source_company_id
    )


def result_row(candidate: dict[str, str], result: DomainResolutionResult) -> dict[str, Any]:
    provenance = normalized_crawl_provenance(candidate, fallback_crawl=None)
    return {
        "board_token": candidate["board_token"],
        "canonical_source_url": candidate["canonical_source_url"],
        "example_observed_url": candidate.get("example_observed_url", ""),
        "observation_count": candidate.get("observation_count", ""),
        **provenance,
        "verification_status": candidate["verification_status"],
        "board_name": candidate["board_name"],
        "job_count": candidate.get("job_count", ""),
        "resolution_status": candidate["resolution_status"],
        "domain_resolution_status": result.status,
        "accepted_domain": result.accepted_domain or "",
        "website_candidate": result.website_candidate or "",
        "generated_text": result.generated_text,
        "search_query_count": result.search_query_count,
        "search_queries": json.dumps(result.search_queries),
        "citation_count": result.citation_count,
        "citations": citations_json(result),
        "grounding_metadata": json.dumps(result.grounding_metadata or {}, sort_keys=True),
        "candidate_domain_count": result.candidate_domain_count,
        "passing_domain_count": result.passing_domain_count,
        "candidate_evidence": result_evidence_json(result),
        "model": result.model,
        "location": result.location,
        "prompt_token_count": result.prompt_token_count,
        "candidates_token_count": result.candidates_token_count,
        "total_token_count": result.total_token_count,
        "thoughts_token_count": result.thoughts_token_count,
        "cached_content_token_count": result.cached_content_token_count,
        "cache_source": result.cache_source,
        "request_attempt_count": result.request_attempt_count,
        "retryable": str(result.retryable).lower(),
        "quota_exhausted": str(result.quota_exhausted).lower(),
        "registry_resolution_status": "",
        "company_id": "",
        "career_source_id": "",
        "registration_status": "not_requested",
        "error": result.error or "",
        "checked_at": datetime.now(UTC).isoformat(),
    }


def load_registry_state(engine: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
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
    result: DomainResolutionResult,
    candidate: dict[str, str],
    companies: list[dict[str, Any]],
    existing_sources: dict[str, int],
    engine: Any,
) -> None:
    if result.status != "accepted":
        return

    try:
        token, website = validate_apply_invariants(
            row=row,
            result=result,
            candidate=candidate,
        )
        job_count = parse_non_negative_job_count(candidate.get("job_count"))
        evidence = GreenhouseBoardEvidence(
            board_token=token,
            verification_status="verified",
            http_status=200,
            company_name=candidate["board_name"],
            job_count=job_count,
            external_job_origins=(website,),
        )
        resolution = resolve_company(
            evidence,
            companies=companies,
            existing_source_company_id=existing_sources.get(token),
        )
        row["registry_resolution_status"] = resolution.status
        if resolution.status == "already_registered":
            row["company_id"] = resolution.company_id or ""
            row["registration_status"] = "source_existing"
            return
        if resolution.status not in {"existing_exact_name", "new_company_domain_candidate"}:
            row["registration_status"] = "identity_conflict"
            row["error"] = resolution.reason or resolution.status
            return

        if resolution.status == "new_company_domain_candidate":
            company = CompanyRegistry(engine).register_company(
                name=candidate["board_name"], website=website
            )
            company_id = company.company_id
            company_status = "company_created" if company.company_created else "company_reused"
        else:
            company_id = int(resolution.company_id)
            company_status = "company_reused"
        source = JobSourceRegistry(engine).register_url(
            company_id=company_id,
            provider="greenhouse",
            source_url=candidate["canonical_source_url"],
            discovered_from_url=candidate.get("example_observed_url") or None,
            evidence={
                "discovery_provider": "vertex_google_search",
                "observation_count": int(candidate.get("observation_count") or 0),
                **registration_crawl_provenance(candidate),
                "google_candidate_is_identity_proof": False,
                "deterministic_brand_and_reciprocal_link": True,
                "accepted_domain": result.accepted_domain,
                "search_query_count": result.search_query_count,
                "citation_count": result.citation_count,
                "model": result.model,
                "prompt_version": PROMPT_VERSION,
                "evidence_version": EVIDENCE_VERSION,
                "company_domain_matches": accepted_company_domain_matches(result),
                "accepted_proof": accepted_proof(result),
            },
        )
        row["company_id"] = company_id
        row["career_source_id"] = source.career_source_id
        row["registration_status"] = (
            f"{company_status}_source_created" if source.created else "source_existing"
        )
        existing_sources[token] = company_id
        if not any(int(company["id"]) == company_id for company in companies):
            companies.append(
                {
                    "id": company_id,
                    "name": candidate["board_name"],
                    "primary_domain": (urlparse(website).hostname or "").removeprefix("www."),
                }
            )
    except (TypeError, ValueError, SQLAlchemyError) as exc:
        row["registration_status"] = "registration_failed"
        row["error"] = f"{type(exc).__name__}:{exc}"[:500]


def validate_apply_invariants(
    *,
    row: dict[str, Any],
    result: DomainResolutionResult,
    candidate: dict[str, str],
) -> tuple[str, str]:
    """Recheck identity and deterministic proof immediately before DB mutation."""

    token = candidate.get("board_token", "")
    if token != token.strip().lower():
        raise ValueError("apply candidate board token is not canonical")
    try:
        canonical_url = GREENHOUSE_ADAPTER.canonical_source_url(token)
    except ValueError as exc:
        raise ValueError("apply candidate has an invalid Greenhouse board token") from exc
    if candidate.get("canonical_source_url") != canonical_url:
        raise ValueError("apply candidate canonical source URL does not match board token")
    if row.get("board_token") != token or row.get("canonical_source_url") != canonical_url:
        raise ValueError("accepted row identity does not match apply candidate")
    expected_provenance = normalized_crawl_provenance(candidate, fallback_crawl=None)
    if any(
        str(row.get(field) or "") != expected_provenance[field]
        for field in CRAWL_PROVENANCE_FIELDS
    ):
        raise ValueError("accepted row does not preserve candidate crawl provenance")

    accepted_domain = result.accepted_domain
    if not accepted_domain or acceptable_company_domain(accepted_domain) != accepted_domain:
        raise ValueError("accepted result has an invalid canonical company domain")
    website = f"https://{accepted_domain}"
    if result.website_candidate != website:
        raise ValueError("accepted result website does not match accepted domain")
    if result.retryable or result.quota_exhausted:
        raise ValueError("accepted result cannot be retryable or quota exhausted")

    passing = [evidence for evidence in result.candidate_evidence if evidence.passed]
    if len(passing) != 1 or passing[0].domain != accepted_domain:
        raise ValueError("accepted result must have exactly one matching passing domain")
    accepted = passing[0]
    brand_proven = any(page.brand_matches for page in accepted.pages)
    reciprocal_link_proven = any(
        GREENHOUSE_ADAPTER.extract_board_token(link) == token
        for page in accepted.pages
        for link in page.greenhouse_links
    )
    expected_domain_matches = find_company_domain_matches(accepted_domain, candidate["board_name"])
    if not (
        accepted.brand_valid
        and brand_proven
        and accepted.reciprocal_link_valid
        and reciprocal_link_proven
        and accepted.company_domain_compatible
        and expected_domain_matches
        and accepted.company_domain_matches == expected_domain_matches
    ):
        raise ValueError("accepted result is missing deterministic identity proof")
    if any(
        evidence.retryable and evidence.company_domain_compatible and not evidence.passed
        for evidence in result.candidate_evidence
    ):
        raise ValueError("accepted result contains incomplete retryable identity evidence")

    expected_row_fields: dict[str, Any] = {
        "domain_resolution_status": "accepted",
        "accepted_domain": accepted_domain,
        "website_candidate": website,
        "candidate_domain_count": len(result.candidate_evidence),
        "passing_domain_count": 1,
        "candidate_evidence": result_evidence_json(result),
    }
    if any(row.get(field) != value for field, value in expected_row_fields.items()):
        raise ValueError("accepted row does not preserve the validated result proof")
    return token, website


def parse_non_negative_job_count(value: Any) -> int:
    raw = str(value or "0").strip()
    try:
        job_count = int(raw or "0")
    except ValueError as exc:
        raise ValueError("job_count must be a non-negative integer") from exc
    if job_count < 0:
        raise ValueError("job_count must be a non-negative integer")
    return job_count


def normalized_crawl_provenance(
    candidate: dict[str, Any],
    *,
    fallback_crawl: str | None,
) -> dict[str, str]:
    """Validate and canonicalize optional union or legacy single-crawl evidence."""

    first_observed_raw = str(candidate.get("first_observed_at") or "").strip()
    last_observed_raw = str(candidate.get("last_observed_at") or "").strip()
    if bool(first_observed_raw) != bool(last_observed_raw):
        raise ValueError("first_observed_at and last_observed_at must be supplied together")
    first_observed = (
        parsed_provenance_timestamp(first_observed_raw, field="first_observed_at")
        if first_observed_raw
        else None
    )
    last_observed = (
        parsed_provenance_timestamp(last_observed_raw, field="last_observed_at")
        if last_observed_raw
        else None
    )
    if first_observed is not None and last_observed is not None and first_observed > last_observed:
        raise ValueError("first_observed_at is after last_observed_at")

    raw_crawl_ids = str(candidate.get("crawl_ids") or "").strip()
    if raw_crawl_ids:
        try:
            parsed_crawl_ids = json.loads(raw_crawl_ids)
        except json.JSONDecodeError as exc:
            raise ValueError("crawl_ids must be a JSON array") from exc
        if not isinstance(parsed_crawl_ids, list) or not parsed_crawl_ids:
            raise ValueError("crawl_ids must be a non-empty JSON array")
        crawl_ids = sorted(
            {str(value) for value in parsed_crawl_ids},
            key=crawl_sort_key,
        )
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
            if crawl_count < 1:
                raise ValueError("crawl_count must be a positive integer")
            if crawl_count != len(crawl_ids):
                raise ValueError("crawl_count does not match crawl_ids")
        else:
            crawl_count = len(crawl_ids)
    else:
        crawl_count = 0
        if first_seen or last_seen or raw_count:
            raise ValueError("crawl summary fields require crawl_ids or a single-crawl filename")

    return {
        "first_observed_at": format_provenance_timestamp(first_observed),
        "last_observed_at": format_provenance_timestamp(last_observed),
        "first_seen_crawl": crawl_ids[0] if crawl_ids else "",
        "last_seen_crawl": crawl_ids[-1] if crawl_ids else "",
        "crawl_count": str(crawl_count) if crawl_ids else "",
        "crawl_ids": json.dumps(crawl_ids, separators=(",", ":")) if crawl_ids else "",
    }


def registration_crawl_provenance(candidate: dict[str, Any]) -> dict[str, Any]:
    normalized = normalized_crawl_provenance(candidate, fallback_crawl=None)
    provenance: dict[str, Any] = {
        field: normalized[field]
        for field in ("first_observed_at", "last_observed_at")
        if normalized[field]
    }
    if normalized["crawl_ids"]:
        crawl_ids = json.loads(normalized["crawl_ids"])
        provenance.update(
            {
                "first_seen_crawl": normalized["first_seen_crawl"],
                "last_seen_crawl": normalized["last_seen_crawl"],
                "crawl_count": int(normalized["crawl_count"]),
                "crawl_ids": crawl_ids,
            }
        )
        if len(crawl_ids) == 1:
            provenance["crawl"] = crawl_ids[0]
    return provenance


def parsed_provenance_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_provenance_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def crawl_sort_key(crawl_id: str) -> tuple[int, int]:
    if not CRAWL_RE.fullmatch(crawl_id):
        raise ValueError(f"invalid crawl ID: {crawl_id!r}")
    _, _, year, week = crawl_id.split("-")
    return int(year), int(week)


def crawl_from_path(path: Path) -> str | None:
    matches = re.findall(r"CC-MAIN-\d{4}-\d{2}", path.name)
    return matches[0] if len(matches) == 1 else None


def accepted_proof(result: DomainResolutionResult) -> list[dict[str, Any]]:
    """Return bounded public deterministic proof, excluding raw Google response data."""
    accepted = next(
        (
            evidence
            for evidence in result.candidate_evidence
            if evidence.domain == result.accepted_domain and evidence.passed
        ),
        None,
    )
    if accepted is None:
        return []
    proof: list[dict[str, Any]] = []
    for page in accepted.pages:
        if not page.brand_matches and not page.greenhouse_links:
            continue
        proof.append(
            {
                "page_url": sanitized_url(page.final_url, keep_query=False),
                "brand_match_kinds": sorted(
                    {match.partition(":")[0] for match in page.brand_matches}
                ),
                "greenhouse_links": [
                    sanitized_url(link, keep_query=True) for link in page.greenhouse_links
                ],
            }
        )
    return proof[:3]


def accepted_company_domain_matches(result: DomainResolutionResult) -> list[str]:
    accepted = next(
        (
            evidence
            for evidence in result.candidate_evidence
            if evidence.domain == result.accepted_domain and evidence.passed
        ),
        None,
    )
    return list(accepted.company_domain_matches) if accepted else []


def sanitized_url(value: str, *, keep_query: bool) -> str:
    parsed = urlparse(value)
    return urlunparse(
        (
            parsed.scheme,
            parsed.hostname or "",
            parsed.path,
            parsed.params,
            parsed.query if keep_query else "",
            "",
        )
    )


def checkpoint(
    args: argparse.Namespace,
    status: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    selected_candidates: list[dict[str, str]],
    resumed: int,
    preserved_rows: dict[str, dict[str, str]],
) -> None:
    partial = args.output.with_suffix(".partial.csv")
    durable_rows = merge_checkpoint_rows(selected_candidates, rows, preserved_rows)
    write_csv_atomic(partial, durable_rows)
    selected_count = len(selected_candidates)
    summary = summary_counts(durable_rows, selected=selected_count, resumed=resumed)
    write_status(
        args.status_file,
        stage_checkpoint(status, **summary, checkpoint=str(partial)),
    )
    print_progress(len(durable_rows), selected_count, durable_rows)


def merge_checkpoint_rows(
    selected_candidates: list[dict[str, str]],
    rows: list[dict[str, Any]],
    preserved_rows: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Overlay visited rows without dropping the unvisited tail of a prior checkpoint."""

    merged: dict[str, dict[str, Any]] = {token: dict(row) for token, row in preserved_rows.items()}
    for row in rows:
        token = str(row.get("board_token") or "").strip().lower()
        if token:
            merged[token] = row
    return [
        merged[token]
        for candidate in selected_candidates
        if (token := candidate["board_token"].strip().lower()) in merged
    ]


def summary_counts(rows: list[dict[str, Any]], *, selected: int, resumed: int) -> dict[str, Any]:
    outcomes = Counter(str(row.get("domain_resolution_status") or "") for row in rows)

    def summed(field: str) -> int:
        total = 0
        for row in rows:
            try:
                total += int(row.get(field) or 0)
            except (TypeError, ValueError):
                continue
        return total

    registration_outcomes = Counter(str(row.get("registration_status") or "") for row in rows)
    registration_failed = registration_outcomes["registration_failed"]
    failed = outcomes["request_failed"] + outcomes["quota_exhausted"] + registration_failed
    retryable = sum(
        str(row.get("retryable") or "").strip().lower() in {"1", "true", "yes"} for row in rows
    )
    return {
        "selected": selected,
        "processed": len(rows),
        "succeeded": len(rows) - failed,
        "failed": failed,
        "retryable": retryable,
        "registration_failed": registration_failed,
        "registration_conflicts": registration_outcomes["identity_conflict"],
        "resumed": resumed,
        "accepted": outcomes["accepted"],
        "ambiguous": outcomes["ambiguous"],
        "manual_review": outcomes["manual_review"],
        "unresolved": outcomes["unresolved"],
        "network_requests": sum(row.get("cache_source") == "network" for row in rows),
        "cache_hits": sum(row.get("cache_source") == "disk" for row in rows),
        "request_attempt_count": summed("request_attempt_count"),
        "search_query_count": summed("search_query_count"),
        "prompt_token_count": summed("prompt_token_count"),
        "candidates_token_count": summed("candidates_token_count"),
        "total_token_count": summed("total_token_count"),
        "thoughts_token_count": summed("thoughts_token_count"),
        "cached_content_token_count": summed("cached_content_token_count"),
    }


def write_csv_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
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
    outcomes = Counter(str(row.get("domain_resolution_status") or "") for row in rows)
    print(f"processed={processed}/{selected} outcomes={dict(outcomes)}", flush=True)


if __name__ == "__main__":
    main()
