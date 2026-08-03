from argparse import Namespace
import csv
from datetime import UTC, datetime
import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy import func, select

from yc_radar.services.company_registry import CompanyRegistry, sync_yc_job_snapshots
from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    engine_from_url,
    jobs_table,
    sync_runs_table,
    upsert_yc_companies,
)


def _load_script(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"yc_radar_test_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract_yc_companies = _load_script("extract_yc_companies")
load_snapshots = _load_script("load_snapshots")


def test_extraction_does_not_duplicate_jobs_in_company_source_metadata() -> None:
    company = {
        "id": 42,
        "name": "Lean Source",
        "jobPostings": [{"id": 100, "title": "Software Engineer"}],
        "batch": "S24",
    }

    payload = extract_yc_companies.company_persistence_payload(company)

    assert payload == {"id": 42, "name": "Lean Source", "batch": "S24"}
    assert company["jobPostings"] == [{"id": 100, "title": "Software Engineer"}]


def test_checked_in_snapshot_loads_yc_into_unified_sources_and_jobs(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = engine_from_url(postgres_database_url)
    existing = CompanyRegistry(engine).register_company(
        name="Shared Employer",
        website="https://shared.example",
        requested_slug="shared-employer",
    )
    company_row = _csv_row(
        extract_yc_companies.CSV_FIELDS,
        {
            "id": "99",
            "name": "Shared Employer",
            "slug": "shared-employer-yc",
            "yc_url": "https://www.ycombinator.com/companies/shared-employer-yc",
            "website": "https://shared.example/about",
            "batch": "S24",
            "isHiring": "true",
            "regions": "Remote",
            "industries": "B2B",
            "tags": "Developer Tools",
            "job_count": "1",
        },
    )
    job_row = _csv_row(
        extract_yc_companies.JOB_CSV_FIELDS,
        {
            "id": "1001",
            "company_id": "99",
            "company_slug": "shared-employer-yc",
            "company_name": "Shared Employer",
            "company_yc_url": "https://www.ycombinator.com/companies/shared-employer-yc",
            "title": "Backend Engineer",
            "url": "/companies/shared-employer-yc/jobs/backend-engineer",
            "absolute_url": (
                "https://www.ycombinator.com/companies/shared-employer-yc/"
                "jobs/backend-engineer"
            ),
            "location": "Remote",
            "type": "Full-time",
            "role": "Engineering",
            "skills": "Python; PostgreSQL",
            "is_incomplete": "false",
        },
    )
    _write_csv(tmp_path / "yc_companies.csv", extract_yc_companies.CSV_FIELDS, [company_row])
    _write_csv(
        tmp_path / "yc_job_postings.csv",
        extract_yc_companies.JOB_CSV_FIELDS,
        [job_row],
    )
    monkeypatch.setattr(load_snapshots, "parse_args", lambda: Namespace(snapshot_dir=tmp_path))
    monkeypatch.setattr(load_snapshots, "engine_from_url", lambda: engine)

    load_snapshots.main()

    with engine.connect() as connection:
        companies = list(connection.execute(select(companies_table)).mappings())
        source = connection.execute(select(company_sources_table)).mappings().one()
        job = connection.execute(select(jobs_table)).mappings().one()
        run = connection.execute(select(sync_runs_table)).mappings().one()
        assert connection.scalar(select(func.count()).select_from(jobs_table)) == 1

    assert len(companies) == 1
    assert source["company_id"] == existing.company_id
    assert source["provider"] == "yc"
    assert source["external_id"] == "99"
    assert source["sync_mode"] == "complete_snapshot"
    assert source["metadata"]["slug"] == "shared-employer-yc"
    assert source["metadata"]["batch"] == "S24"
    assert "company_id" not in job
    assert job["company_source_id"] == source["id"]
    assert job["external_job_id"] == "1001"
    assert job["title"] == "Backend Engineer"
    assert job["status"] == "active"
    assert job["structured_evidence"]["skills"] == ["Python", "PostgreSQL"]
    assert run["company_source_id"] == source["id"]
    assert run["status"] == "completed"
    assert run["is_complete"] is True


def test_yc_snapshots_use_the_shared_two_miss_lifecycle(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    company = {
        "id": 501,
        "name": "Lifecycle YC",
        "slug": "lifecycle-yc",
        "website": "https://lifecycle.example",
    }
    job = {
        "id": 502,
        "company_id": 501,
        "company_slug": "lifecycle-yc",
        "title": "Software Engineer",
        "url": "/companies/lifecycle-yc/jobs/software-engineer",
        "location": "Remote",
        "skills": ["Python"],
    }
    upsert_yc_companies(engine, [company])

    sync_yc_job_snapshots(
        engine,
        [job],
        complete_company_slugs={"lifecycle-yc"},
        observed_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    sync_yc_job_snapshots(
        engine,
        [],
        complete_company_slugs={"lifecycle-yc"},
        observed_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    with engine.connect() as connection:
        missed_once = connection.execute(select(jobs_table)).mappings().one()
    assert missed_once["status"] == "active"
    assert missed_once["consecutive_complete_misses"] == 1

    sync_yc_job_snapshots(
        engine,
        [],
        complete_company_slugs={"lifecycle-yc"},
        observed_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    with engine.connect() as connection:
        closed = connection.execute(select(jobs_table)).mappings().one()
    assert closed["status"] == "closed"
    assert closed["consecutive_complete_misses"] == 2

    sync_yc_job_snapshots(
        engine,
        [job],
        complete_company_slugs={"lifecycle-yc"},
        observed_at=datetime(2026, 8, 4, tzinfo=UTC),
    )
    with engine.connect() as connection:
        reactivated = connection.execute(select(jobs_table)).mappings().one()
        run_count = connection.scalar(select(func.count()).select_from(sync_runs_table))
    assert reactivated["status"] == "active"
    assert reactivated["consecutive_complete_misses"] == 0
    assert reactivated["closed_at"] is None
    assert run_count == 4


def _csv_row(fields: list[str], values: dict[str, str]) -> dict[str, str]:
    return {field: values.get(field, "") for field in fields}


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
