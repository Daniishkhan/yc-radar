import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot
from yc_radar.domain.models import Company
from yc_radar.services.candidate_fit import DEFAULT_CANDIDATE_PROFILE, score_company, target_record
from yc_radar.services.database import engine_from_url, upsert_yc_companies
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_sync_service import JobSyncService

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_weekly_targets.py"
SPEC = importlib.util.spec_from_file_location("generate_weekly_targets", SCRIPT_PATH)
assert SPEC and SPEC.loader
generate_weekly_targets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generate_weekly_targets)


def test_active_canonical_jobs_change_shortlist_role_evidence_with_public_provenance(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "Example",
                "slug": "example",
                "website": "https://example.test",
                "one_liner": "Workflow software",
                "team_size": 5,
                "regions": [],
                "industries": [],
                "tags": [],
            }
        ],
    )
    source, _, _ = JobRepository(engine).register_career_source(
        company_id=1,
        provider="greenhouse",
        source_kind="ats_board",
        external_source_id="example",
        source_url="https://boards.greenhouse.io/example",
        discovered_from_url="https://boards.greenhouse.io/example",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    job = NormalizedJob(
        external_job_id="42",
        title="Senior Backend Engineer",
        posting_url="https://boards.greenhouse.io/example/jobs/42",
        description_text="Own distributed API reliability.",
        content_hash="backend-42",
        raw_payload={"id": 42},
    )
    complete = SourceSnapshot(
        provider="greenhouse",
        external_source_id="example",
        adapter_version="test",
        is_complete=True,
        jobs=[job],
        http_status=200,
    )
    service = JobSyncService(engine, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    service.sync_snapshot(career_source_id=source["id"], run_key="initial", snapshot=complete)

    active = JobRepository(engine).active_job_rows()
    assert len(active) == 1
    company = Company(
        id=1,
        name="Example",
        slug="example",
        yc_url="https://www.ycombinator.com/companies/example",
        website="https://example.test",
        one_liner="Workflow software",
        team_size=5,
    )
    target = target_record(
        score_company(company, DEFAULT_CANDIDATE_PROFILE), rank=1, canonical_jobs=active
    )
    assert target["role_match_status"] == "strong"
    assert target["canonical_active_job_count"] == 1
    assert target["matching_job_provenance"] == [
        {
            "title": "Senior Backend Engineer",
            "provider": "greenhouse",
            "external_job_id": "42",
            "career_source_kind": "ats_board",
            "career_source_url": "https://boards.greenhouse.io/example",
            "posting_url": "https://boards.greenhouse.io/example/jobs/42",
            "source_published_at": None,
            "source_updated_at": None,
        }
    ]

    generate_weekly_targets.refresh_role_focus(
        [target], {"example": company}, {}, {"example": active}
    )
    assert target["matching_job_titles"] == ["Senior Backend Engineer"]

    service.sync_snapshot(
        career_source_id=source["id"],
        run_key="miss-one",
        snapshot=complete.model_copy(update={"jobs": []}),
    )
    service.sync_snapshot(
        career_source_id=source["id"],
        run_key="miss-two",
        snapshot=complete.model_copy(update={"jobs": []}),
    )
    assert JobRepository(engine).active_job_rows() == []
