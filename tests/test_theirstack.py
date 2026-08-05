from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from yc_radar.services.candidate_fit import classify_remote_eligibility
from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    engine_from_url,
    ingest_raw_observations_table,
    ingest_url_work_items_table,
    jobs_table,
    sync_runs_table,
)
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.theirstack import (
    DEFAULT_SEARCH_STRATA,
    GLOBAL_REMOTE_DESCRIPTION_PATTERNS,
    THEIRSTACK_PROVIDER,
    import_theirstack_jobs,
    normalize_theirstack_job,
    paid_search_body,
    plan_digest,
    preview_search_body,
    quota_by_stratum,
    resolve_theirstack_company,
    select_preview_jobs,
)


FIXTURE = Path(__file__).parent / "fixtures" / "theirstack_jobs.json"


def fixture_jobs() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]


def test_default_query_mix_uses_global_remote_filters_without_pakistan_gate() -> None:
    quotas = quota_by_stratum(175)
    assert quotas == {
        "global_explicit": 40,
        "backend": 25,
        "software": 25,
        "fullstack": 20,
        "production_ai": 20,
        "data_engineering": 15,
        "frontend": 10,
        "platform_infra": 15,
        "founding": 5,
    }

    body = preview_search_body(
        DEFAULT_SEARCH_STRATA[0],
        page=0,
        excluded_job_ids=[11, 12],
    )
    assert body["remote"] is True
    assert body["is_closed"] is False
    assert body["company_type"] == "direct_employer"
    assert body["employment_statuses_or"] == ["full_time"]
    assert body["job_seniority_or"] == ["mid_level", "senior", "staff"]
    assert body["property_exists_and"] == ["final_url"]
    assert body["job_id_not"] == [11, 12]
    assert body["blur_company_data"] is True
    assert body["include_total_results"] is True
    assert body["job_description_pattern_or"]
    assert "job_country_code_or" not in body
    assert "pakistan" not in json.dumps(body).casefold()
    assert any(
        re.search(pattern, "Work from anywhere in the world.")
        for pattern in GLOBAL_REMOTE_DESCRIPTION_PATTERNS
    )
    for restricted_anywhere_claim in (
        "This role is remote from anywhere in the United States.",
        "Work remotely from anywhere in Canada.",
        "Work from anywhere in the United States.",
        "Open to candidates based anywhere in Australia.",
        "We hire people from anywhere in Europe.",
        "Work from anywhere except Canada.",
        "Work from anywhere, except Canada.",
        "Work from anywhere — within Canada.",
        "Work from anywhere for up to 8 weeks per year.",
        "Work from anywhere 20 days per year.",
    ):
        assert not any(
            re.search(pattern, restricted_anywhere_claim)
            for pattern in GLOBAL_REMOTE_DESCRIPTION_PATTERNS
        ), restricted_anywhere_claim

    with pytest.raises(ValueError, match="between 0 and 4"):
        preview_search_body(DEFAULT_SEARCH_STRATA[0], page=5)
    with pytest.raises(ValueError, match="between 1 and 25"):
        paid_search_body([])
    assert paid_search_body([1, 2]) == {
        "posted_at_max_age_days": 90,
        "is_closed": False,
        "job_id_or": [1, 2],
        "include_total_results": False,
        "limit": 2,
        "page": 0,
    }


def test_preview_selection_deduplicates_balances_roles_and_limits_staff() -> None:
    previews: dict[str, list[dict]] = {}
    next_id = 1
    for stratum in DEFAULT_SEARCH_STRATA:
        rows = []
        for index in range(15):
            if stratum.name == "global_explicit":
                title = "Senior Software Engineer - Worldwide Remote"
            elif stratum.name == "backend":
                title = "Senior Backend Engineer"
            elif stratum.name == "software":
                title = "Software Engineer"
            elif stratum.name == "fullstack":
                title = "Senior Full-Stack Engineer"
            elif stratum.name == "production_ai":
                title = "Senior Machine Learning Engineer"
            elif stratum.name == "data_engineering":
                title = "Senior Data Engineer"
            elif stratum.name == "frontend":
                title = "Senior Frontend Engineer"
            elif stratum.name == "platform_infra":
                title = "Senior Platform Engineer"
            elif stratum.name == "founding":
                title = "Founding Engineer"
            else:  # pragma: no cover - protects fixture drift
                raise AssertionError(stratum.name)
            if index < 4:
                title = f"Staff {title}"
            rows.append(
                {
                    "id": next_id,
                    "job_title": title,
                    "date_posted": "2026-08-02",
                    "seniority": "staff" if index < 4 else "senior",
                    "technology_slugs": ["python", "aws"],
                    "company_object": {"id": f"company-{next_id}"},
                }
            )
            next_id += 1
        previews[stratum.name] = rows
    # One duplicate should not consume another slot.
    previews["software"].append(previews["backend"][0])

    selection = select_preview_jobs(
        previews,
        credit_budget=70,
        reserve_size=10,
        excluded_job_ids=[1],
    )

    assert len(selection.selected_job_ids) == 70
    assert len(set(selection.selected_job_ids)) == 70
    assert 1 not in selection.selected_job_ids
    assert len(selection.reserve_job_ids) == 10
    selected_rows = {
        row["id"]: row for rows in previews.values() for row in rows
    }
    staff_selected = sum(
        selected_rows[job_id]["seniority"] == "staff"
        for job_id in selection.selected_job_ids
    )
    assert staff_selected <= int(70 * 0.15)


def test_preview_selection_keeps_provider_leveled_generic_roles_but_rejects_bad_reveals() -> None:
    generic = {
        "id": 100,
        "job_title": "Software Engineer",
        "seniority": "mid_level",
        "company_object": {"id": "company-100"},
    }
    missing_company = {**generic, "id": 101, "company_object": {}}
    leadership = {
        **generic,
        "id": 102,
        "job_title": "Lead Software Engineer",
        "company_object": {"id": "company-102"},
    }

    selection = select_preview_jobs(
        {"software": [generic, missing_company, leadership]},
        credit_budget=1,
        reserve_size=2,
    )

    assert selection.selected_job_ids == (100,)
    assert selection.reserve_job_ids == ()


def test_preview_selection_rejects_mixed_employment_freelance_and_manager_rows() -> None:
    base = {
        "seniority": "senior",
        "employment_statuses": ["full_time"],
    }
    previews = {
        "software": [
            {
                **base,
                "id": 201,
                "job_title": "Senior Software Engineer",
                "employment_statuses": ["full_time", "temporary"],
                "company_object": {"id": "company-201"},
            },
            {
                **base,
                "id": 202,
                "job_title": "Backend Engineer (Freelance)",
                "company_object": {"id": "company-202"},
            },
            {
                **base,
                "id": 203,
                "job_title": "Manager Data Engineer - Manager",
                "company_object": {"id": "company-203"},
            },
            {
                **base,
                "id": 204,
                "job_title": "Senior Backend Engineer",
                "remote": True,
                "hybrid": True,
                "company_object": {"id": "company-204"},
            },
        ]
    }

    selection = select_preview_jobs(previews, credit_budget=1, reserve_size=3)

    assert selection.selected_job_ids == ()
    assert selection.reserve_job_ids == ()


def test_normalizer_preserves_vendor_provenance_and_emits_conservative_remote_evidence() -> None:
    first, second = fixture_jobs()
    normalized = normalize_theirstack_job(first)
    reordered = normalize_theirstack_job(dict(reversed(list(first.items()))))

    assert normalized.external_job_id == "7001"
    assert normalized.posting_url == "https://job-boards.greenhouse.io/acmecloud/jobs/7001"
    assert normalized.apply_url == "https://acme.example/careers/7001"
    assert normalized.source_published_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert normalized.structured_evidence["vendor_identity"] == {
        "job_id": "7001",
        "company_id": "ts-acme",
    }
    assert normalized.raw_payload["company_object"]["id"] == "ts-acme"
    assert normalized.content_hash == reordered.content_hash
    remote = classify_remote_eligibility(
        {
            "title": normalized.title,
            "location": normalized.location,
            "description_text": normalized.description_text,
            "structured_evidence": normalized.structured_evidence,
        }
    )
    assert remote.status == "global_explicit"

    restricted = normalize_theirstack_job(second)
    restricted_remote = classify_remote_eligibility(
        {
            "title": restricted.title,
            "location": restricted.location,
            "description_text": restricted.description_text,
            "structured_evidence": restricted.structured_evidence,
        }
    )
    assert restricted_remote.status == "remote_unclear"
    assert "structured posting countries: United Kingdom" in restricted_remote.evidence


def test_normalizer_rejects_closed_paid_rows() -> None:
    closed = {**fixture_jobs()[0], "closed_at": "2026-08-03T00:00:00Z"}

    with pytest.raises(ValueError, match="is closed"):
        normalize_theirstack_job(closed)


def test_import_normalizes_before_creating_company_identity(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    closed = {**fixture_jobs()[0], "closed_at": "2026-08-03T00:00:00Z"}

    result = import_theirstack_jobs(
        engine,
        [closed],
        plan_id="a" * 64,
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert result.jobs_imported == 0
    assert result.jobs_rejected == 1
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(companies_table)) == 0
        assert connection.scalar(select(func.count()).select_from(company_sources_table)) == 0
    engine.dispose()


def test_company_resolution_uses_domain_then_replays_vendor_identity(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    first = fixture_jobs()[0]
    now = datetime(2026, 8, 3, tzinfo=UTC)

    created = resolve_theirstack_company(engine, first, now=now)
    replay = resolve_theirstack_company(engine, first, now=now)

    assert created.company_created is True
    assert created.identity_state == "verified"
    assert replay.company_id == created.company_id
    assert replay.company_source_id == created.company_source_id
    assert replay.matched_by == "theirstack_external_id"
    with engine.connect() as connection:
        source = connection.execute(
            select(company_sources_table).where(
                company_sources_table.c.id == created.company_source_id
            )
        ).mappings().one()
    assert source["provider"] == THEIRSTACK_PROVIDER
    assert source["sync_mode"] == "observation"
    assert source["source_kind"] == "job_aggregator"
    engine.dispose()


def test_import_is_idempotent_observation_sync_and_stages_canonical_boards(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    jobs = fixture_jobs()
    observed_at = datetime(2026, 8, 3, 12, tzinfo=UTC)
    scope = {
        "schema_version": 1,
        "importer_version": "1",
        "credit_budget": 2,
        "excluded_job_ids": [],
        "selected_job_ids": [7001, 7002],
        "reserve_job_ids": [],
        "strata": [],
    }
    plan_id = plan_digest(scope)

    first = import_theirstack_jobs(
        engine,
        jobs,
        plan_id=plan_id,
        now=observed_at,
    )
    replay = import_theirstack_jobs(
        engine,
        jobs,
        plan_id=plan_id,
        now=observed_at,
    )

    assert first.jobs_imported == 2
    assert first.companies_resolved == 2
    assert first.companies_created == 2
    assert first.provisional_companies == 1
    assert first.staging_observations == 2
    assert first.staging_work_items == 2
    assert replay.jobs_imported == 2
    assert replay.staging_observations == 0
    assert replay.staging_work_items == 0
    inventory = JobRepository(engine).list_jobs(provider=THEIRSTACK_PROVIDER)
    assert {row["external_job_id"] for row in inventory} == {"7001", "7002"}
    assert all(row["lifecycle_managed"] is False for row in inventory)
    assert all(row["status_confidence"] == "observation" for row in inventory)

    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(companies_table)
        ) == 2
        assert connection.scalar(
            select(func.count()).select_from(company_sources_table).where(
                company_sources_table.c.provider == THEIRSTACK_PROVIDER
            )
        ) == 2
        assert connection.scalar(
            select(func.count()).select_from(jobs_table)
        ) == 2
        assert connection.scalar(
            select(func.count()).select_from(sync_runs_table)
        ) == 2
        observations = list(
            connection.execute(
                select(ingest_raw_observations_table).order_by(
                    ingest_raw_observations_table.c.observation_key
                )
            ).mappings()
        )
        work_urls = set(
            connection.scalars(select(ingest_url_work_items_table.c.normalized_url))
        )
    assert len(observations) == 2
    assert observations[0]["payload"]["provider"] == THEIRSTACK_PROVIDER
    assert work_urls == {
        "https://job-boards.greenhouse.io/acmecloud",
        "https://jobs.ashbyhq.com/unknownlabs",
    }
    engine.dispose()


def test_plan_digest_ignores_runtime_progress_but_binds_selected_ids() -> None:
    scope = {
        "schema_version": 1,
        "importer_version": "1",
        "credit_budget": 2,
        "excluded_job_ids": [],
        "selected_job_ids": [1, 2],
        "reserve_job_ids": [3],
        "strata": [],
        "state": "previewed",
    }
    digest = plan_digest(scope)
    assert digest == plan_digest({**scope, "state": "applied", "updated_at": "later"})
    assert digest != plan_digest({**scope, "selected_job_ids": [1, 4]})
