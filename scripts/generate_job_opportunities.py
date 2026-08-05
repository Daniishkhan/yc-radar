#!/usr/bin/env python3
"""Export the source-neutral public job inventory for local inspection."""

from __future__ import annotations

import argparse
import csv
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from yc_radar.core.config import get_settings
from yc_radar.services.artifact_generation import (
    ArtifactGenerationLocked,
    artifact_generation_lock,
    atomic_text_writer,
    atomic_write_json,
)
from yc_radar.services.application_pool import build_application_pool
from yc_radar.services.candidate_fit import (
    classify_remote_eligibility,
    classify_role_text,
    job_seniority,
)
from yc_radar.services.database import create_schema, engine_from_url
from yc_radar.services.job_repository import (
    DEFAULT_OBSERVATION_MAX_AGE_DAYS,
    JobRepository,
)

CSV_FIELDS = [
    "company_name",
    "company_slug",
    "title",
    "role_match_status",
    "role_match_reasons",
    "remote_eligibility_status",
    "remote_eligibility_reasons",
    "remote_eligibility_evidence",
    "job_key",
    "source_kind",
    "origin_kind",
    "source_record_id",
    "provider",
    "external_job_id",
    "company_source_id",
    "source_external_id",
    "source_url",
    "source_enabled",
    "source_sync_status",
    "source_last_synced_at",
    "posting_url",
    "apply_url",
    "location",
    "department",
    "employment_type",
    "status",
    "lifecycle_managed",
    "status_confidence",
    "source_published_at",
    "source_updated_at",
    "observed_at",
    "last_changed_at",
]
QUEUE_CSV_FIELDS = [
    "priority_score",
    "application_lane",
    "application_lane_reason",
    "application_url",
    "freshness_age_days",
    "freshness_band",
    *CSV_FIELDS,
]
ROLE_MATCH_STATUSES = ("strong", "possible", "weak", "exclude")
REMOTE_ELIGIBILITY_STATUSES = (
    "pakistan_explicit",
    "global_explicit",
    "regional_unconfirmed",
    "remote_unclear",
    "restricted_remote",
    "onsite_explicit",
    "no_remote_evidence",
)


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Export source-neutral public job opportunities.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--include-closed", action="store_true")
    parser.add_argument("--changed-since", help="ISO-8601 timestamp for last_changed_at filtering.")
    parser.add_argument("--provider")
    parser.add_argument("--source-kind")
    parser.add_argument("--origin-kind")
    parser.add_argument("--company-slug")
    parser.add_argument(
        "--role-status",
        action="append",
        choices=ROLE_MATCH_STATUSES,
        default=[],
        help="Keep only the selected role classification; repeat for multiple statuses.",
    )
    parser.add_argument(
        "--remote-status",
        action="append",
        choices=REMOTE_ELIGIBILITY_STATUSES,
        default=[],
        help="Keep only the selected remote classification; repeat for multiple statuses.",
    )
    freshness_group = parser.add_mutually_exclusive_group()
    freshness_group.add_argument(
        "--observation-max-age-days",
        type=non_negative_int,
        metavar="DAYS",
        help=(
            "Exclude older observation-mode jobs "
            f"(default: {DEFAULT_OBSERVATION_MAX_AGE_DAYS} days)."
        ),
    )
    freshness_group.add_argument(
        "--no-observation-age-filter",
        dest="observation_max_age_days",
        action="store_const",
        const=None,
        help="Include observation-mode jobs regardless of when they were last seen.",
    )
    application_freshness_group = parser.add_mutually_exclusive_group()
    application_freshness_group.add_argument(
        "--application-max-age-days",
        type=non_negative_int,
        metavar="DAYS",
        help="Exclude older postings from application and verification queues (default: 90).",
    )
    application_freshness_group.add_argument(
        "--no-application-age-filter",
        dest="application_max_age_days",
        action="store_const",
        const=None,
        help="Allow old postings into queues while retaining their freshness ranking penalty.",
    )
    parser.add_argument(
        "--queue-limit",
        type=non_negative_int,
        default=500,
        help="Maximum rows per application or verification queue (default: 500).",
    )
    parser.add_argument(
        "--as-of",
        help="ISO-8601 reference time for deterministic freshness ranking.",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.set_defaults(
        default_output_root=settings.runs_dir,
        observation_max_age_days=DEFAULT_OBSERVATION_MAX_AGE_DAYS,
        application_max_age_days=90,
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    settings = get_settings()
    changed_since = parse_timestamp(args.changed_since) if args.changed_since else None
    as_of = parse_timestamp(args.as_of) if args.as_of else datetime.now(UTC)
    output_dir = args.output_dir or args.default_output_root / args.date
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with artifact_generation_lock(output_dir=output_dir, local_dir=settings.local_dir):
            generate_artifacts(
                args=args,
                output_dir=output_dir,
                changed_since=changed_since,
                as_of=as_of,
            )
    except ArtifactGenerationLocked as exc:
        raise SystemExit(str(exc)) from exc


def generate_artifacts(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    changed_since: datetime | None,
    as_of: datetime,
) -> None:
    engine = engine_from_url()
    create_schema(engine)
    has_post_filters = bool(args.role_status or args.remote_status)
    rows = JobRepository(engine).list_jobs(
        include_closed=args.include_closed,
        changed_since=changed_since,
        provider=args.provider,
        company_slug=args.company_slug,
        source_kind=args.source_kind,
        origin_kind=args.origin_kind,
        observation_max_age_days=args.observation_max_age_days,
        limit=None if has_post_filters else args.limit,
    )
    payload_rows = [opportunity_row(row) for row in rows]
    payload_rows = filter_opportunity_rows(
        payload_rows,
        role_statuses=args.role_status,
        remote_statuses=args.remote_status,
        limit=args.limit,
    )
    json_path, csv_path = write_job_rows(
        output_dir=output_dir,
        stem="job_opportunities",
        rows=payload_rows,
        fields=CSV_FIELDS,
    )
    pool = build_application_pool(
        payload_rows,
        as_of=as_of,
        application_max_age_days=args.application_max_age_days,
        limit_per_queue=args.queue_limit,
    )
    application_json, application_csv = write_job_rows(
        output_dir=output_dir,
        stem="application_queue",
        rows=pool["application_queue"],
        fields=QUEUE_CSV_FIELDS,
    )
    verification_json, verification_csv = write_job_rows(
        output_dir=output_dir,
        stem="verification_queue",
        rows=pool["verification_queue"],
        fields=QUEUE_CSV_FIELDS,
    )
    summary_path = output_dir / "application_pool_summary.json"
    atomic_write_json(
        summary_path,
        pool["summary"],
        default=str,
        indent=2,
        trailing_newline=True,
    )
    print(f"Wrote {len(payload_rows)} job opportunities: {json_path}")
    print(f"Wrote CSV: {csv_path}")
    print(
        f"Wrote {len(pool['application_queue'])} application candidates: "
        f"{application_json} and {application_csv}"
    )
    print(
        f"Wrote {len(pool['verification_queue'])} verification candidates: "
        f"{verification_json} and {verification_csv}"
    )
    print(f"Wrote application-pool metrics: {summary_path}")


def opportunity_row(row: dict[str, Any]) -> dict[str, Any]:
    classification = classify_role_text(
        str(row["title"]),
        " ".join(
            str(value)
            for value in (row.get("description_text"), row.get("department"), row.get("location"))
            if value
        ),
        seniority=job_seniority(row),
    )
    remote_eligibility = classify_remote_eligibility(row)
    return {
        field: row.get(field)
        for field in CSV_FIELDS
    } | {
        "role_match_status": classification.status,
        "role_match_reasons": classification.reasons,
        "remote_eligibility_status": remote_eligibility.status,
        "remote_eligibility_reasons": remote_eligibility.reasons,
        "remote_eligibility_evidence": remote_eligibility.evidence,
    }


def parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"Invalid ISO-8601 timestamp: {value}") from exc


def filter_opportunity_rows(
    rows: Sequence[dict[str, Any]],
    *,
    role_statuses: Sequence[str] = (),
    remote_statuses: Sequence[str] = (),
    limit: int | None = None,
) -> list[dict[str, Any]]:
    selected_roles = set(role_statuses)
    selected_remote = set(remote_statuses)
    filtered = [
        row
        for row in rows
        if (
            not selected_roles
            or str(row.get("role_match_status")) in selected_roles
        )
        and (
            not selected_remote
            or str(row.get("remote_eligibility_status")) in selected_remote
        )
    ]
    return filtered if limit is None else filtered[:limit]


def csv_value(value: Any) -> str | int | float | bool | None:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return value


def write_job_rows(
    *,
    output_dir: Path,
    stem: str,
    rows: Sequence[dict[str, Any]],
    fields: Sequence[str],
) -> tuple[Path, Path]:
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"
    atomic_write_json(
        json_path,
        {"jobs": list(rows)},
        default=str,
        indent=2,
        trailing_newline=True,
    )
    with atomic_text_writer(csv_path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})
    return json_path, csv_path


if __name__ == "__main__":
    main()
