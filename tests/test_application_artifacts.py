from __future__ import annotations

import json
from pathlib import Path

from yc_radar.services.application_artifacts import (
    discover_queue_artifacts,
    load_queues,
    parse_queue_spec,
    read_queue_artifact,
)


def test_json_queue_object_and_legacy_names_share_canonical_queues(tmp_path: Path) -> None:
    combined = tmp_path / "queues.json"
    combined.write_text(
        json.dumps(
            {
                "application_queue": [{"job_key": "apply-1"}],
                "jobs_to_verify": [{"job_key": "verify-1"}],
                "company_outreach_queue": [{"company_slug": "acme"}],
            }
        ),
        encoding="utf-8",
    )

    queues = read_queue_artifact(combined)

    assert list(queues) == [
        "application_queue",
        "verification_queue",
        "company_outreach_queue",
    ]
    assert queues["verification_queue"][0]["job_key"] == "verify-1"


def test_requested_queue_can_read_legacy_csv_and_json_jobs_wrapper(tmp_path: Path) -> None:
    csv_path = tmp_path / "jobs_to_apply.csv"
    csv_path.write_text("job_key,provider\none,greenhouse\n", encoding="utf-8")
    json_path = tmp_path / "job_opportunities.json"
    json_path.write_text(json.dumps({"jobs": [{"job_key": "two"}]}), encoding="utf-8")

    queues = load_queues(
        [
            (None, csv_path),
            ("verification", json_path),
        ]
    )

    assert queues["application_queue"] == [{"job_key": "one", "provider": "greenhouse"}]
    assert queues["verification_queue"] == [{"job_key": "two"}]


def test_weekly_targets_input_is_recognized_as_company_outreach(tmp_path: Path) -> None:
    artifact = tmp_path / "weekly_targets.json"
    artifact.write_text(
        json.dumps({"targets": [{"company_slug": "acme"}]}),
        encoding="utf-8",
    )

    assert read_queue_artifact(artifact) == {
        "company_outreach_queue": [{"company_slug": "acme"}]
    }


def test_run_directory_prefers_new_json_names_without_double_counting_csv(tmp_path: Path) -> None:
    for filename in (
        "application_queue.json",
        "application_queue.csv",
        "jobs_to_apply.csv",
        "jobs_to_verify.csv",
        "weekly_targets.json",
    ):
        (tmp_path / filename).write_text("[]" if filename.endswith(".json") else "job_key\n", encoding="utf-8")

    assert discover_queue_artifacts(tmp_path) == [
        ("application_queue", tmp_path / "application_queue.json"),
        ("verification_queue", tmp_path / "jobs_to_verify.csv"),
        ("company_outreach_queue", tmp_path / "weekly_targets.json"),
    ]


def test_queue_spec_splits_only_the_first_equals_character() -> None:
    assert parse_queue_spec("verify=/tmp/run=a/jobs.csv") == (
        "verification_queue",
        Path("/tmp/run=a/jobs.csv"),
    )
