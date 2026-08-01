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
        "matching_raw_variant_count": 3,
        "matching_company_title_cluster_count": 2,
        "matching_duplicate_variant_count": 1,
        "matching_variant_remote_status_distribution": {
            "pakistan_explicit": 1,
            "global_explicit": 1,
            "remote_unclear": 1,
        },
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


def test_build_report_accounts_for_non_prefiltered_jobs_without_exposing_descriptions() -> None:
    candidate_rows = [job(1, description="This role is remote worldwide.")]
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
    serialized = json.dumps(report)
    assert "This role is remote worldwide" not in serialized
    assert "description_text" not in serialized
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
            "absolute_url": (
                "https://job-boards.greenhouse.io/funnel-evidence-example/jobs/123"
            ),
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
