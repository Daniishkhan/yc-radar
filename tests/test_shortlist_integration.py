import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot
from yc_radar.domain.models import Company
from yc_radar.services.candidate_fit import DEFAULT_CANDIDATE_PROFILE, score_company, target_record
from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.company_repository import CompanyRepository
from yc_radar.services.database import engine_from_url, upsert_yc_companies
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_sync_service import JobSyncService

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_weekly_targets.py"
SPEC = importlib.util.spec_from_file_location("generate_weekly_targets", SCRIPT_PATH)
assert SPEC and SPEC.loader
generate_weekly_targets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generate_weekly_targets)


def test_weekly_ranking_includes_website_less_company_with_strong_managed_job(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    provisional = CompanyRegistry(engine).register_provisional_company(
        name="Unresolved Global Employer",
        requested_slug="unresolved-global-employer",
        now=now,
    )
    source, allowed, created = JobRepository(engine).register_source(
        company_id=provisional.company_id,
        provider="greenhouse",
        source_kind="ats_board",
        external_id="unresolved-global-employer",
        source_url="https://job-boards.greenhouse.io/unresolved-global-employer",
        sync_mode="complete_snapshot",
        now=now,
    )
    assert allowed is True
    assert created is True
    job = NormalizedJob(
        external_job_id="global-backend-1",
        title="Senior Backend Engineer",
        posting_url=(
            "https://job-boards.greenhouse.io/unresolved-global-employer/"
            "jobs/global-backend-1"
        ),
        location="Remote - Worldwide",
        description_text="Build globally distributed API systems from anywhere in the world.",
        content_hash="global-backend-1-v1",
        raw_payload={"id": "global-backend-1"},
    )
    JobSyncService(engine, clock=lambda: now).sync_snapshot(
        company_source_id=source["id"],
        run_key="verified-global-job",
        snapshot=SourceSnapshot(
            provider="greenhouse",
            external_source_id="unresolved-global-employer",
            adapter_version="test",
            is_complete=True,
            jobs=[job],
            http_status=200,
        ),
    )
    upsert_yc_companies(
        engine,
        [
            {
                "id": 900,
                "name": "Metadata Only",
                "slug": "metadata-only",
                "website": "https://metadata-only.example",
                "one_liner": "AI data infrastructure and backend automation",
                "status": "Active",
                "batch": "S25",
                "team_size": 5,
                "isHiring": True,
                "prototype_score": 20,
            }
        ],
    )

    companies = CompanyRepository(postgres_database_url).list()
    jobs_by_slug = generate_weekly_targets.load_jobs_by_slug(postgres_database_url)
    targets = generate_weekly_targets.build_ranked_candidate_targets(
        companies,
        DEFAULT_CANDIDATE_PROFILE,
        jobs_by_slug,
        max_team_size=25,
    )

    assert {target["slug"] for target in targets} == {
        "unresolved-global-employer",
        "metadata-only",
    }
    provisional_target = next(
        target for target in targets if target["slug"] == "unresolved-global-employer"
    )
    metadata_target = next(target for target in targets if target["slug"] == "metadata-only")
    assert provisional_target["website"] is None
    assert provisional_target["managed_role_match_status"] == "strong"
    assert provisional_target["managed_best_remote_eligibility"] == "global_explicit"
    assert provisional_target["fit_score"] > metadata_target["fit_score"]
    assert targets[0]["slug"] == "unresolved-global-employer"


def test_active_jobs_change_shortlist_role_evidence_with_public_provenance(
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
    source, _, _ = JobRepository(engine).register_source(
        company_id=1,
        provider="greenhouse",
        source_kind="ats_board",
        external_id="example",
        source_url="https://boards.greenhouse.io/example",
        sync_mode="complete_snapshot",
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
    service.sync_snapshot(company_source_id=source["id"], run_key="initial", snapshot=complete)

    active = JobRepository(engine).list_jobs()
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
        score_company(company, DEFAULT_CANDIDATE_PROFILE), rank=1, jobs=active
    )
    assert target["role_match_status"] == "strong"
    assert target["managed_active_job_count"] == 1
    assert target["managed_raw_active_job_count"] == 1
    assert target["managed_duplicate_posting_count"] == 0
    provenance = target["matching_job_provenance"]
    assert len(provenance) == 1
    cluster = provenance[0]
    assert cluster["title"] == "Senior Backend Engineer"
    assert cluster["provider"] == "greenhouse"
    assert cluster["external_job_id"] == "42"
    assert cluster["company_source_id"] == source["id"]
    assert cluster["source_external_id"] == "example"
    assert cluster["source_kind"] == "ats_board"
    assert cluster["source_url"] == "https://boards.greenhouse.io/example"
    assert cluster["posting_url"] == "https://boards.greenhouse.io/example/jobs/42"
    assert cluster["remote_eligibility"] == "no_remote_evidence"
    assert cluster["remote_reasons"] == [
        "No role-specific remote or onsite evidence was detected"
    ]
    assert cluster["remote_evidence"] == []
    assert cluster["structured_evidence"] == {}
    assert cluster["posting_variant_count"] == 1
    assert cluster["remote_eligibility_distribution"] == {"no_remote_evidence": 1}
    assert len(cluster["posting_variants"]) == 1
    posting_variant = cluster["posting_variants"][0]
    comparable_fields = (
        "title",
        "provider",
        "external_job_id",
        "company_source_id",
        "source_external_id",
        "source_kind",
        "source_url",
        "posting_url",
        "location",
        "department",
        "remote_eligibility",
        "remote_reasons",
        "remote_evidence",
        "structured_evidence",
        "source_published_at",
        "source_updated_at",
    )
    assert {key: posting_variant[key] for key in comparable_fields} == {
        key: cluster[key] for key in comparable_fields
    }
    assert posting_variant["source_kind"] == "ats_board"
    assert posting_variant["source_record_id"] == str(active[0]["source_record_id"])

    generate_weekly_targets.refresh_role_focus(
        [target], {"example": company}, {"example": active}
    )
    assert target["matching_job_titles"] == ["Senior Backend Engineer"]

    service.sync_snapshot(
        company_source_id=source["id"],
        run_key="miss-one",
        snapshot=complete.model_copy(update={"jobs": []}),
    )
    service.sync_snapshot(
        company_source_id=source["id"],
        run_key="miss-two",
        snapshot=complete.model_copy(update={"jobs": []}),
    )
    assert JobRepository(engine).list_jobs() == []
