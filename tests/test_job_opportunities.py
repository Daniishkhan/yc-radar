import importlib.util
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_job_opportunities.py"
SPEC = importlib.util.spec_from_file_location("generate_job_opportunities", SCRIPT_PATH)
assert SPEC and SPEC.loader
generate_job_opportunities = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generate_job_opportunities)


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
