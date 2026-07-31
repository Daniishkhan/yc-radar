import argparse
import asyncio
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot
from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.database import (
    engine_from_url,
    career_sources_table,
    job_posting_observations_table,
    job_posting_versions_table,
    job_postings_table,
    source_sync_runs_table,
    upsert_yc_companies,
)
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_source_registry import (
    JobSourceProviderRegistry,
    JobSourceRegistry,
)
from yc_radar.services.job_sync_service import JobSyncService, RunKeyReuseError

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_job_sources.py"
SPEC = importlib.util.spec_from_file_location("sync_job_sources", SCRIPT_PATH)
assert SPEC and SPEC.loader
sync_job_sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_job_sources)
sync_sources = sync_job_sources.sync_sources


def normalized_job(job_id: str, *, description: str = "Build API systems") -> NormalizedJob:
    return NormalizedJob(
        external_job_id=job_id,
        title="Senior Backend Engineer",
        posting_url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        location="Remote",
        department="Engineering",
        description_html=f"<p>{description}</p>",
        description_text=description,
        content_hash=f"hash:{job_id}:{description}",
        raw_payload={"id": job_id, "content": description},
    )


def snapshot(*jobs: NormalizedJob) -> SourceSnapshot:
    return SourceSnapshot(
        provider="greenhouse",
        external_source_id="acme",
        adapter_version="test",
        is_complete=True,
        jobs=list(jobs),
        http_status=200,
        request_metadata={"url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs"},
    )


def test_complete_snapshot_lifecycle_is_safe_and_idempotent(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "Acme",
                "slug": "acme",
                "website": "https://acme.example",
                "regions": [],
                "industries": [],
                "tags": [],
            }
        ],
    )
    now = datetime(2026, 5, 1, tzinfo=UTC)
    repository = JobRepository(engine)
    source, allowed, created = repository.register_career_source(
        company_id=1,
        provider="greenhouse",
        source_kind="ats_board",
        external_source_id="acme",
        source_url="https://boards.greenhouse.io/acme",
        discovered_from_url="https://boards.greenhouse.io/acme",
        now=now,
    )
    assert allowed is True
    assert created is True
    service = JobSyncService(engine, clock=lambda: now)
    job_one = normalized_job("1")
    job_two = normalized_job("2")

    first = service.sync_snapshot(
        career_source_id=source["id"], run_key="one", snapshot=snapshot(job_one, job_two)
    )
    assert (first.jobs_added, first.jobs_updated, first.jobs_unchanged) == (2, 0, 0)

    now += timedelta(minutes=1)
    second = service.sync_snapshot(
        career_source_id=source["id"], run_key="two", snapshot=snapshot(job_one, job_two)
    )
    assert (second.jobs_added, second.jobs_updated, second.jobs_unchanged) == (0, 0, 2)

    now += timedelta(minutes=1)
    changed_one = normalized_job("1", description="Build distributed API systems")
    third = service.sync_snapshot(
        career_source_id=source["id"], run_key="three", snapshot=snapshot(changed_one, job_two)
    )
    assert (third.jobs_updated, third.jobs_unchanged) == (1, 1)

    now += timedelta(minutes=1)
    fourth = service.sync_snapshot(
        career_source_id=source["id"], run_key="four", snapshot=snapshot(changed_one)
    )
    assert fourth.jobs_missed == 1

    with engine.connect() as connection:
        second_job = connection.execute(
            select(job_postings_table).where(job_postings_table.c.external_job_id == "2")
        ).mappings().one()
    assert second_job["status"] == "active"
    assert second_job["consecutive_complete_misses"] == 1
    with engine.connect() as connection:
        last_successful_sync = connection.scalar(
            select(career_sources_table.c.last_synced_at).where(
                career_sources_table.c.id == source["id"]
            )
        )
    assert last_successful_sync == now

    now += timedelta(minutes=1)
    failed = service.sync_snapshot(
        career_source_id=source["id"],
        run_key="failed",
        snapshot=SourceSnapshot(
            provider="greenhouse",
            external_source_id="acme",
            adapter_version="test",
            is_complete=False,
            http_status=500,
            errors=[{"kind": "http_status", "message": "500"}],
        ),
    )
    assert failed.status == "failed"
    with engine.connect() as connection:
        after_failed = connection.execute(
            select(job_postings_table).where(job_postings_table.c.id == second_job["id"])
        ).mappings().one()
    assert after_failed["status"] == "active"
    assert after_failed["consecutive_complete_misses"] == 1
    with engine.connect() as connection:
        assert connection.scalar(
            select(career_sources_table.c.last_synced_at).where(
                career_sources_table.c.id == source["id"]
            )
        ) == last_successful_sync

    partial = service.sync_snapshot(
        career_source_id=source["id"],
        run_key="partial",
        snapshot=SourceSnapshot(
            provider="greenhouse",
            external_source_id="acme",
            adapter_version="test",
            is_complete=True,
            http_status=200,
            errors=[{"kind": "invalid_job", "message": "missing title"}],
        ),
    )
    assert partial.status == "partial"
    with engine.connect() as connection:
        after_partial = connection.execute(
            select(job_postings_table).where(job_postings_table.c.id == second_job["id"])
        ).mappings().one()
    assert after_partial["status"] == "active"
    assert after_partial["consecutive_complete_misses"] == 1
    with engine.connect() as connection:
        assert connection.scalar(
            select(career_sources_table.c.last_synced_at).where(
                career_sources_table.c.id == source["id"]
            )
        ) == last_successful_sync

    now += timedelta(minutes=1)
    sixth = service.sync_snapshot(
        career_source_id=source["id"], run_key="six", snapshot=snapshot(changed_one)
    )
    assert (sixth.jobs_missed, sixth.jobs_closed) == (1, 1)
    with engine.connect() as connection:
        closed_job = connection.execute(
            select(job_postings_table).where(job_postings_table.c.id == second_job["id"])
        ).mappings().one()
    assert closed_job["status"] == "closed"
    assert closed_job["consecutive_complete_misses"] == 2
    assert closed_job["closed_at"] == now

    now += timedelta(minutes=1)
    seventh = service.sync_snapshot(
        career_source_id=source["id"], run_key="seven", snapshot=snapshot(changed_one, job_two)
    )
    assert seventh.jobs_reactivated == 1
    with engine.connect() as connection:
        reactivated = connection.execute(
            select(job_postings_table).where(job_postings_table.c.id == second_job["id"])
        ).mappings().one()
        version_count = connection.scalar(select(func.count()).select_from(job_posting_versions_table))
        observation_count = connection.scalar(
            select(func.count()).select_from(job_posting_observations_table)
        )
    assert reactivated["id"] == second_job["id"]
    assert reactivated["status"] == "active"
    assert reactivated["closed_at"] is None
    assert reactivated["consecutive_complete_misses"] == 0
    assert version_count == 3
    assert observation_count == 12

    replay = service.sync_snapshot(
        career_source_id=source["id"], run_key="seven", snapshot=snapshot(changed_one, job_two)
    )
    assert replay.idempotent_replay is True
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_sync_runs_table)) == 8
        assert connection.scalar(select(func.count()).select_from(job_posting_versions_table)) == 3
        assert connection.scalar(select(func.count()).select_from(job_posting_observations_table)) == 12


def test_running_run_is_committed_before_snapshot_application(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "Acme",
                "slug": "acme",
                "website": "https://acme.example",
                "regions": [],
                "industries": [],
                "tags": [],
            }
        ],
    )
    now = datetime(2026, 5, 1, tzinfo=UTC)
    source, _, _ = JobRepository(engine).register_career_source(
        company_id=1,
        provider="greenhouse",
        source_kind="ats_board",
        external_source_id="acme",
        source_url="https://boards.greenhouse.io/acme",
        discovered_from_url="https://boards.greenhouse.io/acme",
        now=now,
    )
    service = JobSyncService(engine, clock=lambda: now)

    started = service.start_run(
        career_source_id=source["id"],
        run_key="durable-before-fetch",
        provider="greenhouse",
        adapter_version="test",
    )

    with engine.connect() as connection:
        run = connection.execute(
            select(source_sync_runs_table).where(source_sync_runs_table.c.id == started.run_id)
        ).mappings().one()
    assert run["status"] == "running"
    assert run["completed_at"] is None

    result = service.apply_snapshot(started=started, snapshot=snapshot(normalized_job("1")))

    assert result.status == "completed"
    with engine.connect() as connection:
        assert connection.scalar(
            select(source_sync_runs_table.c.status).where(source_sync_runs_table.c.id == started.run_id)
        ) == "completed"


def test_resumed_batch_marks_an_orphaned_running_attempt_failed(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    company = CompanyRegistry(engine).register_company(
        name="Interrupted Company", website="https://interrupted.example"
    )
    source = JobSourceRegistry(engine).register_url(
        company_id=company.company_id,
        source_url="https://job-boards.greenhouse.io/interrupted-company",
    )
    service = JobSyncService(engine)
    service.start_run(
        career_source_id=source.career_source_id,
        run_key="interrupted-batch:attempt-1",
        provider="greenhouse",
        adapter_version="test",
    )

    result = service.interrupt_running_run(
        career_source_id=source.career_source_id,
        run_key="interrupted-batch:attempt-1",
    )

    assert result is not None
    assert result.status == "failed"
    with engine.connect() as connection:
        row = connection.execute(
            select(source_sync_runs_table).where(
                source_sync_runs_table.c.run_key == "interrupted-batch:attempt-1"
            )
        ).mappings().one()
    assert row["errors"] == [
        {
            "kind": "interrupted",
            "message": "worker restarted before the source attempt completed",
        }
    ]


def test_failed_run_key_requires_a_new_attempt_key(postgres_database_url: str, capsys) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": 1,
                "name": "Acme",
                "slug": "acme",
                "website": "https://acme.example",
                "regions": [],
                "industries": [],
                "tags": [],
            }
        ],
    )
    now = datetime(2026, 5, 1, tzinfo=UTC)
    source, _, _ = JobRepository(engine).register_career_source(
        company_id=1,
        provider="greenhouse",
        source_kind="ats_board",
        external_source_id="acme",
        source_url="https://boards.greenhouse.io/acme",
        discovered_from_url="https://boards.greenhouse.io/acme",
        now=now,
    )
    service = JobSyncService(engine, clock=lambda: now)
    failed_run_key = f"retry-key:{source['id']}"
    failed = service.sync_snapshot(
        career_source_id=source["id"],
        run_key=failed_run_key,
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
    with pytest.raises(RunKeyReuseError, match="Use a new run key"):
        service.sync_snapshot(
            career_source_id=source["id"],
            run_key=failed_run_key,
            snapshot=snapshot(normalized_job("1")),
        )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(job_postings_table)) == 0

    class NeverFetchAdapter:
        provider = "greenhouse"
        adapter_version = "test"
        calls = 0

        async def fetch_snapshot(self, external_source_id: str) -> SourceSnapshot:
            del external_source_id
            self.calls += 1
            raise AssertionError("failed run key should be preflighted before fetching")

    adapter = NeverFetchAdapter()
    results = asyncio.run(
        sync_sources(
            engine,
            argparse.Namespace(provider="greenhouse", company_id=None, limit=None, run_key="retry-key"),
            adapter=adapter,
        )
    )

    assert adapter.calls == 0
    assert results[0].status == "failed"
    assert "skipping fetch. Use a new --run-key" in capsys.readouterr().out


def test_sync_cli_paces_source_fetches_sequentially(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    upsert_yc_companies(
        engine,
        [
            {
                "id": company_id,
                "name": f"Company {company_id}",
                "slug": f"company-{company_id}",
                "website": f"https://company-{company_id}.example",
                "regions": [],
                "industries": [],
                "tags": [],
            }
            for company_id in (1, 2)
        ],
    )
    repository = JobRepository(engine)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    for company_id, token in ((1, "one"), (2, "two")):
        _, allowed, _ = repository.register_career_source(
            company_id=company_id,
            provider="greenhouse",
            source_kind="ats_board",
            external_source_id=token,
            source_url=f"https://boards.greenhouse.io/{token}",
            discovered_from_url=f"https://boards.greenhouse.io/{token}",
            now=now,
        )
        assert allowed is True

    class RecordingAdapter:
        provider = "greenhouse"
        adapter_version = "test"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_snapshot(self, external_source_id: str) -> SourceSnapshot:
            self.calls.append(external_source_id)
            return SourceSnapshot(
                provider=self.provider,
                external_source_id=external_source_id,
                adapter_version=self.adapter_version,
                is_complete=True,
                jobs=[],
                http_status=200,
            )

    sleeps: list[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    adapter = RecordingAdapter()
    results = asyncio.run(
        sync_sources(
            engine,
            argparse.Namespace(
                provider="greenhouse",
                company_id=None,
                limit=2,
                run_key="paced",
                delay_seconds=2.5,
            ),
            adapter=adapter,
            sleeper=sleeper,
        )
    )

    assert adapter.calls == ["one", "two"]
    assert sleeps == [2.5]
    assert [result.status for result in results] == ["completed", "completed"]


def test_sync_cli_dispatches_every_registered_provider_without_yc_seed(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    companies = CompanyRegistry(engine)
    one = companies.register_company(name="One", website="https://one.example")
    two = companies.register_company(name="Two", website="https://two.example")
    sources = JobSourceRegistry(engine)
    sources.register_url(
        company_id=one.company_id,
        source_url="https://job-boards.greenhouse.io/one",
    )
    sources.register_url(
        company_id=two.company_id,
        source_url="https://jobs.ashbyhq.com/two",
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
                jobs=[],
                http_status=200,
            )

    greenhouse = RecordingAdapter("greenhouse")
    ashby = RecordingAdapter("ashby")
    providers = JobSourceProviderRegistry([greenhouse, ashby])
    results = asyncio.run(
        sync_sources(
            engine,
            argparse.Namespace(
                provider=None,
                company_id=None,
                limit=None,
                run_key="all-providers",
                delay_seconds=0,
            ),
            providers=providers,
        )
    )

    assert greenhouse.calls == ["one"]
    assert ashby.calls == ["two"]
    assert [result.status for result in results] == ["completed", "completed"]


def test_checkpointed_sync_resumes_at_the_first_unfinished_source(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = engine_from_url(postgres_database_url)
    companies = CompanyRegistry(engine)
    registry = JobSourceRegistry(engine)
    for number in range(1, 4):
        company = companies.register_company(
            name=f"Company {number}", website=f"https://company-{number}.example"
        )
        registry.register_url(
            company_id=company.company_id,
            source_url=f"https://job-boards.greenhouse.io/board-{number}",
        )

    class RecordingAdapter:
        provider = "greenhouse"
        adapter_version = "test"

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_snapshot(self, external_source_id: str) -> SourceSnapshot:
            self.calls.append(external_source_id)
            return SourceSnapshot(
                provider=self.provider,
                external_source_id=external_source_id,
                adapter_version=self.adapter_version,
                is_complete=True,
                jobs=[],
                http_status=200,
            )

    checkpoint = tmp_path / "sync-checkpoint.json"
    args = argparse.Namespace(
        provider="greenhouse",
        company_id=None,
        source_ids=None,
        min_source_id=None,
        limit=2,
        run_key="resume-batch",
        delay_seconds=0,
        checkpoint_file=checkpoint,
        max_attempts=4,
        status_file=None,
    )
    adapter = RecordingAdapter()

    first = asyncio.run(sync_sources(engine, args, adapter=adapter))
    second = asyncio.run(sync_sources(engine, args, adapter=adapter))

    assert len(first) == 2
    assert len(second) == 1
    assert adapter.calls == ["board-1", "board-2", "board-3"]
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert {entry["state"] for entry in payload["sources"].values()} == {"completed"}


def test_checkpointed_sync_retries_a_failed_source_with_a_new_attempt_key(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = engine_from_url(postgres_database_url)
    company = CompanyRegistry(engine).register_company(
        name="Retry Company", website="https://retry-company.example"
    )
    source = JobSourceRegistry(engine).register_url(
        company_id=company.company_id,
        source_url="https://job-boards.greenhouse.io/retry-company",
    )

    class RetryAdapter:
        provider = "greenhouse"
        adapter_version = "test"

        def __init__(self) -> None:
            self.calls = 0

        async def fetch_snapshot(self, external_source_id: str) -> SourceSnapshot:
            self.calls += 1
            return SourceSnapshot(
                provider=self.provider,
                external_source_id=external_source_id,
                adapter_version=self.adapter_version,
                is_complete=self.calls > 1,
                jobs=[],
                http_status=200 if self.calls > 1 else 503,
                errors=[] if self.calls > 1 else [{"kind": "http_status", "message": "503"}],
            )

    args = argparse.Namespace(
        provider="greenhouse",
        company_id=None,
        source_ids=[source.career_source_id],
        min_source_id=None,
        limit=None,
        run_key="retry-batch",
        delay_seconds=0,
        checkpoint_file=tmp_path / "retry-checkpoint.json",
        max_attempts=4,
        status_file=None,
    )
    adapter = RetryAdapter()

    first = asyncio.run(sync_sources(engine, args, adapter=adapter))
    second = asyncio.run(sync_sources(engine, args, adapter=adapter))

    assert first[0].status == "failed"
    assert second[0].status == "completed"
    with engine.connect() as connection:
        keys = list(
            connection.scalars(
                select(source_sync_runs_table.c.run_key)
                .where(source_sync_runs_table.c.career_source_id == source.career_source_id)
                .order_by(source_sync_runs_table.c.id)
            )
        )
    assert keys == [
        f"retry-batch:{source.career_source_id}:attempt-1",
        f"retry-batch:{source.career_source_id}:attempt-2",
    ]
