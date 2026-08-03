#!/usr/bin/env python3
"""Load checked-in YC snapshots into the unified company/source/job store."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

from yc_radar.core.config import get_settings
from yc_radar.services.company_registry import sync_yc_job_snapshots
from yc_radar.services.database import engine_from_url, upsert_yc_companies


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Load YC CSV snapshots into Postgres.")
    parser.add_argument("--snapshot-dir", type=Path, default=settings.snapshots_dir)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    companies_path = args.snapshot_dir / "yc_companies.csv"
    jobs_path = args.snapshot_dir / "yc_job_postings.csv"

    companies = [company_payload(row) for row in read_csv(companies_path)]
    jobs = [job_payload(row) for row in read_csv(jobs_path)]

    engine = engine_from_url()
    upsert_yc_companies(engine, companies)
    sync_yc_job_snapshots(
        engine,
        jobs,
        complete_company_slugs={
            str(company.get("slug") or "") for company in companies if company.get("slug")
        },
    )

    print(f"Loaded {len(companies)} companies from {companies_path}")
    print(f"Loaded {len(jobs)} YC job postings from {jobs_path}")
    print(f"Updated {engine.url.database}")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def company_payload(row: dict[str, str]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "slug": row.get("slug"),
        "website": blank_to_none(row.get("website")),
        "one_liner": blank_to_none(row.get("one_liner")),
        "batch": blank_to_none(row.get("batch")),
        "status": blank_to_none(row.get("status")),
        "stage": blank_to_none(row.get("stage")),
        "team_size": blank_to_none(row.get("team_size")),
        "isHiring": parse_bool(row.get("isHiring")),
        "all_locations": blank_to_none(row.get("all_locations")),
        "regions": split_semicolon(row.get("regions")),
        "industry": blank_to_none(row.get("industry")),
        "subindustry": blank_to_none(row.get("subindustry")),
        "industries": split_semicolon(row.get("industries")),
        "tags": split_semicolon(row.get("tags")),
        "prototype_score": blank_to_none(row.get("prototype_score")),
        "prototype_angle": blank_to_none(row.get("prototype_angle")),
    }


def job_payload(row: dict[str, str]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "company_id": row.get("company_id"),
        "company_slug": row.get("company_slug"),
        "company_name": row.get("company_name"),
        "company_yc_url": row.get("company_yc_url"),
        "title": row.get("title"),
        "url": row.get("url"),
        "applyUrl": blank_to_none(row.get("apply_url")),
        "location": blank_to_none(row.get("location")),
        "type": blank_to_none(row.get("type")),
        "role": blank_to_none(row.get("role")),
        "roleSpecificType": blank_to_none(row.get("role_specific_type")),
        "prettyRole": blank_to_none(row.get("pretty_role")),
        "salaryRange": blank_to_none(row.get("salary_range")),
        "equityRange": blank_to_none(row.get("equity_range")),
        "minExperience": blank_to_none(row.get("min_experience")),
        "minSchoolYear": blank_to_none(row.get("min_school_year")),
        "visa": blank_to_none(row.get("visa")),
        "skills": split_semicolon(row.get("skills")),
        "isIncomplete": parse_bool(row.get("is_incomplete")),
        "createdAt": blank_to_none(row.get("created_at")),
        "lastActive": blank_to_none(row.get("last_active")),
    }


def split_semicolon(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


def blank_to_none(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def parse_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


if __name__ == "__main__":
    main()
