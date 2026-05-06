import importlib.util
import json
from pathlib import Path

from yc_radar.domain.models import Company
from yc_radar.services.candidate_fit import DEFAULT_CANDIDATE_PROFILE, score_company, target_record
from yc_radar.services.hiring_verifier import verification_cache_key

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_weekly_targets.py"
SPEC = importlib.util.spec_from_file_location("generate_weekly_targets", SCRIPT_PATH)
assert SPEC and SPEC.loader
generate_weekly_targets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generate_weekly_targets)

verify_targets = generate_weekly_targets.verify_targets
write_csv = generate_weekly_targets.write_csv
write_json = generate_weekly_targets.write_json


def test_cache_reuse_prevents_duplicate_verification_calls(monkeypatch) -> None:
    company = Company(
        name="Example",
        slug="example",
        yc_url="https://www.ycombinator.com/companies/example",
        website="https://example.com",
        one_liner="AI infra",
        status="Active",
        team_size=5,
        isHiring=True,
    )
    target = target_record(score_company(company, DEFAULT_CANDIDATE_PROFILE), rank=1)
    cache = {
        verification_cache_key(company): {
            "verified_hiring_status": "hiring",
            "career_page_url": "https://example.com/careers/",
            "verified_roles": ["Senior Backend Engineer"],
            "role_fit": "strong",
            "verification_source_url": "https://example.com/careers/",
            "verification_checked_at": "2026-05-06T00:00:00+00:00",
            "verification_confidence": 0.9,
            "firecrawl_pages_used": 2,
        }
    }

    def fail_if_called(**kwargs):
        raise AssertionError("cache miss unexpectedly called verification")

    monkeypatch.setattr(generate_weekly_targets, "verify_one_company", fail_if_called)

    cached_count, pages_used = verify_targets(
        targets=[target],
        companies_by_slug={company.slug: company},
        profile=DEFAULT_CANDIDATE_PROFILE,
        cache=cache,
        api_key="fake",
        max_pages_per_company=3,
        concurrency=2,
    )

    assert cached_count == 1
    assert pages_used == 0
    assert target["verified_hiring_status"] == "hiring"
    assert target["verified_roles"] == ["Senior Backend Engineer"]


def test_output_json_and_csv_include_yc_and_verified_hiring_fields(tmp_path) -> None:
    target = {
        "rank": 1,
        "name": "Example",
        "slug": "example",
        "yc_url": "https://www.ycombinator.com/companies/example",
        "website": "https://example.com",
        "one_liner": "AI infra",
        "yc_is_hiring": True,
        "verified_hiring_status": "hiring",
        "career_page_url": "https://example.com/careers/",
        "verified_roles": ["Senior Backend Engineer"],
        "target_role_lane": "Senior Backend / Senior Software",
        "matching_job_titles": ["Senior Backend Engineer"],
        "role_match_status": "strong",
        "role_match_reasons": ["Backend/platform role matches primary target lane"],
        "application_angle": "Apply directly as a senior backend/SWE candidate.",
        "proof_points_to_emphasize": ["Senior backend/API ownership"],
        "role_fit": "strong",
        "verification_source_url": "https://example.com/careers/",
        "verification_checked_at": "2026-05-06T00:00:00+00:00",
        "verification_confidence": 0.9,
        "firecrawl_pages_used": 2,
    }
    json_path = tmp_path / "weekly_targets.json"
    csv_path = tmp_path / "weekly_targets.csv"

    write_json(json_path, {"targets": [target]})
    write_csv(csv_path, [target])

    json_payload = json.loads(json_path.read_text())
    csv_text = csv_path.read_text()
    assert json_payload["targets"][0]["yc_is_hiring"] is True
    assert json_payload["targets"][0]["verified_hiring_status"] == "hiring"
    assert "yc_is_hiring" in csv_text
    assert "verified_hiring_status" in csv_text
    assert "Senior Backend Engineer" in csv_text
    assert "target_role_lane" in csv_text
    assert "role_match_status" in csv_text
