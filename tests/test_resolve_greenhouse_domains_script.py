from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

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
    fieldnames = list(scout_row())
    for row in rows:
        fieldnames.extend(field for field in row if field not in fieldnames)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
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
        "company_timeout_seconds": 120,
        "no_resume": False,
        "apply": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def accepted_result(*, greenhouse_token: str = "acme") -> DomainResolutionResult:
    brand_page = PageEvidence(
        requested_url="https://acme.test",
        final_url="https://acme.test",
        http_status=200,
        domain="acme.test",
        brand_matches=("title:Acme Careers",),
    )
    careers_page = PageEvidence(
        requested_url="https://acme.test/careers",
        final_url="https://acme.test/careers",
        http_status=200,
        domain="acme.test",
        greenhouse_links=(f"https://job-boards.greenhouse.io/{greenhouse_token}",),
    )
    return DomainResolutionResult(
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
                company_domain_compatible=True,
                company_domain_matches=("domain_label:name_prefix:acme",),
                passed=True,
            ),
        ),
    )


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


def test_load_candidates_canonicalizes_matching_greenhouse_url(tmp_path: Path) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(
        path,
        [
            scout_row(
                "AcMe",
                canonical_source_url=("http://boards.greenhouse.io/ACME/jobs/123?gh_src=tracking"),
            )
        ],
    )

    rows = resolver_script.load_candidates(path)

    assert rows[0]["board_token"] == "acme"
    assert rows[0]["canonical_source_url"] == "https://job-boards.greenhouse.io/acme"


def test_union_provenance_survives_loading_and_resolver_csv(tmp_path: Path) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(
        path,
        [
            scout_row(
                first_observed_at="2026-05-08T00:00:00Z",
                last_observed_at="2026-07-12T00:00:00+00:00",
                first_seen_crawl="CC-MAIN-2026-21",
                last_seen_crawl="CC-MAIN-2026-30",
                crawl_count="2",
                crawl_ids='["CC-MAIN-2026-30", "CC-MAIN-2026-21"]',
            )
        ],
    )

    candidate = resolver_script.load_candidates(path)[0]
    expected = {
        "first_observed_at": "2026-05-08T00:00:00Z",
        "last_observed_at": "2026-07-12T00:00:00Z",
        "first_seen_crawl": "CC-MAIN-2026-21",
        "last_seen_crawl": "CC-MAIN-2026-30",
        "crawl_count": "2",
        "crawl_ids": '["CC-MAIN-2026-21","CC-MAIN-2026-30"]',
    }
    assert {field: candidate[field] for field in expected} == expected

    result = DomainResolutionResult(
        status="unresolved",
        model="gemini-3.5-flash-lite",
        location="global",
    )
    row = resolver_script.result_row(candidate, result)
    output = tmp_path / "result.csv"
    resolver_script.write_csv_atomic(output, [row])
    with output.open(newline="", encoding="utf-8") as source:
        written = next(csv.DictReader(source))
    assert {field: written[field] for field in expected} == expected


def test_legacy_single_crawl_input_derives_crawl_summary_from_filename(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scout_CC-MAIN-2026-30.csv"
    write_scout_csv(path, [scout_row()])

    candidate = resolver_script.load_candidates(path)[0]

    assert candidate["first_observed_at"] == ""
    assert candidate["last_observed_at"] == ""
    assert candidate["first_seen_crawl"] == "CC-MAIN-2026-30"
    assert candidate["last_seen_crawl"] == "CC-MAIN-2026-30"
    assert candidate["crawl_count"] == "1"
    assert candidate["crawl_ids"] == '["CC-MAIN-2026-30"]'


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"crawl_ids": "not-json"}, "crawl_ids must be a JSON array"),
        ({"crawl_ids": '["CC-MAIN-invalid"]'}, "invalid crawl ID"),
        (
            {
                "crawl_ids": '["CC-MAIN-2026-21","CC-MAIN-2026-30"]',
                "crawl_count": "1",
            },
            "crawl_count does not match crawl_ids",
        ),
        (
            {
                "crawl_ids": '["CC-MAIN-2026-21","CC-MAIN-2026-30"]',
                "first_seen_crawl": "CC-MAIN-2026-30",
            },
            "first_seen_crawl does not match crawl_ids",
        ),
        (
            {
                "first_observed_at": "2026-07-12T00:00:00Z",
                "last_observed_at": "2026-05-08T00:00:00Z",
            },
            "first_observed_at is after last_observed_at",
        ),
    ],
)
def test_load_candidates_rejects_invalid_union_provenance(
    tmp_path: Path,
    overrides: dict[str, str],
    error: str,
) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(path, [scout_row(**overrides)])

    with pytest.raises(ValueError, match=error):
        resolver_script.load_candidates(path)


@pytest.mark.parametrize(
    ("token", "source_url"),
    [
        ("acme", "https://job-boards.greenhouse.io/other"),
        ("acme", "https://example.com/acme"),
        ("bad/token", "https://job-boards.greenhouse.io/bad%2Ftoken"),
    ],
)
def test_load_candidates_rejects_invalid_or_mismatched_greenhouse_identity(
    tmp_path: Path, token: str, source_url: str
) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(
        path,
        [scout_row(token, canonical_source_url=source_url)],
    )

    with pytest.raises(ValueError, match="eligible Greenhouse|does not match board token"):
        resolver_script.load_candidates(path)


def test_load_resume_rows_rejects_duplicate_normalized_tokens(tmp_path: Path) -> None:
    path = tmp_path / "result.partial.csv"
    resolver_script.write_csv_atomic(
        path,
        [
            {"board_token": "acme"},
            {"board_token": " ACME "},
        ],
    )

    with pytest.raises(ValueError, match="duplicate resume row for board token: acme"):
        resolver_script.load_resume_rows(path)


def test_manifest_fails_closed_when_limit_changes(tmp_path: Path) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(path, [scout_row("one"), scout_row("two")])
    candidates = resolver_script.load_candidates(path)
    calibration = args_for(tmp_path, path, limit=1)
    resolver_script.ensure_checkpoint_manifest(calibration, candidates[:1])
    manifest = json.loads(calibration.output.with_suffix(".checkpoint.json").read_text())
    assert manifest["prompt_version"] == resolver_script.PROMPT_VERSION
    assert manifest["evidence_version"] == resolver_script.EVIDENCE_VERSION

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
    registered = {
        **row,
        "registration_status": "source_existing",
        "company_id": "42",
    }
    assert (
        resolver_script.can_resume_row(
            registered,
            candidate=candidate,
            apply=True,
            existing_source_company_id=42,
        )
        is True
    )
    assert (
        resolver_script.can_resume_row(
            registered,
            candidate=candidate,
            apply=True,
            existing_source_company_id=41,
        )
        is False
    )
    assert (
        resolver_script.can_resume_row(
            {**registered, "board_token": " ACME "},
            candidate=candidate,
            apply=True,
            existing_source_company_id=42,
        )
        is False
    )
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


def test_resume_identity_includes_union_crawl_provenance() -> None:
    provenance = {
        "first_observed_at": "2026-05-08T00:00:00Z",
        "last_observed_at": "2026-07-12T00:00:00Z",
        "first_seen_crawl": "CC-MAIN-2026-21",
        "last_seen_crawl": "CC-MAIN-2026-30",
        "crawl_count": "2",
        "crawl_ids": '["CC-MAIN-2026-21","CC-MAIN-2026-30"]',
    }
    candidate = {**scout_row(), **provenance}
    row = {
        **candidate,
        "domain_resolution_status": "unresolved",
        "registration_status": "not_requested",
    }

    assert resolver_script.can_resume_row(row, candidate=candidate, apply=False) is True
    assert (
        resolver_script.can_resume_row(
            {**row, "first_observed_at": "2026-05-09T00:00:00Z"},
            candidate=candidate,
            apply=False,
        )
        is False
    )
    assert (
        resolver_script.can_resume_row(
            {**row, "crawl_ids": '["CC-MAIN-2026-21"]'},
            candidate=candidate,
            apply=False,
        )
        is False
    )


def test_checkpoint_merge_preserves_unvisited_prior_rows() -> None:
    selected = [
        scout_row("one"),
        scout_row("two"),
        scout_row("three"),
        scout_row("four"),
    ]
    prior = {
        "one": {"board_token": "one", "domain_resolution_status": "unresolved"},
        "two": {"board_token": "two", "domain_resolution_status": "accepted"},
        "three": {"board_token": "three", "domain_resolution_status": "manual_review"},
        "outside": {"board_token": "outside", "domain_resolution_status": "accepted"},
    }
    visited = [
        {"board_token": "one", "domain_resolution_status": "request_failed"},
        {"board_token": "four", "domain_resolution_status": "accepted"},
    ]

    merged = resolver_script.merge_checkpoint_rows(selected, visited, prior)

    assert [row["board_token"] for row in merged] == ["one", "two", "three", "four"]
    assert merged[0]["domain_resolution_status"] == "request_failed"
    assert merged[1:] == [prior["two"], prior["three"], visited[1]]


def test_interrupted_resume_does_not_truncate_prior_checkpoint(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "scout.csv"
    candidates = [
        scout_row("one", board_name="One"),
        scout_row("two", board_name="Two"),
        scout_row("three", board_name="Three"),
        scout_row("four", board_name="Four"),
    ]
    write_scout_csv(path, candidates)
    args = args_for(tmp_path, path)
    resolver_script.ensure_checkpoint_manifest(args, candidates)
    prior_rows = [
        {**candidates[0], "domain_resolution_status": "unresolved", "retryable": "false"},
        {**candidates[1], "domain_resolution_status": "request_failed", "retryable": "true"},
        {**candidates[2], "domain_resolution_status": "request_failed", "retryable": "true"},
        {**candidates[3], "domain_resolution_status": "accepted", "retryable": "false"},
    ]
    partial = args.output.with_suffix(".partial.csv")
    resolver_script.write_csv_atomic(partial, prior_rows)

    class FakeResolver:
        def __init__(self, *args, **kwargs) -> None:
            self.cache = type("Cache", (), {"hits": 0, "stores": 0})()

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def close(self) -> None:
            pass

        def resolve(self, *, company_name: str, **kwargs) -> DomainResolutionResult:
            if company_name == "Three":
                raise RuntimeError("interrupted retry")
            assert company_name == "Two"
            return DomainResolutionResult(
                status="unresolved",
                model="gemini-3.5-flash-lite",
                location="global",
                cache_source="disk",
            )

    monkeypatch.setattr(resolver_script, "GoogleDomainResolver", FakeResolver)

    with pytest.raises(RuntimeError, match="interrupted retry"):
        resolver_script.run(args)

    with partial.open(newline="", encoding="utf-8") as source:
        checkpoint_rows = list(csv.DictReader(source))
    assert [row["board_token"] for row in checkpoint_rows] == [
        "one",
        "two",
        "three",
        "four",
    ]
    assert checkpoint_rows[1]["domain_resolution_status"] == "unresolved"
    assert checkpoint_rows[2]["domain_resolution_status"] == "request_failed"
    status = json.loads(args.status_file.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["processed"] == 4


def test_quota_checkpoint_is_durable_and_returns_success_to_avoid_restart_loop(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(path, [scout_row()])
    args = args_for(tmp_path, path)

    class FakeResolver:
        def __init__(self, *args, **kwargs) -> None:
            self.cache = type("Cache", (), {"hits": 0, "stores": 0})()

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
    assert status["retryable"] == 1
    assert status["request_attempt_count"] == 3
    assert status["prompt_version"] == resolver_script.PROMPT_VERSION
    assert status["evidence_version"] == resolver_script.EVIDENCE_VERSION


def test_company_timeout_is_retryable_and_run_continues(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(
        path,
        [
            scout_row("slow", board_name="Slow Company"),
            scout_row("next", board_name="Next Company"),
        ],
    )
    args = args_for(
        tmp_path,
        path,
        checkpoint_every=10,
        company_timeout_seconds=0.01,
    )
    checkpoint_lengths: list[int] = []
    original_checkpoint = resolver_script.checkpoint

    def recording_checkpoint(*checkpoint_args, **checkpoint_kwargs) -> None:
        checkpoint_lengths.append(len(checkpoint_args[2]))
        original_checkpoint(*checkpoint_args, **checkpoint_kwargs)

    monkeypatch.setattr(resolver_script, "checkpoint", recording_checkpoint)

    class FakeResolver:
        def __init__(self, *args, **kwargs) -> None:
            self.cache = type("Cache", (), {"hits": 0, "stores": 0})()

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def close(self) -> None:
            pass

        def resolve(self, *, company_name: str, **kwargs) -> DomainResolutionResult:
            if company_name == "Slow Company":
                self.cache.hits += 1
                time.sleep(1)
            return DomainResolutionResult(
                status="unresolved",
                model="gemini-3.5-flash-lite",
                location="global",
                cache_source="network",
            )

    monkeypatch.setattr(resolver_script, "GoogleDomainResolver", FakeResolver)

    assert resolver_script.run(args) == 0

    with args.output.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    assert [row["board_token"] for row in rows] == ["slow", "next"]
    assert rows[0]["domain_resolution_status"] == "request_failed"
    assert rows[0]["retryable"] == "true"
    assert rows[0]["cache_source"] == "disk"
    assert rows[0]["error"] == "company_timeout:0.01s"
    assert rows[1]["domain_resolution_status"] == "unresolved"
    assert checkpoint_lengths == [1]
    status = json.loads(args.status_file.read_text(encoding="utf-8"))
    assert status["state"] == "partial"
    assert status["processed"] == 2
    assert status["failed"] == 1
    assert status["retryable"] == 1
    assert status["company_timeout_seconds"] == 0.01

    retry_calls: list[str] = []

    class RetryResolver(FakeResolver):
        def resolve(self, *, company_name: str, **kwargs) -> DomainResolutionResult:
            retry_calls.append(company_name)
            return DomainResolutionResult(
                status="unresolved",
                model="gemini-3.5-flash-lite",
                location="global",
                cache_source="disk",
            )

    monkeypatch.setattr(resolver_script, "GoogleDomainResolver", RetryResolver)

    assert resolver_script.run(args) == 0
    assert retry_calls == ["Slow Company"]
    retry_status = json.loads(args.status_file.read_text(encoding="utf-8"))
    assert retry_status["processed"] == 2
    assert retry_status["failed"] == 0
    assert retry_status["retryable"] == 0
    assert retry_status["resumed"] == 1


def test_invalid_resume_rows_fail_before_status_is_marked_running(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scout.csv"
    candidate = scout_row()
    write_scout_csv(path, [candidate])
    args = args_for(tmp_path, path)
    resolver_script.ensure_checkpoint_manifest(args, [candidate])
    resolver_script.write_csv_atomic(
        args.output.with_suffix(".partial.csv"),
        [
            {"board_token": "acme"},
            {"board_token": " ACME "},
        ],
    )

    with pytest.raises(ValueError, match="duplicate resume row"):
        resolver_script.run(args)

    assert not args.status_file.exists()


def test_validate_args_rejects_non_positive_company_timeout(tmp_path: Path) -> None:
    path = tmp_path / "scout.csv"
    write_scout_csv(path, [scout_row()])
    args = args_for(tmp_path, path, company_timeout_seconds=0)

    with pytest.raises(SystemExit, match="--company-timeout-seconds must be positive"):
        resolver_script.validate_args(args)


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
                company_domain_compatible=True,
                company_domain_matches=("domain_label:name_prefix:acme",),
                passed=True,
            ),
        ),
    )

    row = resolver_script.result_row(scout_row(), result)
    proof = resolver_script.accepted_proof(result)

    assert row["job_count"] == "7"
    assert row["domain_resolution_status"] == "accepted"
    assert resolver_script.accepted_company_domain_matches(result) == [
        "domain_label:name_prefix:acme"
    ]
    assert proof == [
        {
            "page_url": "https://www.acme.test/",
            "brand_match_kinds": ["title"],
            "greenhouse_links": [],
        },
        {
            "page_url": "https://acme.test/careers",
            "brand_match_kinds": [],
            "greenhouse_links": ["https://boards.greenhouse.io/embed/job_board?for=acme"],
        },
    ]


def test_apply_registration_rechecks_identity_and_accepted_proof() -> None:
    candidate = scout_row()
    result = accepted_result()
    row = resolver_script.result_row(candidate, result)

    resolver_script.apply_registration(
        row,
        result=result,
        candidate=candidate,
        companies=[],
        existing_sources={"acme": 42},
        engine=object(),
    )

    assert row["company_id"] == 42
    assert row["registration_status"] == "source_existing"

    tampered_row = resolver_script.result_row(candidate, result)
    tampered_row["canonical_source_url"] = "https://job-boards.greenhouse.io/other"
    resolver_script.apply_registration(
        tampered_row,
        result=result,
        candidate=candidate,
        companies=[],
        existing_sources={},
        engine=object(),
    )
    assert tampered_row["registration_status"] == "registration_failed"
    assert "accepted row identity" in tampered_row["error"]

    wrong_proof = accepted_result(greenhouse_token="other")
    wrong_proof_row = resolver_script.result_row(candidate, wrong_proof)
    resolver_script.apply_registration(
        wrong_proof_row,
        result=wrong_proof,
        candidate=candidate,
        companies=[],
        existing_sources={},
        engine=object(),
    )
    assert wrong_proof_row["registration_status"] == "registration_failed"
    assert "deterministic identity proof" in wrong_proof_row["error"]

    stripped_proof_row = resolver_script.result_row(candidate, result)
    stripped_proof_row["candidate_evidence"] = "[]"
    resolver_script.apply_registration(
        stripped_proof_row,
        result=result,
        candidate=candidate,
        companies=[],
        existing_sources={},
        engine=object(),
    )
    assert stripped_proof_row["registration_status"] == "registration_failed"
    assert "does not preserve the validated result proof" in stripped_proof_row["error"]


def test_apply_registration_writes_union_provenance_to_source_evidence(monkeypatch) -> None:
    captured: dict = {}

    class FakeJobSourceRegistry:
        def __init__(self, _engine) -> None:
            pass

        def register_url(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(career_source_id=8, created=True)

    monkeypatch.setattr(resolver_script, "JobSourceRegistry", FakeJobSourceRegistry)
    candidate = scout_row(
        first_observed_at="2026-05-08T00:00:00Z",
        last_observed_at="2026-07-12T00:00:00Z",
        first_seen_crawl="CC-MAIN-2026-21",
        last_seen_crawl="CC-MAIN-2026-30",
        crawl_count="2",
        crawl_ids='["CC-MAIN-2026-21","CC-MAIN-2026-30"]',
    )
    result = accepted_result()
    row = resolver_script.result_row(candidate, result)

    resolver_script.apply_registration(
        row,
        result=result,
        candidate=candidate,
        companies=[{"id": 42, "name": "Acme", "primary_domain": "acme.test"}],
        existing_sources={},
        engine=object(),
    )

    assert row["registration_status"] == "company_reused_source_created"
    expected_provenance = {
        "first_observed_at": "2026-05-08T00:00:00Z",
        "last_observed_at": "2026-07-12T00:00:00Z",
        "first_seen_crawl": "CC-MAIN-2026-21",
        "last_seen_crawl": "CC-MAIN-2026-30",
        "crawl_count": 2,
        "crawl_ids": ["CC-MAIN-2026-21", "CC-MAIN-2026-30"],
    }
    assert captured["evidence"]["observation_count"] == 3
    assert all(
        captured["evidence"].get(field) == value
        for field, value in expected_provenance.items()
    )


@pytest.mark.parametrize("job_count", ["-1", "not-a-number"])
def test_apply_registration_contains_invalid_job_count(job_count: str) -> None:
    candidate = scout_row(job_count=job_count)
    result = accepted_result()
    row = resolver_script.result_row(candidate, result)

    resolver_script.apply_registration(
        row,
        result=result,
        candidate=candidate,
        companies=[],
        existing_sources={},
        engine=object(),
    )

    assert row["registration_status"] == "registration_failed"
    assert "job_count must be a non-negative integer" in row["error"]


def test_summary_counts_registration_failures() -> None:
    summary = resolver_script.summary_counts(
        [
            {
                "domain_resolution_status": "accepted",
                "registration_status": "registration_failed",
            },
            {
                "domain_resolution_status": "accepted",
                "registration_status": "identity_conflict",
            },
        ],
        selected=2,
        resumed=0,
    )

    assert summary["failed"] == 1
    assert summary["succeeded"] == 1
    assert summary["registration_failed"] == 1
    assert summary["registration_conflicts"] == 1
