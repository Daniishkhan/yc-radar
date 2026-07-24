#!/usr/bin/env python3
"""Export public canonical job opportunities for local inspection."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from yc_radar.core.config import get_settings
from yc_radar.services.candidate_fit import classify_role_text
from yc_radar.services.database import create_schema, engine_from_url
from yc_radar.services.job_repository import JobRepository

CSV_FIELDS = [
    "company_name",
    "company_slug",
    "title",
    "role_match_status",
    "role_match_reasons",
    "provider",
    "external_job_id",
    "career_source_kind",
    "career_source_url",
    "posting_url",
    "apply_url",
    "location",
    "department",
    "employment_type",
    "status",
    "source_published_at",
    "source_updated_at",
    "last_changed_at",
]


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Export canonical public job opportunities.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--include-closed", action="store_true")
    parser.add_argument("--changed-since", help="ISO-8601 timestamp for last_changed_at filtering.")
    parser.add_argument("--provider")
    parser.add_argument("--company-slug")
    parser.add_argument("--limit", type=int, default=100)
    parser.set_defaults(default_output_root=settings.runs_dir)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    changed_since = parse_timestamp(args.changed_since) if args.changed_since else None
    output_dir = args.output_dir or args.default_output_root / args.date
    output_dir.mkdir(parents=True, exist_ok=True)
    engine = engine_from_url()
    create_schema(engine)
    rows = JobRepository(engine).active_job_rows(
        include_closed=args.include_closed,
        changed_since=changed_since,
        provider=args.provider,
        company_slug=args.company_slug,
        limit=args.limit,
    )
    payload_rows = [opportunity_row(row) for row in rows]
    json_path = output_dir / "job_opportunities.json"
    csv_path = output_dir / "job_opportunities.csv"
    json_path.write_text(json.dumps({"jobs": payload_rows}, default=str, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in payload_rows:
            writer.writerow({field: csv_value(row.get(field)) for field in CSV_FIELDS})
    print(f"Wrote {len(payload_rows)} job opportunities: {json_path}")
    print(f"Wrote CSV: {csv_path}")


def opportunity_row(row: dict[str, Any]) -> dict[str, Any]:
    classification = classify_role_text(
        str(row["title"]),
        " ".join(
            str(value)
            for value in (row.get("description_text"), row.get("department"), row.get("location"))
            if value
        ),
    )
    return {
        field: row.get(field)
        for field in CSV_FIELDS
    } | {
        "role_match_status": classification.status,
        "role_match_reasons": classification.reasons,
    }


def parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"Invalid --changed-since timestamp: {value}") from exc


def csv_value(value: Any) -> str | int | float | bool | None:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return value


if __name__ == "__main__":
    main()
