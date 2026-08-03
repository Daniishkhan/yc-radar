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
    assert row["provider"] == "greenhouse"
    assert row["job_key"] == "source:greenhouse:example:42"
    assert row["origin_kind"] == "ats"
    assert row["company_source_id"] == 3
    assert row["source_external_id"] == "example"
    assert row["source_enabled"] is True
    assert "description_text" not in row
    assert "candidate" not in " ".join(row)
