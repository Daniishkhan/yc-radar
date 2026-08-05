from __future__ import annotations

import argparse
import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select, update

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot
from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    engine_from_url,
    jobs_table,
    sync_runs_table,
)
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_source_registry import JobSourceProviderRegistry
from yc_radar.services.job_sync_service import JobSyncService, RunKeyReuseError

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_job_sources.py"
SPEC = importlib.util.spec_from_file_location("sync_job_sources", SCRIPT_PATH)
assert SPEC and SPEC.loader
sync_job_sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_job_sources)


def add_company(engine, *, name: str, slug: str, now: datetime) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                insert(companies_table)
                .values(
                    name=name,
                    normalized_name=name.lower(),
                    slug=slug,
                    website=f"https://{slug}.example",
                    primary_domain=f"{slug}.example",
                    identity_state="verified",
                    metadata={},
                    created_at=now,
                    updated_at=now,
                )
                .returning(companies_table.c.id)
            ).scalar_one()
        )


def add_source(
    repository: JobRepository,
    *,
    company_id: int,
    provider: str = "greenhouse",
    external_id: str = "acme",
    sync_mode: str = "complete_snapshot",
    now: datetime,
) -> dict:
    source, allowed, created = repository.register_source(
        company_id=company_id,
        provider=provider,
        source_kind="ats_board",
        external_id=external_id,
        source_url=(
            f"https://job-boards.greenhouse.io/{external_id}"
            if provider == "greenhouse"
            else f"https://jobs.ashbyhq.com/{external_id}"
        ),
        sync_mode=sync_mode,
        now=now,
    )
    assert allowed is True
    assert created is True
    return source


def normalized_job(job_id: str, *, description: str = "Build API systems") -> NormalizedJob:
    return NormalizedJob(
        external_job_id=job_id,
        title="Senior Backend Engineer",
        posting_url=f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
        apply_url=f"https://job-boards.greenhouse.io/acme/jobs/{job_id}#apply",
        location="Remote",
        department="Engineering",
        employment_type="Full-time",
        description_html=f"<p>{description}</p>",
        description_text=description,
        content_hash=f"hash:{job_id}:{description}",
        structured_evidence={"locations": ["Remote"]},
        raw_payload={"id": job_id, "content": description},
    )


def snapshot(*jobs: NormalizedJob, external_id: str = "acme") -> SourceSnapshot:
    return SourceSnapshot(
        provider="greenhouse",
        external_source_id=external_id,
        adapter_version="test",
        is_complete=True,
        jobs=list(jobs),
        http_status=200,
        request_metadata={"url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs"},
    )


def test_complete_snapshots_own_one_current_job_lifecycle(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    company_id = add_company(engine, name="Acme", slug="acme", now=now)
    repository = JobRepository(engine)
    source = add_source(repository, company_id=company_id, now=now)
    service = JobSyncService(engine, clock=lambda: now)
    job_one = normalized_job("1")
    job_two = normalized_job("2")

    first = service.sync_snapshot(
        company_source_id=source["id"],
        run_key="one",
        snapshot=snapshot(job_one, job_two),
    )
    assert (first.jobs_added, first.jobs_updated, first.jobs_unchanged) == (2, 0, 0)

    now += timedelta(minutes=1)
    second = service.sync_snapshot(
        company_source_id=source["id"],
        run_key="two",
        snapshot=snapshot(job_one, job_two),
    )
    assert (second.jobs_added, second.jobs_updated, second.jobs_unchanged) == (0, 0, 2)

    now += timedelta(minutes=1)
    changed_one = normalized_job("1", description="Build distributed API systems")
    changed = service.sync_snapshot(
        company_source_id=source["id"],
        run_key="changed",
        snapshot=snapshot(changed_one, job_two),
    )
    assert (changed.jobs_updated, changed.jobs_unchanged) == (1, 1)

    now += timedelta(minutes=1)
    first_miss = service.sync_snapshot(
        company_source_id=source["id"],
        run_key="first-miss",
        snapshot=snapshot(changed_one),
    )
    assert (first_miss.jobs_missed, first_miss.jobs_closed) == (1, 0)

    now += timedelta(minutes=1)
    failed = service.sync_snapshot(
        company_source_id=source["id"],
        run_key="failed",
        snapshot=SourceSnapshot(
            provider="greenhouse",
            external_source_id="acme",
            adapter_version="test",
            is_complete=False,
            http_status=503,
            errors=[{"kind": "http_status", "message": "503"}],
        ),
    )
    assert failed.status == "failed"

    with engine.connect() as connection:
        still_active = (
            connection.execute(select(jobs_table).where(jobs_table.c.external_job_id == "2"))
            .mappings()
            .one()
        )
    assert still_active["status"] == "active"
    assert still_active["consecutive_complete_misses"] == 1

    now += timedelta(minutes=1)
    second_miss = service.sync_snapshot(
        company_source_id=source["id"],
        run_key="second-miss",
        snapshot=snapshot(changed_one),
    )
    assert (second_miss.jobs_missed, second_miss.jobs_closed) == (1, 1)

    now += timedelta(minutes=1)
    reappeared = service.sync_snapshot(
        company_source_id=source["id"],
        run_key="reappeared",
        snapshot=snapshot(changed_one, job_two),
    )
    assert reappeared.jobs_reactivated == 1

    with engine.connect() as connection:
        current = (
            connection.execute(select(jobs_table).where(jobs_table.c.external_job_id == "2"))
            .mappings()
            .one()
        )
        assert connection.scalar(select(func.count()).select_from(jobs_table)) == 2
        assert connection.scalar(select(func.count()).select_from(sync_runs_table)) == 7
    assert current["status"] == "active"
    assert current["consecutive_complete_misses"] == 0
    assert current["closed_at"] is None

    inventory = repository.list_jobs()
    assert len(inventory) == 2
    assert inventory[0]["company_id"] == company_id
    assert inventory[0]["company_source_id"] == source["id"]
    assert inventory[0]["provider"] == "greenhouse"
    assert inventory[0]["source_kind"] == "ats_board"
    assert inventory[0]["source_url"] == "https://job-boards.greenhouse.io/acme"
    assert inventory[0]["lifecycle_managed"] is True
    assert inventory[0]["source_sync_status"] == "completed"


def test_incomplete_or_invalid_snapshot_never_mutates_jobs(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    company_id = add_company(engine, name="Acme", slug="acme", now=now)
    source = add_source(JobRepository(engine), company_id=company_id, now=now)
    service = JobSyncService(engine, clock=lambda: now)

    incomplete = service.sync_snapshot(
        company_source_id=source["id"],
        run_key="incomplete",
        snapshot=SourceSnapshot(
            provider="greenhouse",
            external_source_id="acme",
            adapter_version="test",
            is_complete=False,
            jobs=[normalized_job("1")],
            http_status=200,
        ),
    )
    assert incomplete.status == "partial"

    duplicate = normalized_job("2")
    invalid = service.sync_snapshot(
        company_source_id=source["id"],
        run_key="duplicate",
        snapshot=snapshot(duplicate, duplicate),
    )
    assert invalid.status == "partial"
    assert invalid.errors_count == 1

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(jobs_table)) == 0
        rows = list(
            connection.execute(select(sync_runs_table).order_by(sync_runs_table.c.id)).mappings()
        )
    assert [row["status"] for row in rows] == ["partial", "partial"]
    assert [row["is_complete"] for row in rows] == [False, False]


def test_observation_sync_upserts_seen_jobs_without_applying_absence_lifecycle(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    company_id = add_company(engine, name="Observed Co", slug="observed", now=now)
    repository = JobRepository(engine)
    source = add_source(
        repository,
        company_id=company_id,
        external_id="observed",
        sync_mode="observation",
        now=now,
    )
    service = JobSyncService(engine, clock=lambda: now)
    job_one = normalized_job("1")
    job_two = normalized_job("2")

    first = service.sync_observations(
        company_source_id=source["id"],
        run_key="observation-one",
        jobs=[job_one, job_two],
        adapter_version="parser-v1",
    )
    assert first.status == "completed"
    assert first.is_complete_scan is False
    assert (first.jobs_added, first.jobs_missed, first.jobs_closed) == (2, 0, 0)

    now += timedelta(minutes=1)
    with engine.begin() as connection:
        connection.execute(
            update(jobs_table)
            .where(jobs_table.c.external_job_id == "1")
            .values(
                status="closed",
                consecutive_complete_misses=2,
                closed_at=now,
            )
        )
        connection.execute(
            update(jobs_table)
            .where(jobs_table.c.external_job_id == "2")
            .values(consecutive_complete_misses=1)
        )

    now += timedelta(minutes=1)
    changed_one = normalized_job("1", description="Build changed API systems")
    second = service.sync_observations(
        company_source_id=source["id"],
        run_key="observation-two",
        jobs=[changed_one, normalized_job("3")],
        adapter_version="parser-v2",
    )
    assert second.is_complete_scan is False
    assert (
        second.jobs_added,
        second.jobs_updated,
        second.jobs_reactivated,
        second.jobs_missed,
        second.jobs_closed,
    ) == (1, 1, 1, 0, 0)

    now += timedelta(minutes=1)
    replay = service.sync_observations(
        company_source_id=source["id"],
        run_key="observation-two",
        jobs=[normalized_job("never-inserted")],
        adapter_version="ignored-on-replay",
    )

    with engine.connect() as connection:
        rows = {
            row["external_job_id"]: row
            for row in connection.execute(select(jobs_table)).mappings()
        }
        runs = list(
            connection.execute(select(sync_runs_table).order_by(sync_runs_table.c.id)).mappings()
        )
    assert replay.idempotent_replay is True
    assert replay.run_id == second.run_id
    assert set(rows) == {"1", "2", "3"}
    assert rows["1"]["status"] == "active"
    assert rows["1"]["consecutive_complete_misses"] == 0
    assert rows["1"]["closed_at"] is None
    assert rows["2"]["status"] == "active"
    assert rows["2"]["consecutive_complete_misses"] == 1
    assert len(runs) == 2
    assert runs[1]["status"] == "completed"
    assert runs[1]["is_complete"] is False
    assert runs[1]["details"] == {
        "provider": "greenhouse",
        "external_id": "observed",
        "adapter_version": "parser-v2",
        "sync_mode": "observation",
        "errors": [],
    }
    assert runs[1]["stats"]["jobs_missed"] == 0
    assert runs[1]["stats"]["jobs_closed"] == 0
    inventory = repository.list_jobs()
    assert all(job["lifecycle_managed"] is False for job in inventory)
    assert all(job["status_confidence"] == "observation" for job in inventory)


def test_list_jobs_filters_only_stale_observation_rows(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime.now(UTC)
    company_id = add_company(engine, name="Freshness Co", slug="freshness", now=now)
    repository = JobRepository(engine)
    managed_source = add_source(
        repository,
        company_id=company_id,
        external_id="managed-freshness",
        now=now,
    )
    observation_source = add_source(
        repository,
        company_id=company_id,
        provider="ashby",
        external_id="observed-freshness",
        sync_mode="observation",
        now=now,
    )

    managed_seen_at = now - timedelta(days=90)
    JobSyncService(engine, clock=lambda: managed_seen_at).sync_snapshot(
        company_source_id=managed_source["id"],
        run_key="managed-old",
        snapshot=snapshot(
            normalized_job("managed-old"),
            external_id="managed-freshness",
        ),
    )
    stale_seen_at = now - timedelta(days=46)
    JobSyncService(engine, clock=lambda: stale_seen_at).sync_observations(
        company_source_id=observation_source["id"],
        run_key="observed-stale",
        jobs=[normalized_job("observed-stale")],
    )
    fresh_seen_at = now - timedelta(days=44)
    JobSyncService(engine, clock=lambda: fresh_seen_at).sync_observations(
        company_source_id=observation_source["id"],
        run_key="observed-fresh",
        jobs=[normalized_job("observed-fresh")],
    )

    all_ids = {
        job["external_job_id"]
        for job in repository.list_jobs(observation_max_age_days=None)
    }
    fresh_ids = {
        job["external_job_id"]
        for job in repository.list_jobs(observation_max_age_days=45)
    }

    assert all_ids == {"managed-old", "observed-stale", "observed-fresh"}
    assert fresh_ids == {"managed-old", "observed-fresh"}
    with pytest.raises(ValueError, match="must be non-negative or None"):
        repository.list_jobs(observation_max_age_days=-1)


def test_observation_sync_rejects_non_observation_sources(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    company_id = add_company(engine, name="Acme", slug="acme", now=now)
    source = add_source(JobRepository(engine), company_id=company_id, now=now)

    with pytest.raises(ValueError, match="does not support observation sync"):
        JobSyncService(engine, clock=lambda: now).sync_observations(
            company_source_id=source["id"],
            run_key="wrong-mode",
            jobs=[normalized_job("1")],
        )

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(sync_runs_table)) == 0
        assert connection.scalar(select(func.count()).select_from(jobs_table)) == 0


@pytest.mark.parametrize("field_name", ["external_job_id", "title", "content_hash"])
def test_observation_sync_rejects_blank_canonical_job_fields_without_writes(
    postgres_database_url: str,
    field_name: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    company_id = add_company(engine, name="Observed Co", slug="observed", now=now)
    source = add_source(
        JobRepository(engine),
        company_id=company_id,
        external_id="observed",
        sync_mode="observation",
        now=now,
    )
    invalid_job = normalized_job("1").model_copy(update={field_name: " \t "})

    with pytest.raises(ValueError, match=rf"blank {field_name}$"):
        JobSyncService(engine, clock=lambda: now).sync_observations(
            company_source_id=source["id"],
            run_key=f"blank-{field_name}",
            jobs=[invalid_job],
        )

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(sync_runs_table)) == 0
        assert connection.scalar(select(func.count()).select_from(jobs_table)) == 0


def test_run_keys_are_durable_and_completed_runs_replay(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    company_id = add_company(engine, name="Acme", slug="acme", now=now)
    source = add_source(JobRepository(engine), company_id=company_id, now=now)
    service = JobSyncService(engine, clock=lambda: now)

    started = service.start_run(
        company_source_id=source["id"],
        run_key="durable",
        provider="greenhouse",
        adapter_version="test",
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(sync_runs_table.c.status).where(sync_runs_table.c.id == started.run_id)
            )
            == "running"
        )

    completed = service.apply_snapshot(started=started, snapshot=snapshot(normalized_job("1")))
    replay = service.sync_snapshot(
        company_source_id=source["id"],
        run_key="durable",
        snapshot=snapshot(normalized_job("1")),
    )
    assert completed.status == "completed"
    assert replay.idempotent_replay is True
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(sync_runs_table)) == 1

    failed_started = service.start_run(
        company_source_id=source["id"],
        run_key="interrupted",
        provider="greenhouse",
        adapter_version="test",
    )
    assert not isinstance(failed_started, type(completed))
    interrupted = service.interrupt_running_run(
        company_source_id=source["id"],
        run_key="interrupted",
    )
    assert interrupted is not None
    assert interrupted.status == "failed"
    with pytest.raises(RunKeyReuseError, match="Use a new run key"):
        service.start_run(
            company_source_id=source["id"],
            run_key="interrupted",
            provider="greenhouse",
            adapter_version="test",
        )


def test_sync_script_dispatches_all_providers_sequentially(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    repository = JobRepository(engine)
    first_company = add_company(engine, name="One", slug="one", now=now)
    second_company = add_company(engine, name="Two", slug="two", now=now)
    add_source(
        repository,
        company_id=first_company,
        provider="greenhouse",
        external_id="one",
        now=now,
    )
    add_source(
        repository,
        company_id=second_company,
        provider="ashby",
        external_id="two",
        now=now,
    )
    repository.register_source(
        company_id=first_company,
        provider="yc",
        source_kind="directory",
        external_id="123",
        source_url="https://www.ycombinator.com/companies/one",
        sync_mode="complete_snapshot",
        now=now,
    )

    class RecordingAdapter:
        adapter_version = "test"
        source_kind = "ats_board"

        def __init__(self, provider: str) -> None:
            self.provider = provider
            self.calls: list[str] = []

        def extract_source_id(self, url: str) -> str | None:
            del url
            return None

        def canonical_source_url(self, external_source_id: str) -> str:
            return external_source_id

        async def fetch_snapshot(self, external_source_id: str) -> SourceSnapshot:
            self.calls.append(external_source_id)
            return SourceSnapshot(
                provider=self.provider,
                external_source_id=external_source_id,
                adapter_version=self.adapter_version,
                is_complete=True,
                http_status=200,
            )

    greenhouse = RecordingAdapter("greenhouse")
    ashby = RecordingAdapter("ashby")
    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    results = asyncio.run(
        sync_job_sources.sync_sources(
            engine,
            argparse.Namespace(
                provider=None,
                company_id=None,
                source_ids=None,
                limit=None,
                run_key="all",
                delay_seconds=2.5,
            ),
            providers=JobSourceProviderRegistry([greenhouse, ashby]),
            sleeper=sleeper,
        )
    )

    assert greenhouse.calls == ["one"]
    assert ashby.calls == ["two"]
    assert sleeps == [2.5]
    assert [result.status for result in results] == ["completed", "completed"]


def test_disabled_and_non_job_sources_are_not_selected(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    repository = JobRepository(engine)
    company_id = add_company(engine, name="Acme", slug="acme", now=now)
    active = add_source(repository, company_id=company_id, external_id="active", now=now)
    disabled = add_source(repository, company_id=company_id, external_id="disabled", now=now)
    with engine.begin() as connection:
        connection.execute(
            update(company_sources_table)
            .where(company_sources_table.c.id == disabled["id"])
            .values(status="disabled")
        )
    repository.register_source(
        company_id=company_id,
        provider="yc",
        source_kind="directory",
        external_id="yc-acme",
        source_url="https://www.ycombinator.com/companies/acme",
        sync_mode="none",
        now=now,
    )

    assert [source["id"] for source in repository.active_sources()] == [active["id"]]
