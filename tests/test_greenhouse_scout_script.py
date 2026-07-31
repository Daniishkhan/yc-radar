import importlib.util
import argparse
import csv
from pathlib import Path

import pytest


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


def test_resume_reuses_completed_rows_and_retries_transient_failures() -> None:
    assert scout.can_resume_row(result(), candidate=candidate(), apply=True) is True
    assert (
        scout.can_resume_row(
            result(verification_status="failed"), candidate=candidate(), apply=True
        )
        is False
    )
    assert (
        scout.can_resume_row(
            result(registration_status="homepage_unverified"),
            candidate=candidate(),
            apply=True,
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
