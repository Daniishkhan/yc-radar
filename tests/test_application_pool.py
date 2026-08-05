from __future__ import annotations

from datetime import UTC, datetime

from yc_radar.services.application_pool import build_application_pool


AS_OF = datetime(2026, 8, 5, tzinfo=UTC)


def job(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "company_name": "Example",
        "title": "Senior Backend Engineer",
        "provider": "greenhouse",
        "status": "active",
        "role_match_status": "strong",
        "remote_eligibility_status": "global_explicit",
        "posting_url": "https://example.com/jobs/1",
        "apply_url": "https://example.com/jobs/1/apply",
        "source_published_at": "2026-08-01T00:00:00+00:00",
        "lifecycle_managed": True,
        "status_confidence": "complete_snapshot",
    }
    row.update(overrides)
    return row


def test_pool_splits_explicit_and_ambiguous_remote_jobs() -> None:
    explicit_observation = job(
        provider="theirstack",
        lifecycle_managed=False,
        status_confidence="observation",
    )
    ambiguous = job(
        title="Staff Platform Engineer",
        remote_eligibility_status="remote_unclear",
    )

    pool = build_application_pool(
        [ambiguous, explicit_observation],
        as_of=AS_OF,
        application_max_age_days=90,
    )

    assert [row["provider"] for row in pool["application_queue"]] == ["theirstack"]
    assert [row["title"] for row in pool["verification_queue"]] == [
        "Staff Platform Engineer"
    ]
    assert pool["summary"]["application_queue_count"] == 1
    assert pool["summary"]["verification_queue_count"] == 1
    assert pool["summary"]["provider_contribution"] == {
        "greenhouse": 1,
        "theirstack": 1,
    }


def test_managed_job_ranks_above_equivalent_observation_without_changing_lane() -> None:
    observation = job(
        company_name="Observation",
        provider="theirstack",
        lifecycle_managed=False,
        status_confidence="observation",
    )
    managed = job(company_name="Managed")

    pool = build_application_pool([observation, managed], as_of=AS_OF)

    assert [row["company_name"] for row in pool["application_queue"]] == [
        "Managed",
        "Observation",
    ]
    assert pool["application_queue"][0]["priority_score"] > (
        pool["application_queue"][1]["priority_score"]
    )


def test_stale_restricted_and_missing_url_jobs_are_excluded_with_reasons() -> None:
    rows = [
        job(
            company_name="Stale",
            source_published_at="2025-01-01T00:00:00+00:00",
        ),
        job(
            company_name="Restricted",
            remote_eligibility_status="restricted_remote",
        ),
        job(company_name="No URL", posting_url=None, apply_url=None),
    ]

    pool = build_application_pool(rows, as_of=AS_OF, application_max_age_days=90)

    assert not pool["application_queue"]
    reasons = pool["summary"]["exclusion_reason_distribution"]
    assert reasons["posting is older than the application freshness window"] == 1
    assert reasons["remote eligibility is explicitly restricted"] == 1
    assert reasons["no public posting or application URL is available"] == 1


def test_queue_limit_does_not_hide_exclusion_metrics() -> None:
    pool = build_application_pool(
        [job(company_name="A"), job(company_name="B")],
        as_of=AS_OF,
        limit_per_queue=1,
    )

    assert len(pool["application_queue"]) == 1
    assert pool["summary"]["inventory_count"] == 2
    assert pool["summary"]["application_candidate_count"] == 2
    assert pool["summary"]["application_queue_count"] == 1
