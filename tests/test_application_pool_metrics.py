from __future__ import annotations

from datetime import UTC, datetime

from yc_radar.services.application_pool_metrics import build_application_pool_metrics


def test_metrics_report_queue_provider_coverage_and_dead_links() -> None:
    queues = {
        "jobs_to_apply": [
            {
                "job_key": "greenhouse:1",
                "company_slug": "acme",
                "provider": "greenhouse",
                "role_match_status": "strong",
                "role_family": "backend",
                "remote_eligibility": "global_explicit",
                "freshness_bucket": "0_14_days",
                "application_url": "https://acme.example/jobs/1/apply",
            },
            {
                "job_key": "theirstack:2",
                "company_slug": "beta",
                "provider": "theirstack",
                "role_match_status": "strong",
                "role_family": "full_stack",
                "remote_eligibility": "global_explicit",
                "posting_url": "https://beta.example/jobs/2",
            },
        ],
        "verification_queue": [
            {
                "job_key": "greenhouse:3",
                "company_slug": "acme",
                "provider": "greenhouse",
                "role_match_status": "strong",
                "remote_eligibility_status": "remote_unclear",
            }
        ],
    }
    validations = [
        {
            "queue": "application_queue",
            "input_index": 0,
            "provider": "greenhouse",
            "outcome": "live",
        },
        {
            "queue": "jobs_to_apply",
            "input_index": 1,
            "provider": "theirstack",
            "outcome": "dead",
        },
        {
            "queue": "verification_queue",
            "input_index": 0,
            "provider": "greenhouse",
            "outcome": "invalid",
        },
    ]

    report = build_application_pool_metrics(
        queues,
        validations=validations,
        generated_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
    )

    assert report["selected_row_count"] == 3
    assert report["queues"]["application_queue"] == {
        "row_count": 2,
        "company_count": 2,
        "provider_distribution": {"greenhouse": 1, "theirstack": 1},
        "role_match_distribution": {"strong": 2},
        "role_family_distribution": {"backend": 1, "full_stack": 1},
        "remote_eligibility_distribution": {"global_explicit": 2},
        "freshness_distribution": {"0_14_days": 1, "unknown": 1},
        "direct_application_url_count": 1,
        "selected_url_count": 2,
        "missing_url_count": 0,
        "direct_application_url_coverage": 0.5,
        "selected_url_coverage": 1.0,
        "application_status_distribution": {},
        "url_validation": {
            "validation_row_count": 2,
            "outcome_distribution": {"dead": 1, "live": 1},
            "live_link_count": 1,
            "dead_link_count": 1,
            "blocked_link_count": 0,
            "transient_error_count": 0,
            "invalid_link_count": 0,
            "dead_link_rate_denominator": 2,
            "dead_link_rate": 0.5,
            "expected_queue_row_count": 2,
            "validation_coverage": 1.0,
            "unvalidated_row_count": 0,
        },
    }
    assert report["provider_contribution"]["greenhouse"]["selected_row_count"] == 2
    assert report["provider_contribution"]["greenhouse"]["by_queue"] == {
        "application_queue": 1,
        "verification_queue": 1,
    }
    assert report["provider_contribution"]["theirstack"]["url_validation"][
        "dead_link_count"
    ] == 1
    assert report["url_validation"]["dead_link_rate"] == 0.5


def test_empty_queue_metrics_are_explicit_instead_of_dividing_by_zero() -> None:
    report = build_application_pool_metrics(
        {"company_outreach": []},
        generated_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
    )

    queue = report["queues"]["company_outreach_queue"]
    assert queue["row_count"] == 0
    assert queue["direct_application_url_coverage"] is None
    assert queue["selected_url_coverage"] is None
    assert queue["url_validation"]["validation_coverage"] is None
    assert report["url_validation"]["dead_link_rate"] is None


def test_outreach_company_count_accepts_target_name_and_slug_fields() -> None:
    report = build_application_pool_metrics(
        {
            "company_outreach": [
                {"id": 30960, "name": "Metorial", "slug": "metorial"},
                {"id": 29425, "name": "Zep AI", "slug": "zep-ai"},
            ]
        },
        generated_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
    )

    assert report["queues"]["company_outreach_queue"]["company_count"] == 2
