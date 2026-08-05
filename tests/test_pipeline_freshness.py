from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yc_radar.services.pipeline_freshness import (
    MONITORED_SOURCE_KIND,
    PipelineFreshnessSnapshot,
    ProviderFreshness,
    assess_pipeline_freshness,
)


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_pipeline_freshness.py"
SPEC = importlib.util.spec_from_file_location("check_pipeline_freshness", SCRIPT_PATH)
assert SPEC and SPEC.loader
check_pipeline_freshness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_pipeline_freshness)


def test_freshness_alarm_tracks_recurring_ats_boards_not_directory_snapshots() -> None:
    assert MONITORED_SOURCE_KIND == "ats_board"


def snapshot(*, latest_success: datetime | None, source_count: int = 3) -> PipelineFreshnessSnapshot:
    return PipelineFreshnessSnapshot(
        active_complete_source_count=source_count,
        active_job_count=42,
        running_sync_count=1,
        latest_attempt_at=datetime(2026, 8, 5, 11, 45, tzinfo=UTC),
        latest_successful_complete_sync_at=latest_success,
        latest_job_seen_at=datetime(2026, 8, 5, 11, 30, tzinfo=UTC),
        providers=(
            ProviderFreshness(
                provider="greenhouse",
                active_complete_source_count=source_count,
                latest_attempt_at=datetime(2026, 8, 5, 11, 45, tzinfo=UTC),
                latest_successful_complete_sync_at=latest_success,
            ),
        ),
    )


def test_complete_sync_at_freshness_boundary_is_healthy() -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)

    report = assess_pipeline_freshness(
        snapshot(latest_success=now - timedelta(hours=24)),
        now=now,
        max_age=timedelta(hours=24),
    )

    assert report["status"] == "healthy"
    assert report["successful_complete_sync_age_seconds"] == 86_400
    assert report["providers"][0]["fresh"] is True
    assert report["failures"] == []


def test_old_complete_sync_fails_even_when_a_run_is_currently_running() -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)

    report = assess_pipeline_freshness(
        snapshot(latest_success=now - timedelta(hours=24, seconds=1)),
        now=now,
        max_age=timedelta(hours=24),
    )

    assert report["status"] == "stale"
    assert report["running_sync_count"] == 1
    assert [failure["code"] for failure in report["failures"]] == [
        "successful_complete_sync_stale",
        "provider_successful_complete_sync_stale",
    ]
    assert report["failures"][1]["provider"] == "greenhouse"


def test_observation_only_database_cannot_satisfy_complete_sync_alarm() -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)

    report = assess_pipeline_freshness(
        snapshot(latest_success=None, source_count=0),
        now=now,
        max_age=timedelta(hours=24),
    )

    assert report["status"] == "stale"
    assert [failure["code"] for failure in report["failures"]] == [
        "no_active_complete_sources",
        "no_successful_complete_sync",
    ]


def test_naive_database_timestamps_are_treated_as_utc() -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)

    report = assess_pipeline_freshness(
        snapshot(latest_success=datetime(2026, 8, 5, 11)),
        now=now,
        max_age=timedelta(hours=24),
    )

    assert report["status"] == "healthy"
    assert report["latest_successful_complete_sync_at"] == "2026-08-05T11:00:00+00:00"


def test_one_fresh_provider_cannot_mask_another_stale_provider() -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    report = assess_pipeline_freshness(
        PipelineFreshnessSnapshot(
            active_complete_source_count=5,
            active_job_count=100,
            running_sync_count=0,
            latest_attempt_at=now - timedelta(minutes=10),
            latest_successful_complete_sync_at=now - timedelta(minutes=10),
            latest_job_seen_at=now - timedelta(minutes=10),
            providers=(
                ProviderFreshness(
                    provider="greenhouse",
                    active_complete_source_count=3,
                    latest_attempt_at=now - timedelta(minutes=10),
                    latest_successful_complete_sync_at=now - timedelta(minutes=10),
                ),
                ProviderFreshness(
                    provider="ashby",
                    active_complete_source_count=2,
                    latest_attempt_at=now - timedelta(days=2),
                    latest_successful_complete_sync_at=now - timedelta(days=2),
                ),
            ),
        ),
        now=now,
        max_age=timedelta(hours=24),
    )

    assert report["status"] == "stale"
    assert report["successful_complete_sync_age_seconds"] == 600
    assert report["failures"] == [
        {
            "code": "provider_successful_complete_sync_stale",
            "provider": "ashby",
            "message": "Provider 'ashby' has no successful complete sync within 24 hours.",
        }
    ]


def test_provider_with_sources_but_no_complete_success_is_stale() -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    report = assess_pipeline_freshness(
        PipelineFreshnessSnapshot(
            active_complete_source_count=2,
            active_job_count=10,
            running_sync_count=0,
            latest_attempt_at=now - timedelta(minutes=5),
            latest_successful_complete_sync_at=now - timedelta(minutes=5),
            latest_job_seen_at=now - timedelta(minutes=5),
            providers=(
                ProviderFreshness(
                    provider="greenhouse",
                    active_complete_source_count=1,
                    latest_attempt_at=now - timedelta(minutes=5),
                    latest_successful_complete_sync_at=now - timedelta(minutes=5),
                ),
                ProviderFreshness(
                    provider="ashby",
                    active_complete_source_count=1,
                    latest_attempt_at=now - timedelta(minutes=5),
                    latest_successful_complete_sync_at=None,
                ),
            ),
        ),
        now=now,
        max_age=timedelta(hours=24),
    )

    assert report["status"] == "stale"
    assert report["failures"] == [
        {
            "code": "provider_no_successful_complete_sync",
            "provider": "ashby",
            "message": (
                "Provider 'ashby' has active complete-snapshot sources but no successful "
                "complete sync."
            ),
        }
    ]


@pytest.mark.parametrize(("status", "expected_exit"), [("healthy", 0), ("stale", 1)])
def test_cli_exit_code_tracks_freshness_status(
    status: str,
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(check_pipeline_freshness, "engine_from_url", lambda: engine)
    monkeypatch.setattr(
        check_pipeline_freshness,
        "inspect_pipeline_freshness",
        lambda _engine, max_age: {
            "schema_version": 1,
            "status": status,
            "max_age_seconds": max_age.total_seconds(),
            "failures": [],
        },
    )

    assert check_pipeline_freshness.main([]) == expected_exit
    assert engine.disposed is True
