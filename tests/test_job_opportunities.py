import importlib.util
import json
from argparse import Namespace
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_job_opportunities.py"
SPEC = importlib.util.spec_from_file_location("generate_job_opportunities", SCRIPT_PATH)
assert SPEC and SPEC.loader
generate_job_opportunities = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generate_job_opportunities)


def test_main_holds_shared_artifact_lock_while_generating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "data" / "local" / "runs" / "current"
    local_dir = tmp_path / "data" / "local"
    events: list[object] = []

    @contextmanager
    def fake_lock(*, output_dir: Path, local_dir: Path):
        events.append(("acquire", output_dir, local_dir))
        try:
            yield local_dir / ".queue-artifact-generation.lock"
        finally:
            events.append("release")

    monkeypatch.setattr(
        generate_job_opportunities,
        "parse_args",
        lambda: Namespace(
            changed_since=None,
            as_of="2026-08-05T00:00:00+00:00",
            output_dir=output_dir,
            default_output_root=local_dir / "runs",
            date="2026-08-05",
        ),
    )
    monkeypatch.setattr(
        generate_job_opportunities,
        "get_settings",
        lambda: SimpleNamespace(local_dir=local_dir),
    )
    monkeypatch.setattr(generate_job_opportunities, "artifact_generation_lock", fake_lock)
    monkeypatch.setattr(
        generate_job_opportunities,
        "generate_artifacts",
        lambda **kwargs: events.append(("generate", kwargs["output_dir"])),
    )

    generate_job_opportunities.main()

    assert events == [
        ("acquire", output_dir, local_dir),
        ("generate", output_dir),
        "release",
    ]


def test_opportunity_row_is_public_provenance_and_backend_classified() -> None:
    row = generate_job_opportunities.opportunity_row(
        {
            "company_name": "Example",
            "company_slug": "example",
            "title": "Senior Backend Engineer",
            "job_key": "source:greenhouse:example:42",
            "source_kind": "ats_board",
            "origin_kind": "ats",
            "source_record_id": "7",
            "provider": "greenhouse",
            "external_job_id": "42",
            "company_source_id": 3,
            "source_url": "https://boards.greenhouse.io/example",
            "source_external_id": "example",
            "source_enabled": True,
            "source_sync_status": "complete",
            "posting_url": "https://boards.greenhouse.io/example/jobs/42",
            "status": "active",
            "description_text": "Build API infrastructure",
            "last_changed_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )

    assert row["role_match_status"] == "strong"
    assert row["remote_eligibility_status"] == "no_remote_evidence"
    assert row["remote_eligibility_reasons"] == [
        "No role-specific remote or onsite evidence was detected"
    ]
    assert row["remote_eligibility_evidence"] == []
    assert row["provider"] == "greenhouse"
    assert row["job_key"] == "source:greenhouse:example:42"
    assert row["origin_kind"] == "ats"
    assert row["company_source_id"] == 3
    assert row["source_external_id"] == "example"
    assert row["source_enabled"] is True
    assert "description_text" not in row
    assert "candidate" not in " ".join(row)


def test_opportunity_row_exposes_ambiguous_remote_eligibility_for_json_and_csv() -> None:
    row = generate_job_opportunities.opportunity_row(
        {
            "company_name": "Remote Example",
            "company_slug": "remote-example",
            "title": "Software Engineer",
            "location": "Remote",
            "description_text": "Join our fully remote engineering team.",
        }
    )

    assert row["remote_eligibility_status"] == "remote_unclear"
    assert row["remote_eligibility_reasons"] == [
        "Role is remote, but eligible countries are not explicit"
    ]
    assert row["remote_eligibility_evidence"]
    assert "remote_eligibility_status" in generate_job_opportunities.CSV_FIELDS
    assert "remote_eligibility_reasons" in generate_job_opportunities.CSV_FIELDS
    assert "remote_eligibility_evidence" in generate_job_opportunities.CSV_FIELDS
    assert generate_job_opportunities.csv_value(row["remote_eligibility_reasons"]) == (
        "Role is remote, but eligible countries are not explicit"
    )
    assert generate_job_opportunities.csv_value(row["remote_eligibility_evidence"])


def test_observation_freshness_cli_defaults_overrides_and_disables() -> None:
    default_args = generate_job_opportunities.parse_args([])
    override_args = generate_job_opportunities.parse_args(
        ["--observation-max-age-days", "12"]
    )
    disabled_args = generate_job_opportunities.parse_args(
        ["--no-observation-age-filter"]
    )

    assert default_args.observation_max_age_days == 45
    assert override_args.observation_max_age_days == 12
    assert disabled_args.observation_max_age_days is None


def test_role_and_remote_filters_are_repeatable_and_apply_before_limit() -> None:
    args = generate_job_opportunities.parse_args(
        [
            "--role-status",
            "strong",
            "--role-status",
            "possible",
            "--remote-status",
            "global_explicit",
        ]
    )
    rows = [
        {"role_match_status": "exclude", "remote_eligibility_status": "global_explicit"},
        {"role_match_status": "strong", "remote_eligibility_status": "remote_unclear"},
        {"role_match_status": "strong", "remote_eligibility_status": "global_explicit"},
        {"role_match_status": "possible", "remote_eligibility_status": "global_explicit"},
    ]

    filtered = generate_job_opportunities.filter_opportunity_rows(
        rows,
        role_statuses=args.role_status,
        remote_statuses=args.remote_status,
        limit=1,
    )

    assert args.role_status == ["strong", "possible"]
    assert args.remote_status == ["global_explicit"]
    assert filtered == [rows[2]]


def test_role_first_filtered_export_matches_classify_then_filter_semantics() -> None:
    source_rows = [
        {
            "company_name": "Backend",
            "title": "Senior Backend Engineer",
            "location": "Worldwide Remote",
            "description_text": "Work remotely from anywhere in the world.",
            "status": "active",
            "posting_url": "https://example.com/backend",
        },
        {
            "company_name": "Frontend",
            "title": "Frontend Engineer",
            "location": "Remote",
            "description_text": "Join our remote frontend engineering team.",
            "status": "active",
            "posting_url": "https://example.com/frontend",
        },
        {
            "company_name": "Full Stack",
            "title": "Full Stack Engineer",
            "location": "Remote",
            "description_text": "Join our remote engineering team.",
            "status": "active",
            "posting_url": "https://example.com/full-stack",
        },
        {
            "company_name": "AI",
            "title": "Applied AI Engineer",
            "location": "Remote",
            "description_text": "Build AI systems with our remote team.",
            "status": "active",
            "posting_url": "https://example.com/ai",
        },
        {
            "company_name": "Sales",
            "title": "Account Executive",
            "location": "Worldwide Remote",
            "description_text": "Work remotely from anywhere.",
            "status": "active",
            "posting_url": "https://example.com/sales",
        },
        {
            "company_name": "Weak",
            "title": "Software Engineer",
            "location": "Remote",
            "description_text": "Join our remote team.",
            "status": "active",
            "posting_url": "https://example.com/software",
        },
    ]
    role_statuses = ["strong", "possible"]
    remote_statuses = ["global_explicit", "remote_unclear"]
    old_semantics = generate_job_opportunities.filter_opportunity_rows(
        [generate_job_opportunities.opportunity_row(row) for row in source_rows],
        role_statuses=role_statuses,
        remote_statuses=remote_statuses,
        limit=None,
    )

    optimized = generate_job_opportunities.classify_and_filter_opportunity_rows(
        source_rows,
        role_statuses=role_statuses,
        remote_statuses=remote_statuses,
        limit=None,
    )

    assert optimized == old_semantics


def test_role_filter_skips_remote_analysis_for_nonmatching_rows_and_classifies_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RejectDescriptionAccess(dict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            if key in {"description_text", "department", "location"}:
                raise AssertionError(f"context accessed for rejected title: {key}")
            return super().get(key, default)

    source_rows = [
        RejectDescriptionAccess(
            {
                "company_name": "Sales",
                "title": "Account Executive",
                "description_text": "Sell enterprise software remotely.",
            }
        ),
        RejectDescriptionAccess(
            {
                "company_name": "Design",
                "title": "Product Designer",
                "description_text": "Design products remotely.",
            }
        ),
        {
            "company_name": "Frontend",
            "title": "Frontend Engineer",
            "description_text": "Build React interfaces for a remote team.",
        },
        {
            "company_name": "Full Stack",
            "title": "Full Stack Engineer",
            "description_text": "Build end-to-end software for a remote team.",
        },
        {
            "company_name": "AI",
            "title": "Applied AI Engineer",
            "description_text": "Build AI systems for a remote team.",
        },
    ]
    original_role_classifier = generate_job_opportunities.classify_role_text
    classified_titles: list[str] = []
    remotely_analyzed_titles: list[str] = []

    def count_role_classification(title: str, context: str, **kwargs):
        classified_titles.append(title)
        return original_role_classifier(title, context, **kwargs)

    def count_remote_classification(row: dict[str, object]):
        remotely_analyzed_titles.append(str(row["title"]))
        return SimpleNamespace(
            status="remote_unclear",
            reasons=["test"],
            evidence=["test"],
        )

    monkeypatch.setattr(
        generate_job_opportunities,
        "classify_role_text",
        count_role_classification,
    )
    monkeypatch.setattr(
        generate_job_opportunities,
        "classify_remote_eligibility",
        count_remote_classification,
    )

    rows = generate_job_opportunities.classify_and_filter_opportunity_rows(
        source_rows,
        role_statuses=["possible"],
    )

    assert classified_titles == [
        "Frontend Engineer",
        "Full Stack Engineer",
        "Applied AI Engineer",
    ]
    assert remotely_analyzed_titles == classified_titles
    assert [row["company_name"] for row in rows] == ["Frontend", "Full Stack", "AI"]


def test_role_filter_including_exclude_preserves_non_engineering_rows() -> None:
    source_rows = [
        {
            "company_name": "Design",
            "title": "Product Designer",
            "location": "Remote",
            "description_text": "Design products for a remote team.",
        },
        {
            "company_name": "Frontend",
            "title": "Frontend Engineer",
            "location": "Remote",
            "description_text": "Build React interfaces for a remote team.",
        },
    ]
    old_semantics = generate_job_opportunities.filter_opportunity_rows(
        [generate_job_opportunities.opportunity_row(row) for row in source_rows],
        role_statuses=["exclude"],
    )

    optimized = generate_job_opportunities.classify_and_filter_opportunity_rows(
        source_rows,
        role_statuses=["exclude"],
    )

    assert optimized == old_semantics
    assert [row["company_name"] for row in optimized] == ["Design"]


def test_job_row_artifacts_keep_existing_json_shape_and_publish_csv(tmp_path: Path) -> None:
    rows = [
        {
            "company_name": "Example",
            "role_match_reasons": ["Backend match"],
            "last_changed_at": datetime(2026, 8, 5, tzinfo=UTC),
        }
    ]

    json_path, csv_path = generate_job_opportunities.write_job_rows(
        output_dir=tmp_path,
        stem="job_opportunities",
        rows=rows,
        fields=["company_name", "role_match_reasons", "last_changed_at"],
    )

    assert json.loads(json_path.read_text(encoding="utf-8")) == {
        "jobs": [
            {
                "company_name": "Example",
                "role_match_reasons": ["Backend match"],
                "last_changed_at": "2026-08-05 00:00:00+00:00",
            }
        ]
    }
    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "company_name,role_match_reasons,last_changed_at",
        "Example,Backend match,2026-08-05 00:00:00+00:00",
    ]
