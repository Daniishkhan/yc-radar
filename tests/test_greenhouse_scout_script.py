import argparse
import csv
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from yc_radar.services.database import companies_table, company_sources_table, engine_from_url


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "scout_greenhouse_sources.py"
SPEC = importlib.util.spec_from_file_location("scout_greenhouse_sources", SCRIPT_PATH)
assert SPEC and SPEC.loader
scout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scout)


def candidate(token: str = "acme") -> dict[str, str]:
    return {
        "board_token": token,
        "canonical_source_url": f"https://job-boards.greenhouse.io/{token}",
    }


def result(**overrides: str) -> dict[str, str]:
    row = {
        "board_token": "acme",
        "canonical_source_url": "https://job-boards.greenhouse.io/acme",
        "verification_status": "verified",
        "resolution_status": "new_company_domain_candidate",
        "registration_status": "company_created_source_created",
    }
    row.update(overrides)
    return row


def test_resume_reuses_completed_rows_and_retries_failures() -> None:
    assert scout.can_resume_row(result(), candidate=candidate(), apply=True) is True
    assert (
        scout.can_resume_row(
            result(verification_status="failed"), candidate=candidate(), apply=True
        )
        is False
    )


def test_resume_reprocesses_a_verified_dry_run_when_apply_is_requested() -> None:
    row = result(registration_status="not_requested")

    assert scout.can_resume_row(row, candidate=candidate(), apply=False) is True
    assert scout.can_resume_row(row, candidate=candidate(), apply=True) is False


def test_resume_rejects_rows_from_a_different_candidate_url() -> None:
    row = result(canonical_source_url="https://job-boards.greenhouse.io/other")

    assert scout.can_resume_row(row, candidate=candidate(), apply=True) is False


def test_checkpoint_manifest_fails_closed_when_the_input_changes(tmp_path: Path) -> None:
    input_path = tmp_path / "candidates.csv"
    output_path = tmp_path / "results.csv"
    with input_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=["board_token", "canonical_source_url"])
        writer.writeheader()
        writer.writerow(candidate())
    args = argparse.Namespace(
        input=input_path,
        output=output_path,
        offset=0,
        limit=None,
        apply=True,
        no_resume=False,
    )
    scout.ensure_checkpoint_manifest(args, [candidate()])
    input_path.write_text(
        "board_token,canonical_source_url\nother,https://job-boards.greenhouse.io/other\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="checkpoint manifest does not match"):
        scout.ensure_checkpoint_manifest(args, [candidate("other")])


def test_union_crawl_provenance_is_preserved_in_scout_rows_and_registration_evidence() -> None:
    union_candidate = {
        **candidate(),
        "example_observed_url": "https://boards.greenhouse.io/acme/jobs/1",
        "observation_count": "9",
        "first_observed_at": "2026-05-08T00:00:00Z",
        "last_observed_at": "2026-07-12T00:00:00Z",
        "first_seen_crawl": "CC-MAIN-2026-21",
        "last_seen_crawl": "CC-MAIN-2026-30",
        "crawl_count": "2",
        "crawl_ids": json.dumps(["CC-MAIN-2026-21", "CC-MAIN-2026-30"]),
    }
    evidence = SimpleNamespace(
        verification_status="verified",
        http_status=200,
        company_name="Acme",
        job_count=3,
        external_job_origins=(),
        board_page_origin=None,
        cache_source="network",
        attempt_count=1,
        error=None,
    )
    resolution = SimpleNamespace(
        status="unresolved_no_domain",
        company_id=None,
        website_candidate=None,
        reason=None,
    )

    row = scout.result_row(union_candidate, evidence, resolution)
    provenance = scout.candidate_crawl_provenance(union_candidate, fallback_crawl=None)

    assert {field: row[field] for field in scout.OUTPUT_FIELDS[4:10]} == {
        "first_observed_at": "2026-05-08T00:00:00Z",
        "last_observed_at": "2026-07-12T00:00:00Z",
        "first_seen_crawl": "CC-MAIN-2026-21",
        "last_seen_crawl": "CC-MAIN-2026-30",
        "crawl_count": "2",
        "crawl_ids": '["CC-MAIN-2026-21", "CC-MAIN-2026-30"]',
    }
    assert provenance == {
        "first_observed_at": "2026-05-08T00:00:00Z",
        "last_observed_at": "2026-07-12T00:00:00Z",
        "first_seen_crawl": "CC-MAIN-2026-21",
        "last_seen_crawl": "CC-MAIN-2026-30",
        "crawl_count": 2,
        "crawl_ids": ["CC-MAIN-2026-21", "CC-MAIN-2026-30"],
    }


def test_union_crawl_provenance_fails_closed_on_inconsistent_summary() -> None:
    inconsistent = {
        "crawl_ids": '["CC-MAIN-2026-21","CC-MAIN-2026-30"]',
        "crawl_count": "1",
    }

    with pytest.raises(ValueError, match="crawl_count does not match"):
        scout.candidate_crawl_provenance(inconsistent, fallback_crawl=None)


def test_apply_registration_writes_union_crawl_provenance(monkeypatch) -> None:
    captured: dict = {}

    class FakeJobSourceRegistry:
        def __init__(self, _engine) -> None:
            pass

        def register_url(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(company_source_id=8, created=True)

    monkeypatch.setattr(scout, "JobSourceRegistry", FakeJobSourceRegistry)
    union_candidate = {
        **candidate(),
        "example_observed_url": "https://boards.greenhouse.io/acme/jobs/1",
        "observation_count": "9",
        "first_observed_at": "2026-05-08T00:00:00Z",
        "last_observed_at": "2026-07-12T00:00:00Z",
        "first_seen_crawl": "CC-MAIN-2026-21",
        "last_seen_crawl": "CC-MAIN-2026-30",
        "crawl_count": "2",
        "crawl_ids": '["CC-MAIN-2026-21","CC-MAIN-2026-30"]',
    }
    row: dict = {}
    evidence = SimpleNamespace(
        board_token="acme",
        company_name="Acme",
        job_count=3,
    )
    resolution = SimpleNamespace(
        status="existing_exact_name",
        company_id=1,
        website_candidate=None,
    )

    scout.apply_registration(
        row,
        evidence=evidence,
        resolution=resolution,
        candidate=union_candidate,
        companies=[{"id": 1}],
        existing_sources={},
        crawl=None,
        engine=object(),
        homepage_verifier=lambda _url: None,
    )

    assert row["registration_status"] == "company_reused_source_created"
    assert row["company_source_id"] == 8
    assert captured["evidence"] == {
        "discovery_provider": "commoncrawl_url_index",
        "observation_count": 9,
        "first_observed_at": "2026-05-08T00:00:00Z",
        "last_observed_at": "2026-07-12T00:00:00Z",
        "first_seen_crawl": "CC-MAIN-2026-21",
        "last_seen_crawl": "CC-MAIN-2026-30",
        "crawl_count": 2,
        "crawl_ids": ["CC-MAIN-2026-21", "CC-MAIN-2026-30"],
        "verified_company_name": "Acme",
        "verified_job_count": 3,
        "website_evidence": None,
    }


def test_verified_unresolved_board_registers_a_provisional_company_and_source(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    unresolved_candidate = {
        **candidate("provider-confirmed"),
        "example_observed_url": ("https://job-boards.greenhouse.io/provider-confirmed/jobs/1"),
        "observation_count": "3",
    }
    evidence = SimpleNamespace(
        board_token="provider-confirmed",
        company_name="Provider Confirmed",
        job_count=4,
    )
    resolution = SimpleNamespace(
        status="unresolved_no_domain",
        company_id=None,
        website_candidate=None,
    )
    row: dict = {}

    scout.apply_registration(
        row,
        evidence=evidence,
        resolution=resolution,
        candidate=unresolved_candidate,
        companies=[],
        existing_sources={},
        crawl=None,
        engine=engine,
        homepage_verifier=lambda _url: None,
    )

    with engine.connect() as connection:
        company = connection.execute(select(companies_table)).mappings().one()
        source = connection.execute(select(company_sources_table)).mappings().one()
    assert row["registration_status"] == "company_provisional_source_created"
    assert row["company_id"] == company["id"]
    assert row["company_source_id"] == source["id"]
    assert company["identity_state"] == "provisional"
    assert company["website"] is None
    assert source["provider"] == "greenhouse"
    assert source["external_id"] == "provider-confirmed"
    assert source["sync_mode"] == "complete_snapshot"
