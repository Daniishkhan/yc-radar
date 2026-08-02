from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from yc_radar.adapters.greenhouse import normalize_greenhouse_job
from yc_radar.domain.job_sources import SourceSnapshot
from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.database import engine_from_url
from yc_radar.services.job_source_registry import JobSourceRegistry
from yc_radar.services.job_sync_service import JobSyncService


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "analyze_job_funnel.py"
SPEC = importlib.util.spec_from_file_location("analyze_job_funnel", SCRIPT_PATH)
assert SPEC and SPEC.loader
funnel = importlib.util.module_from_spec(SPEC)
sys.modules["analyze_job_funnel"] = funnel
SPEC.loader.exec_module(funnel)


def job(
    job_id: int,
    *,
    title: str = "Senior Backend Engineer",
    description: str,
    location: str = "Remote",
    company_id: int = 1,
    company_name: str = "Acme",
    company_slug: str = "acme",
    provider: str = "greenhouse",
) -> dict[str, Any]:
    return {
        "id": job_id,
        "company_id": company_id,
        "company_name": company_name,
        "company_slug": company_slug,
        "provider": provider,
        "external_job_id": f"external-{job_id}",
        "title": title,
        "posting_url": f"https://jobs.example/{job_id}",
        "apply_url": f"https://jobs.example/{job_id}/apply",
        "location": location,
        "department": "Engineering",
        "employment_type": "Full-time",
        "structured_evidence": {},
        "source_published_at": datetime(2026, 7, job_id, tzinfo=UTC),
        "source_updated_at": datetime(2026, 7, job_id + 1, tzinfo=UTC),
        "description_text": description,
    }


def test_role_clustering_preserves_variants_and_selects_explicit_pakistan() -> None:
    rows = [
        job(1, description="This role is remote worldwide."),
        job(
            2,
            title="Senior Backend Engineer!",
            description="This remote role is open to candidates based in Pakistan.",
            location="Pakistan - Remote",
            provider="ashby",
        ),
        job(
            3,
            title="Senior Platform Engineer",
            description="This role is remote, but eligible countries are not specified.",
        ),
        job(4, title="Sales Engineer", description="Sell developer infrastructure."),
    ]

    clusters, summary = funnel.build_role_clusters(rows)

    assert summary == {
        "title_prefiltered_job_count": 4,
        "prefilter_role_status_distribution": {"strong": 3, "exclude": 1},
        "prefilter_role_reason_distribution": {
            "Backend/platform role matches primary target lane": 3,
            "Non-engineering or junior/intern role": 1,
        },
        "matching_raw_variant_count": 3,
        "matching_company_title_cluster_count": 2,
        "matching_duplicate_variant_count": 1,
        "matching_variant_remote_status_distribution": {
            "pakistan_explicit": 1,
            "global_explicit": 1,
            "remote_unclear": 1,
        },
        "clearance_required_matching_variant_count": 0,
        "actionable_clearance_excluded_variant_count": 0,
        "actionable_cluster_count": 1,
    }
    acme = next(item for item in clusters if item["normalized_title"] == "senior backend engineer")
    assert acme["posting_variant_count"] == 2
    assert acme["locations"] == ["Pakistan - Remote", "Remote"]
    assert acme["role_status_distribution"] == {"strong": 2}
    assert acme["remote_eligibility_distribution"] == {
        "pakistan_explicit": 1,
        "global_explicit": 1,
    }
    assert acme["provider_distribution"] == {"ashby": 1, "greenhouse": 1}
    assert acme["best_remote_eligibility"] == "pakistan_explicit"
    assert acme["best_variant"]["external_job_id"] == "external-2"
    serialized = json.dumps(acme)
    assert "description_text" not in serialized
    assert "This remote role is open" not in serialized


def test_role_clustering_reuses_the_prefilter_classification(monkeypatch) -> None:
    original = funnel.classify_role_text
    calls = 0

    def count_classification(title: str, context: str = ""):
        nonlocal calls
        calls += 1
        return original(title, context)

    monkeypatch.setattr(funnel, "classify_role_text", count_classification)

    funnel.build_role_clusters(
        [
            job(1, description="This role is remote worldwide."),
            job(
                2,
                title="Software Engineer",
                description="This is a remote opportunity.",
            ),
            job(
                3,
                title="Product Engineer",
                description="This is a remote opportunity.",
            ),
        ]
    )

    assert calls == 3


def test_remote_leads_expand_only_explicit_weak_titles_and_assign_review_tiers() -> None:
    analysis = funnel._analyze_role_rows(
        [
            job(
                1,
                title="Software Engineer",
                description="This is a remote opportunity.",
            ),
            job(
                2,
                title="Full-Stack Developer",
                description="This is a remote opportunity.",
                location="Remote - APAC",
            ),
            job(
                3,
                title="Product Engineer",
                description="This role is remote worldwide.",
            ),
            job(
                4,
                title="Junior Software Engineer",
                description="This role is remote worldwide.",
            ),
            job(5, description="This role is remote worldwide."),
            job(
                6,
                title="Software Developer",
                description=("This remote role is open to candidates based in Pakistan."),
                location="Pakistan - Remote",
            ),
            job(
                7,
                title="Senior Platform Engineer",
                description="This is a remote role for United States candidates.",
                location="Remote - United States",
            ),
            job(
                8,
                description="This role is onsite five days per week.",
                location="Karachi",
            ),
            job(
                9,
                title="Software Engineer II",
                description="Build customer-facing product features.",
                location="San Francisco",
            ),
        ]
    )
    strict = analysis.matching_clusters
    leads = analysis.remote_leads
    summary = analysis.remote_leads_summary

    assert all(item["role_match_status"] != "weak" for item in strict)
    by_title = {item["normalized_title"]: item for item in leads}
    assert set(by_title) == {
        "software engineer",
        "full stack developer",
        "senior backend engineer",
        "software developer",
    }
    assert by_title["software engineer"]["role_match_status"] == "weak"
    assert by_title["software engineer"]["role_scope"] == ("expanded_fullstack_software")
    assert by_title["software engineer"]["lead_tier"] == "verify_country"
    assert by_title["software engineer"]["work_arrangement"] == "remote"
    assert by_title["software engineer"]["geographic_eligibility"] == "unknown"
    assert by_title["full stack developer"]["lead_tier"] == "verify_region"
    assert by_title["full stack developer"]["geographic_eligibility"] == ("regional_unconfirmed")
    assert by_title["senior backend engineer"]["role_scope"] == "primary_target"
    assert by_title["senior backend engineer"]["lead_tier"] == "confirmed"
    assert by_title["senior backend engineer"]["geographic_eligibility"] == "global"
    assert by_title["software developer"]["lead_tier"] == "confirmed"
    assert by_title["software developer"]["geographic_eligibility"] == "pakistan"
    assert all("apply-now" not in item["review_note"].casefold() for item in leads)
    assert summary["lead_company_title_cluster_count"] == 4
    assert summary["role_scope_distribution"] == {
        "primary_target": 1,
        "expanded_fullstack_software": 3,
    }
    assert summary["lead_tier_distribution"] == {
        "confirmed": 2,
        "verify_country": 1,
        "verify_region": 1,
    }
    assert summary["work_arrangement_distribution"] == {"remote": 4}
    assert summary["geographic_eligibility_distribution"] == {
        "pakistan": 1,
        "global": 1,
        "unknown": 1,
        "regional_unconfirmed": 1,
    }
    assert summary["candidate_variant_count"] == 7
    assert summary["candidate_remote_status_distribution"] == {
        "pakistan_explicit": 1,
        "global_explicit": 1,
        "regional_unconfirmed": 1,
        "remote_unclear": 1,
        "restricted_remote": 1,
        "onsite_explicit": 1,
        "no_remote_evidence": 1,
    }
    assert summary["queue_exclusion_distribution"] == {
        "no_remote_evidence": 1,
        "onsite_explicit": 1,
        "restricted_remote": 1,
    }


def test_remote_leads_include_sde_but_preserve_role_lane_exclusions() -> None:
    analysis = funnel._analyze_role_rows(
        [
            job(
                1,
                title="Software Development Engineer II",
                description="This role is remote worldwide.",
            ),
            job(
                2,
                title="Junior Software Development Engineer",
                description="This role is remote worldwide.",
            ),
            job(
                3,
                title="Software Development Engineer in Test",
                description="This role is remote worldwide.",
            ),
            job(
                4,
                title="Software Development Engineer IV - QA",
                description="This role is remote worldwide.",
            ),
            job(
                5,
                title="QA Engineer",
                description="This role is remote worldwide.",
            ),
            job(
                6,
                title="Engineering Manager",
                description="This role is remote worldwide.",
            ),
            job(
                7,
                title="Software Systems Engineer",
                description="This is a remote role.",
            ),
            job(
                8,
                title="SDE II",
                description="This is a remote role.",
            ),
        ]
    )
    leads = analysis.remote_leads
    summary = analysis.remote_leads_summary

    assert funnel.is_expanded_remote_lead_title("Software Development Engineer II")
    assert funnel.is_expanded_remote_lead_title("SDE II")
    assert "sde" in funnel.ROLE_TITLE_PREFILTER_PATTERN
    assert [item["normalized_title"] for item in leads] == [
        "software development engineer ii",
        "sde ii",
        "software systems engineer",
    ]
    assert leads[0]["role_match_status"] == "weak"
    assert leads[0]["role_scope"] == "expanded_fullstack_software"
    assert leads[0]["lead_tier"] == "confirmed"
    assert summary["lead_company_title_cluster_count"] == 3
    assert summary["role_scope_distribution"] == {"expanded_fullstack_software": 3}


def test_required_active_clearance_stays_measurable_but_is_excluded_from_queues() -> None:
    analysis = funnel._analyze_role_rows(
        [
            job(
                1,
                description=(
                    "This role is remote worldwide. Candidates must possess an active "
                    "TS/SCI government clearance."
                ),
            ),
            job(
                2,
                title="Software Engineer",
                description=(
                    "This role is remote worldwide. An active Secret clearance is required."
                ),
            ),
            job(
                3,
                title="Software Developer",
                description=(
                    "This role is remote worldwide. Candidates must be able to obtain and "
                    "maintain an active Secret clearance."
                ),
            ),
        ]
    )
    strict = analysis.matching_clusters
    strict_summary = analysis.role_summary
    leads = analysis.remote_leads
    lead_summary = analysis.remote_leads_summary

    assert strict_summary["actionable_cluster_count"] == 0
    assert strict_summary["clearance_required_matching_variant_count"] == 1
    assert strict_summary["actionable_clearance_excluded_variant_count"] == 1
    assert strict[0]["best_variant"]["external_job_id"] == "external-1"
    assert strict[0]["best_variant"]["requires_active_clearance"] is True
    assert strict[0]["required_active_clearance_variant_count"] == 1
    assert analysis.actionable_clusters == []
    with pytest.raises(ValueError, match="active government clearance"):
        funnel.actionable_csv_row(strict[0])
    with pytest.raises(ValueError, match="active government clearance"):
        funnel.remote_lead_csv_row(strict[0])
    assert [item["best_variant"]["external_job_id"] for item in leads] == ["external-3"]
    assert lead_summary["clearance_excluded_variant_count"] == 2
    assert all(item["required_active_clearance_variant_count"] == 0 for item in leads)
    assert "Candidates must be able to obtain" not in json.dumps(leads)


def test_required_clearance_in_title_is_excluded_without_active_keyword() -> None:
    analysis = funnel._analyze_role_rows(
        [
            job(
                1,
                title="Senior Backend Engineer (TS/SCI Clearance Required)",
                description="This role is remote worldwide.",
            ),
            job(
                2,
                title="Senior Backend Engineer (TS/SCI Clearance Preferred)",
                description="This role is remote worldwide.",
            ),
        ]
    )

    assert analysis.role_summary["clearance_required_matching_variant_count"] == 1
    assert analysis.role_summary["actionable_clearance_excluded_variant_count"] == 1
    assert [item["best_variant"]["external_job_id"] for item in analysis.actionable_clusters] == [
        "external-2"
    ]
    assert [item["best_variant"]["external_job_id"] for item in analysis.remote_leads] == [
        "external-2"
    ]


@pytest.mark.parametrize(
    "description",
    (
        "Candidates must possess an active TS/SCI government clearance.",
        "An active Secret clearance is required.",
        "Minimum qualification: an active Top Secret clearance.",
        "Applicants are required to hold an active Secret clearance.",
        "An active Secret clearance is mandatory.",
        "Must have an active DoD security clearance.",
        "Must hold an active TS/SCI.",
        "TS/SCI Clearance Required.",
        "A Top Secret clearance is mandatory.",
        "Candidates must hold a Secret clearance.",
        ("<p>Required Qualifications:</p><ul><li>Active Secret clearance</li></ul>"),
        ("<li><strong>Investigative Requirement</strong>: Secret clearance.</li>"),
    ),
)
def test_active_clearance_filter_detects_clause_local_requirements(
    description: str,
) -> None:
    assert funnel.requires_active_government_clearance(description)


@pytest.mark.parametrize(
    "description",
    (
        "Required: five years of Python. An active Secret clearance is preferred but not required.",
        "An active Secret clearance is preferred. Python experience is required.",
        "Python expertise is required, while an active Secret clearance is preferred.",
        "The role requires Python and prefers candidates with an active Secret clearance.",
        "Preferred qualification: an active Secret clearance.",
        "Candidates must be able to obtain and maintain an active Secret clearance.",
        "No active Secret clearance is required.",
        "An active Secret clearance is required or the ability to obtain one.",
        "You must have Python experience. Active Secret clearance is a nice-to-have.",
        "TS/SCI clearance is preferred but not required.",
        "Ability to obtain a TS/SCI clearance is required.",
        "No TS/SCI clearance is required.",
        "A Top Secret clearance is optional.",
        ("<p>Preferred Qualifications:</p><ul><li>Active Secret clearance</li></ul>"),
        (
            "<p>Required Qualifications:</p><ul><li>Ability to obtain an active "
            "Secret clearance</li></ul>"
        ),
    ),
)
def test_active_clearance_filter_preserves_optional_and_obtainable_roles(
    description: str,
) -> None:
    assert not funnel.requires_active_government_clearance(description)


def test_remote_lead_clustering_and_atomic_csv_do_not_expose_descriptions(
    tmp_path: Path,
) -> None:
    analysis = funnel._analyze_role_rows(
        [
            job(
                1,
                title="Software Engineer",
                description="This role is remote worldwide.",
            ),
            job(
                2,
                title="Software Engineer!",
                description="This is a remote opportunity.",
                location="Remote",
                provider="ashby",
            ),
        ]
    )
    leads = analysis.remote_leads
    summary = analysis.remote_leads_summary

    assert len(leads) == 1
    lead = leads[0]
    assert lead["posting_variant_count"] == 2
    assert lead["best_remote_eligibility"] == "global_explicit"
    assert lead["remote_eligibility_distribution"] == {
        "global_explicit": 1,
        "remote_unclear": 1,
    }
    assert lead["provider_distribution"] == {"ashby": 1, "greenhouse": 1}
    assert summary["lead_duplicate_variant_count"] == 1
    assert "description_text" not in json.dumps(lead)
    assert "This role is remote worldwide" not in json.dumps(lead)

    output = tmp_path / "remote_role_leads.csv"
    funnel.write_remote_leads_csv_atomic(output, leads)
    with output.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 1
    assert rows[0]["lead_tier"] == "confirmed"
    assert rows[0]["role_scope"] == "expanded_fullstack_software"
    assert rows[0]["work_arrangement"] == "remote"
    assert rows[0]["geographic_eligibility"] == "global"
    assert rows[0]["posting_url"] == "https://jobs.example/1"
    assert "description_text" not in rows[0]
    original = output.read_bytes()

    invalid = {**lead, "best_remote_eligibility": "restricted_remote"}
    with pytest.raises(ValueError, match="allowed remote eligibility"):
        funnel.write_remote_leads_csv_atomic(output, [invalid])
    assert output.read_bytes() == original

    wrong_geography = {**lead, "geographic_eligibility": "pakistan"}
    with pytest.raises(ValueError, match="geographic eligibility"):
        funnel.write_remote_leads_csv_atomic(output, [wrong_geography])
    assert output.read_bytes() == original

    wrong_arrangement = {**lead, "work_arrangement": "hybrid"}
    with pytest.raises(ValueError, match="work_arrangement=remote"):
        funnel.write_remote_leads_csv_atomic(output, [wrong_arrangement])
    assert output.read_bytes() == original
    assert list(tmp_path.glob(f".{output.name}.*")) == []


def test_application_queues_separate_and_rank_apply_and_verification_work() -> None:
    rows = [
        job(
            1,
            title="Senior Backend Engineer",
            description="This role is remote worldwide.",
        ),
        job(
            2,
            title="Software Engineer",
            description="This role is remote worldwide.",
        ),
        job(
            3,
            title="Senior Platform Engineer",
            description="This is a fully remote role.",
        ),
        job(
            4,
            title="Senior Software Engineer",
            description="This is a remote APAC role.",
            location="Remote - APAC",
        ),
    ]
    rows[0]["source_published_at"] = datetime(2026, 7, 30, tzinfo=UTC)
    rows[1]["source_published_at"] = datetime(2026, 7, 31, tzinfo=UTC)
    rows[2]["source_published_at"] = datetime(2026, 7, 29, tzinfo=UTC)
    rows[3]["source_published_at"] = datetime(2026, 7, 28, tzinfo=UTC)
    analysis = funnel._analyze_role_rows(rows)

    apply_rows, verify_rows, summary = funnel.build_application_queues(
        analysis.remote_leads,
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert [row["title"] for row in apply_rows] == [
        "Senior Backend Engineer",
        "Software Engineer",
    ]
    assert [row["priority_rank"] for row in apply_rows] == [1, 2]
    assert all(row["recommendation"] == "apply_now" for row in apply_rows)
    assert apply_rows[0]["recommendation_score"] > apply_rows[1]["recommendation_score"]
    assert [row["recommendation"] for row in verify_rows] == [
        "verify_country_then_apply",
        "verify_region_then_apply",
    ]
    assert summary["apply_now_count"] == 2
    assert summary["verify_before_apply_count"] == 2
    assert summary["recommendation_distribution"] == {
        "apply_now": 2,
        "verify_country_then_apply": 1,
        "verify_region_then_apply": 1,
    }
    assert "description_text" not in json.dumps([*apply_rows, *verify_rows])


def test_application_queue_csv_is_atomic_and_has_no_profile_or_description(
    tmp_path: Path,
) -> None:
    analysis = funnel._analyze_role_rows(
        [job(1, description="This role is remote worldwide. PRIVATE_DESCRIPTION")]
    )
    apply_rows, _, _ = funnel.build_application_queues(
        analysis.remote_leads,
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
    )
    output = tmp_path / "jobs_to_apply.csv"

    funnel.write_application_queue_csv_atomic(output, apply_rows)

    with output.open(newline="", encoding="utf-8") as source:
        written = list(csv.DictReader(source))
    assert len(written) == 1
    assert written[0]["recommendation"] == "apply_now"
    assert written[0]["application_url"] == "https://jobs.example/1/apply"
    assert "PRIVATE_DESCRIPTION" not in output.read_text()
    assert "description_text" not in written[0]


def test_adjacent_engineering_title_requires_role_fit_review() -> None:
    row = job(
        1,
        title="Senior Security Engineer",
        description=(
            "Build backend platform and infrastructure security. This role is remote worldwide."
        ),
    )
    row["source_published_at"] = datetime(2026, 7, 31, tzinfo=UTC)
    analysis = funnel._analyze_role_rows([row])

    apply_rows, verify_rows, summary = funnel.build_application_queues(
        analysis.remote_leads,
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert apply_rows == []
    assert len(verify_rows) == 1
    assert verify_rows[0]["recommendation"] == "verify_role_fit_then_apply"
    assert verify_rows[0]["title_alignment"] == "supporting_engineering"
    assert verify_rows[0]["remote_eligibility"] == "global_explicit"
    assert "software/backend aligned" in verify_rows[0]["manual_check"]
    assert summary["title_alignment_distribution"] == {"supporting_engineering": 1}


def test_rerank_mode_rebuilds_queues_without_database_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = funnel._analyze_role_rows(
        [job(1, description="This role is remote worldwide.")]
    )
    report_path = tmp_path / "job_funnel_report.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "as_of": "2026-08-01T00:00:00+00:00",
                "remote_role_leads": analysis.remote_leads,
                "actionable_clusters": analysis.actionable_clusters,
            },
            default=funnel.iso_value,
        ),
        encoding="utf-8",
    )

    def fail_database_access(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("rerank mode must not open Postgres")

    monkeypatch.setattr(funnel, "engine_from_url", fail_database_access)
    funnel.main(["--rerank-report", str(report_path)])

    with (tmp_path / "jobs_to_apply.csv").open(newline="", encoding="utf-8") as source:
        apply_rows = list(csv.DictReader(source))
    assert len(apply_rows) == 1
    assert apply_rows[0]["recommendation"] == "apply_now"
    rewritten = json.loads(report_path.read_text())
    assert rewritten["schema_version"] == funnel.SCHEMA_VERSION
    assert rewritten["application_queue_analysis"]["apply_now_count"] == 1
    assert "application_queue_generated_at" in rewritten


def test_report_artifacts_publish_as_one_prevalidated_set(tmp_path: Path) -> None:
    report = {"generation": "new", "remote_role_leads": []}

    published = funnel.publish_report_artifacts(
        tmp_path,
        report=report,
        actionable=[],
    )

    assert tuple(published) == funnel.REPORT_ARTIFACT_FILENAMES
    assert json.loads(published["job_funnel_report.json"].read_text()) == report
    assert (
        published["actionable_job_clusters.csv"]
        .read_text()
        .startswith("company_name,company_slug,")
    )
    assert published["remote_role_leads.csv"].read_text().startswith("lead_tier,role_scope,")
    assert (
        published["jobs_to_apply.csv"]
        .read_text()
        .startswith("priority_rank,priority_band,recommendation,")
    )
    assert (
        published["jobs_to_verify.csv"]
        .read_text()
        .startswith("priority_rank,priority_band,recommendation,")
    )
    assert list(tmp_path.glob(".*.stage-*")) == []
    assert list(tmp_path.glob(".*.backup-*")) == []


def test_report_artifact_prevalidation_preserves_existing_set(tmp_path: Path) -> None:
    originals: dict[str, bytes] = {}
    for name in funnel.REPORT_ARTIFACT_FILENAMES:
        content = f"old:{name}\n".encode()
        (tmp_path / name).write_bytes(content)
        originals[name] = content
    invalid_clearance_lead = {"required_active_clearance_variant_count": 1}

    with pytest.raises(ValueError, match="active government clearance"):
        funnel.publish_report_artifacts(
            tmp_path,
            report={"remote_role_leads": [invalid_clearance_lead]},
            actionable=[],
        )

    assert {
        name: (tmp_path / name).read_bytes() for name in funnel.REPORT_ARTIFACT_FILENAMES
    } == originals
    assert list(tmp_path.glob(".*.stage-*")) == []
    assert list(tmp_path.glob(".*.backup-*")) == []


def test_report_artifact_publish_failure_rolls_back_entire_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    originals: dict[str, bytes] = {}
    for name in funnel.REPORT_ARTIFACT_FILENAMES:
        content = f"old:{name}\n".encode()
        (tmp_path / name).write_bytes(content)
        originals[name] = content

    original_replace = funnel.os.replace
    published_replacements = 0

    def fail_second_publish(source: str | Path, destination: str | Path) -> None:
        nonlocal published_replacements
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            destination_path.name in funnel.REPORT_ARTIFACT_FILENAMES
            and ".stage-" in source_path.name
        ):
            published_replacements += 1
            if published_replacements == 2:
                raise OSError("injected second-artifact publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(funnel.os, "replace", fail_second_publish)

    with pytest.raises(OSError, match="injected second-artifact"):
        funnel.publish_report_artifacts(
            tmp_path,
            report={"generation": "new", "remote_role_leads": []},
            actionable=[],
        )

    assert {
        name: (tmp_path / name).read_bytes() for name in funnel.REPORT_ARTIFACT_FILENAMES
    } == originals
    assert list(tmp_path.glob(".*.stage-*")) == []
    assert list(tmp_path.glob(".*.backup-*")) == []


def test_actionable_csv_rejects_unclear_and_writes_only_explicit_clusters(
    tmp_path: Path,
) -> None:
    clusters, _ = funnel.build_role_clusters(
        [
            job(1, description="This role is remote worldwide."),
            job(
                2,
                title="Senior Platform Engineer",
                description="This is a remote opportunity.",
            ),
        ]
    )
    actionable = [item for item in clusters if funnel.is_actionable_cluster(item)]
    unclear = next(item for item in clusters if not funnel.is_actionable_cluster(item))
    output = tmp_path / "actionable.csv"

    with pytest.raises(ValueError, match="explicit Pakistan or global"):
        funnel.actionable_csv_row(unclear)
    funnel.write_actionable_csv_atomic(output, actionable)

    with output.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 1
    assert rows[0]["best_remote_eligibility"] == "global_explicit"
    assert "work authorization" in rows[0]["eligibility_caveat"]
    assert "description_text" not in rows[0]


def test_age_buckets_and_nearest_rank_are_deterministic() -> None:
    as_of = datetime(2026, 8, 1, tzinfo=UTC)

    assert funnel.age_bucket(None, as_of=as_of) == "unknown"
    assert funnel.age_bucket(as_of + timedelta(days=1), as_of=as_of) == "future"
    assert funnel.age_bucket(as_of - timedelta(days=30), as_of=as_of) == "0_30_days"
    assert funnel.age_bucket(as_of - timedelta(days=31), as_of=as_of) == "31_90_days"
    assert funnel.age_bucket(as_of - timedelta(days=91), as_of=as_of) == "91_180_days"
    assert funnel.age_bucket(as_of - timedelta(days=181), as_of=as_of) == "181_365_days"
    assert funnel.age_bucket(as_of - timedelta(days=366), as_of=as_of) == "over_365_days"
    assert funnel.nearest_rank([100, 1, 11, 3], 0.50) == 3
    assert funnel.nearest_rank([100, 1, 11, 3], 0.99) == 100
    assert funnel.nearest_rank([], 0.50) is None
    with pytest.raises(ValueError, match="between zero and one"):
        funnel.nearest_rank([1], 1.1)


def test_history_summary_embeds_available_counts_and_tolerates_missing_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / "greenhouse-candidate-union.manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "union_token_count": 30,
                "evidence_row_count": 50,
                "inputs": [
                    {
                        "crawl_id": "CC-MAIN-2026-30",
                        "input_row_count": 20,
                        "token_count": 18,
                        "marginal_new_tokens": 18,
                        "path": "/do/not/embed/raw-path.csv",
                    },
                    {
                        "crawl_id": "CC-MAIN-2026-21",
                        "input_row_count": 20,
                        "token_count": 16,
                        "marginal_new_tokens": 12,
                    },
                ],
                "outputs": {"large": "mapping is intentionally not copied"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "greenhouse-sync.checkpoint.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": {
                    "1": {"state": "completed", "attempts": 1},
                    "2": {
                        "state": "terminal_failed",
                        "attempts": 1,
                        "retryable": False,
                    },
                    "3": {"state": "failed", "attempts": 3, "retryable": True},
                },
            }
        ),
        encoding="utf-8",
    )

    summary = funnel.summarize_history_run_dir(tmp_path)

    assert summary is not None
    assert summary["exists"] is True
    assert summary["available_artifact_count"] == 2
    assert "greenhouse-scout.status.json" in summary["missing_artifacts"]
    union = summary["artifacts"]["union"]["summary"]
    assert union["union_token_count"] == 30
    assert [item["marginal_new_tokens"] for item in union["inputs"]] == [18, 12]
    assert "path" not in union["inputs"][0]
    checkpoint = summary["artifacts"]["sync_checkpoint"]["summary"]
    assert checkpoint["source_count"] == 3
    assert checkpoint["source_state_distribution"] == {
        "completed": 1,
        "failed": 1,
        "terminal_failed": 1,
    }
    assert checkpoint["attempt_distribution"] == {"1": 2, "3": 1}
    assert checkpoint["retryable_source_count"] == 1


def test_build_report_emits_bounded_evidence_without_full_descriptions() -> None:
    candidate_rows = [
        job(
            1,
            description=(
                "This role is remote worldwide. "
                "FULL_DESCRIPTION_PRIVATE_TAIL " + "backend systems " * 80
            ),
        )
    ]
    report, actionable = funnel.build_report(
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
        provider_funnel={"provider_count": 1},
        active_overview={
            "raw_active_job_count": 10,
            "company_normalized_title_cluster_count": 9,
            "duplicate_location_or_board_variant_count": 1,
            "source_published_age_buckets": {"0_30_days": 10},
        },
        structured_evidence={"active_jobs": 10},
        jobs_per_board={"boards_with_active_jobs": 1},
        candidate_rows=candidate_rows,
        history_run_dir=None,
    )

    assert report["role_analysis"]["all_active_role_status_distribution"] == {
        "strong": 1,
        "possible": 0,
        "weak": 0,
        "exclude": 9,
    }
    assert len(actionable) == 1
    assert report["remote_leads_analysis"]["lead_company_title_cluster_count"] == 1
    assert report["remote_leads_analysis"]["dimensions"] == {
        "work_arrangement": "All emitted leads have explicit remote evidence.",
        "geographic_eligibility": {
            "pakistan_explicit": "pakistan",
            "global_explicit": "global",
            "remote_unclear": "unknown",
            "regional_unconfirmed": "regional_unconfirmed",
        },
    }
    assert report["remote_role_leads"][0]["lead_tier"] == "confirmed"
    assert report["remote_role_leads"][0]["work_arrangement"] == "remote"
    assert report["remote_role_leads"][0]["geographic_eligibility"] == "global"
    assert report["application_queue_analysis"]["apply_now_count"] == 1
    assert report["application_queue_analysis"]["verify_before_apply_count"] == 0
    assert report["jobs_to_apply"][0]["recommendation"] == "apply_now"
    assert report["jobs_to_verify"] == []
    serialized = json.dumps(report)
    assert "global signal: this role is remote worldwide" in serialized
    assert "FULL_DESCRIPTION_PRIVATE_TAIL" not in serialized
    assert '"description_text":' not in serialized
    assert "Bounded evidence excerpts may be emitted" in report["scope"]["description_handling"]
    assert "full descriptions" in report["remote_leads_analysis"]["description_handling"]
    assert report["scope"]["remote_eligibility"].startswith("Deterministic")


def test_structured_evidence_coverage_query_executes_against_migrated_postgres(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    company = CompanyRegistry(engine).register_company(
        name="Funnel Evidence Example",
        website="https://funnel-evidence.example",
    )
    source = JobSourceRegistry(engine).register_url(
        company_id=company.company_id,
        source_url="https://job-boards.greenhouse.io/funnel-evidence-example",
    )
    normalized = normalize_greenhouse_job(
        {
            "id": 123,
            "title": "Senior Backend Engineer",
            "absolute_url": ("https://job-boards.greenhouse.io/funnel-evidence-example/jobs/123"),
            "content": "<p>This role is remote worldwide.</p>",
            "location": {"name": "Remote"},
            "departments": [{"name": "Engineering"}],
            "metadata": [
                {
                    "name": "Eligible countries",
                    "value_type": "multi_select",
                    "value": ["Pakistan"],
                }
            ],
        }
    )
    snapshot = SourceSnapshot(
        provider="greenhouse",
        external_source_id="funnel-evidence-example",
        adapter_version="3",
        is_complete=True,
        http_status=200,
        jobs=[normalized],
    )
    JobSyncService(engine).sync_snapshot(
        career_source_id=source.career_source_id,
        run_key="coverage-query",
        snapshot=snapshot,
    )

    with engine.connect() as connection:
        coverage = funnel.collect_structured_evidence_coverage(connection)

    assert coverage["active_jobs"] == 1
    assert coverage["nonempty"] == 1
    assert coverage["nonempty_percent"] == 100.0
    assert coverage["eligibility_signals"] == 1
    assert coverage["providers"]["greenhouse"]["primary_location"] == 1
    engine.dispose()
