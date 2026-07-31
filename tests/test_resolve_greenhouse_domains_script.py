from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from yc_radar.services.google_domain_resolver import (
    DomainEvidence,
    DomainResolutionResult,
    PageEvidence,
)

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "resolve_greenhouse_domains.py"
SPEC = importlib.util.spec_from_file_location("resolve_greenhouse_domains", SCRIPT_PATH)
assert SPEC and SPEC.loader
resolver_script = importlib.util.module_from_spec(SPEC)
sys.modules["resolve_greenhouse_domains"] = resolver_script
SPEC.loader.exec_module(resolver_script)


def scout_row(token: str = "acme", **overrides: str) -> dict[str, str]:
    row = {
        "board_token": token,
        "canonical_source_url": f"https://job-boards.greenhouse.io/{token}",
        "example_observed_url": f"https://boards.greenhouse.io/{token}",
        "observation_count": "3",
        "verification_status": "verified",
        "board_name": "Acme",
        "job_count": "7",
        "resolution_status": "unresolved_no_domain",
    }
    row.update(overrides)
    return row


def write_scout_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=list(scout_row()))
        writer.writeheader()
        writer.writerows(rows)


def args_for(tmp_path: Path, input_path: Path, **overrides) -> argparse.Namespace:
    values = {
        "input": input_path,
        "output": tmp_path / "result.csv",
        "status_file": tmp_path / "status.json",
        "cache_file": tmp_path / "cache.json",
        "project": "test-project",
        "location": "global",
        "model": "gemini-3.5-flash-lite",
        "limit": None,
        "offset": 0,
        "checkpoint_every": 10,
        "delay_seconds": 0,
        "retry_delay_seconds": 0,
        "max_attempts": 1,
        "max_pages_per_domain": 3,
        "no_resume": False,
        "apply": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_load_candidates_scopes_only_verified_domainless_rows(tmp_path: Path) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(
        path,
        [
            scout_row("keep"),
            scout_row("failed", verification_status="failed"),
            scout_row("resolved", resolution_status="new_company_domain_candidate"),
        ],
    )

    rows = resolver_script.load_candidates(path)

    assert [row["board_token"] for row in rows] == ["keep"]
    assert rows[0]["job_count"] == "7"


def test_manifest_fails_closed_when_limit_changes(tmp_path: Path) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(path, [scout_row("one"), scout_row("two")])
    candidates = resolver_script.load_candidates(path)
    calibration = args_for(tmp_path, path, limit=1)
    resolver_script.ensure_checkpoint_manifest(calibration, candidates[:1])

    full = args_for(tmp_path, path, limit=None)
    with pytest.raises(SystemExit, match="checkpoint manifest does not match"):
        resolver_script.ensure_checkpoint_manifest(full, candidates)


def test_resume_retries_request_failures_and_apply_pending_acceptance() -> None:
    candidate = scout_row()
    row = {
        **candidate,
        "domain_resolution_status": "accepted",
        "registration_status": "not_requested",
    }

    assert resolver_script.can_resume_row(row, candidate=candidate, apply=False) is True
    assert resolver_script.can_resume_row(row, candidate=candidate, apply=True) is False
    assert (
        resolver_script.can_resume_row(
            {
                **row,
                "domain_resolution_status": "manual_review",
                "retryable": "true",
            },
            candidate=candidate,
            apply=False,
        )
        is False
    )
    assert (
        resolver_script.can_resume_row(
            {**row, "domain_resolution_status": "request_failed"},
            candidate=candidate,
            apply=False,
        )
        is False
    )


def test_quota_checkpoint_is_durable_and_returns_success_to_avoid_restart_loop(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(path, [scout_row()])
    args = args_for(tmp_path, path)

    class FakeResolver:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def resolve(self, **kwargs) -> DomainResolutionResult:
            return DomainResolutionResult(
                status="quota_exhausted",
                model="gemini-3.5-flash-lite",
                location="global",
                cache_source="network",
                request_attempt_count=3,
                error="429 quota",
                retryable=True,
                quota_exhausted=True,
            )

    monkeypatch.setattr(resolver_script, "GoogleDomainResolver", FakeResolver)

    exit_code = resolver_script.run(args)

    assert exit_code == 0
    assert not args.output.exists()
    assert args.output.with_suffix(".partial.csv").exists()
    status = json.loads(args.status_file.read_text(encoding="utf-8"))
    assert status["state"] == "quota_exhausted"
    assert status["processed"] == 1
    assert status["failed"] == 1
    assert status["request_attempt_count"] == 3


def test_result_row_and_registration_proof_retain_auditable_fields() -> None:
    brand_page = PageEvidence(
        requested_url="https://acme.test",
        final_url="https://www.acme.test/?tracking=1",
        http_status=200,
        domain="acme.test",
        brand_matches=("title:Acme Careers",),
    )
    careers_page = PageEvidence(
        requested_url="https://acme.test/careers",
        final_url="https://acme.test/careers?source=search",
        http_status=200,
        domain="acme.test",
        greenhouse_links=("https://boards.greenhouse.io/embed/job_board?for=acme#jobs",),
    )
    result = DomainResolutionResult(
        status="accepted",
        model="gemini-3.5-flash-lite",
        location="global",
        accepted_domain="acme.test",
        website_candidate="https://acme.test",
        candidate_evidence=(
            DomainEvidence(
                domain="acme.test",
                candidate_sources=("generated_text",),
                pages=(brand_page, careers_page),
                brand_valid=True,
                reciprocal_link_valid=True,
                passed=True,
            ),
        ),
    )

    row = resolver_script.result_row(scout_row(), result)
    proof = resolver_script.accepted_proof(result)

    assert row["job_count"] == "7"
    assert row["domain_resolution_status"] == "accepted"
    assert proof == [
        {
            "page_url": "https://www.acme.test/",
            "brand_match_kinds": ["title"],
            "greenhouse_links": [],
        },
        {
            "page_url": "https://acme.test/careers",
            "brand_match_kinds": [],
            "greenhouse_links": [
                "https://boards.greenhouse.io/embed/job_board?for=acme"
            ],
        },
    ]
