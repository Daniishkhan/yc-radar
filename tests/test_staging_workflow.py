from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot
from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.database import engine_from_url
from yc_radar.services.http_cache import DiskHttpCache
from yc_radar.services.job_source_registry import JobSourceProviderRegistry, JobSourceRegistry
from yc_radar.services.staging import (
    FunnelReporter,
    LeaseLostError,
    Observation,
    SnapshotPromoter,
    SourceEnrichHandler,
    SourceParseHandler,
    StageSuccess,
    StagingRepository,
    StagingWorker,
    UrlFetchHandler,
    WorkLease,
    WorkItemError,
    normalize_work_url,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class SnapshotAdapter:
    provider = "greenhouse"
    adapter_version = "test-1"
    source_kind = "ats_board"

    def __init__(self, snapshot: SourceSnapshot) -> None:
        self.snapshot = snapshot
        self.fetches = 0

    def extract_source_id(self, _url: str) -> str | None:
        return self.snapshot.external_source_id

    def canonical_source_url(self, external_source_id: str) -> str:
        return f"https://job-boards.greenhouse.io/{external_source_id}"

    async def fetch_snapshot(self, _external_source_id: str) -> SourceSnapshot:
        self.fetches += 1
        return self.snapshot


class CountingByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def test_url_normalization_collapses_safe_variants_and_rejects_private_literals() -> None:
    variants = {
        normalize_work_url(
            "HTTPS://JOB-BOARDS.GREENHOUSE.IO:443/acme/?b=2&a=1#opening"
        ),
        normalize_work_url("https://job-boards.greenhouse.io/acme?a=1&b=2"),
    }
    assert variants == {"https://job-boards.greenhouse.io/acme?a=1&b=2"}
    for blocked in (
        "http://localhost/jobs",
        "http://worker.local/jobs",
        "http://127.0.0.1/jobs",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/jobs",
    ):
        with pytest.raises(ValueError, match="not public|not globally routable"):
            normalize_work_url(blocked)


def test_load_is_idempotent_per_source_run_and_deduplicates_urls_globally(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    repository = StagingRepository(engine)
    observation = Observation(
        url="HTTPS://JOB-BOARDS.GREENHOUSE.IO/acme#opening",
        observation_key="crawl-row-1",
        payload={"company_id": 42},
    )

    first = repository.load(
        source="commoncrawl",
        run_key="2026-30",
        observations=[observation],
    )
    replay = repository.load(
        source="commoncrawl",
        run_key="2026-30",
        observations=[observation],
    )
    second_source = repository.load(
        source="vendor",
        run_key="2026-30",
        observations=[replace(observation, observation_key="vendor-row-1")],
    )

    with engine.connect() as connection:
        counts = {
            "runs": connection.scalar(text("SELECT count(*) FROM ingest.runs")),
            "observations": connection.scalar(
                text("SELECT count(*) FROM ingest.raw_observations")
            ),
            "work": connection.scalar(text("SELECT count(*) FROM ingest.url_work_items")),
        }
        work = connection.execute(
            text("SELECT normalized_url, host, run_id FROM ingest.url_work_items")
        ).mappings().one()
    assert first.observations_inserted == 1
    assert first.work_items_inserted == 1
    assert replay.observations_inserted == 0
    assert replay.work_items_inserted == 0
    assert second_source.observations_inserted == 1
    assert second_source.work_items_inserted == 0
    assert counts == {"runs": 2, "observations": 2, "work": 1}
    assert work["normalized_url"] == "https://job-boards.greenhouse.io/acme"
    assert work["host"] == "job-boards.greenhouse.io"
    assert work["run_id"] == first.run_id

    with pytest.raises(ValueError, match="different evidence"):
        repository.load(
            source="commoncrawl",
            run_key="2026-30",
            observations=[replace(observation, url="https://jobs.ashbyhq.com/acme")],
        )


def test_stream_load_commits_bounded_chunks_and_resumes_from_cursor(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    repository = StagingRepository(engine)

    def observations():
        for index in range(5):
            yield Observation(
                url=f"https://job-boards.greenhouse.io/company-{index}",
                observation_key=f"row-{index}",
            )

    first = repository.load_stream(
        source="bulk",
        run_key="file-1",
        observations=observations(),
        input_uri="file:///tmp/source.jsonl",
        input_sha256="c" * 64,
        batch_size=2,
    )
    replay = repository.load_stream(
        source="bulk",
        run_key="file-1",
        observations=observations(),
        input_uri="file:///tmp/source.jsonl",
        input_sha256="c" * 64,
        batch_size=2,
    )

    assert first.observations_seen == 5
    assert first.observations_inserted == 5
    assert replay.observations_seen == 0
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT cursor->>'input_rows_committed' FROM ingest.runs")) == "5"
        assert connection.scalar(text("SELECT count(*) FROM ingest.raw_observations")) == 5


def test_malformed_observation_is_preserved_without_blocking_valid_work(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    repository = StagingRepository(engine)
    loaded = repository.load(
        source="mixed",
        run_key="mixed-1",
        observations=[
            Observation(url="http://127.0.0.1/private", observation_key="invalid"),
            Observation(
                url="https://job-boards.greenhouse.io/valid",
                observation_key="valid",
            ),
        ],
    )

    assert loaded.observations_inserted == 2
    assert loaded.observations_rejected == 1
    assert loaded.work_items_inserted == 1
    with engine.connect() as connection:
        invalid = connection.execute(
            text(
                "SELECT url_work_item_id, payload FROM ingest.raw_observations "
                "WHERE observation_key = 'invalid'"
            )
        ).mappings().one()
    assert invalid["url_work_item_id"] is None
    assert invalid["payload"]["ingest_error"]["kind"] == "invalid_observation_url"

    lease = repository.claim(
        stage="fetch",
        limit=1,
        lease_seconds=30,
        lease_owner="worker",
        run_id=loaded.run_id,
    )[0]
    repository.complete(
        lease,
        StageSuccess(next_stage="done", next_state="promoted"),
    )
    with engine.connect() as connection:
        run = connection.execute(
            text("SELECT status, stats FROM ingest.runs")
        ).mappings().one()
    assert run["status"] == "partial"
    assert run["stats"]["invalid_observations"] == 1


def test_oversized_raw_payload_is_replaced_by_recoverable_bounded_evidence(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    repository = StagingRepository(engine)
    loaded = repository.load(
        source="vendor",
        run_key="oversized-payload",
        observations=[
            Observation(
                url="https://job-boards.greenhouse.io/large-payload",
                payload={"company_id": "42", "blob": "x" * 1_100_000},
            )
        ],
    )

    assert loaded.observations_inserted == 1
    assert loaded.work_items_inserted == 1
    with engine.connect() as connection:
        payload = connection.scalar(text("SELECT payload FROM ingest.raw_observations"))
    assert payload["payload_oversized"] is True
    assert payload["payload_size_bytes"] > 1_000_000
    assert len(payload["payload_sha256"]) == 64
    assert payload["company_id"] == "42"
    assert "blob" not in payload


def test_empty_input_finishes_immediately_and_global_status_is_queryable(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    repository = StagingRepository(engine)
    loaded = repository.load(source="empty", run_key="empty-1", observations=[])

    assert repository.status()["due_retries"] == 0
    assert FunnelReporter(engine).report() == {
        "observations": 0,
        "unique_url_work": 0,
        "verified_sources": 0,
        "promoted_sources": 0,
        "active_jobs": 0,
        "engineering_jobs": 0,
        "pakistan_explicit": 0,
        "global_explicit": 0,
        "remote_unclear": 0,
    }
    with engine.connect() as connection:
        status, completed_at = connection.execute(
            text("SELECT status, completed_at FROM ingest.runs WHERE id = :id"),
            {"id": loaded.run_id},
        ).one()
    assert status == "completed"
    assert completed_at is not None


def test_worker_publishes_after_claim_commit_and_rejects_a_stale_lease(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    repository = StagingRepository(engine)
    loaded = repository.load(
        source="test",
        run_key="lease",
        observations=[Observation(url="https://job-boards.greenhouse.io/acme")],
    )
    observed_states: list[str] = []

    async def handler(lease) -> StageSuccess:
        with engine.connect() as connection:
            observed_states.append(
                str(
                    connection.scalar(
                        text("SELECT state FROM ingest.url_work_items WHERE id = :id"),
                        {"id": lease.id},
                    )
                )
            )
        return StageSuccess(next_stage="parse")

    result = asyncio.run(
        StagingWorker(repository).work(
            stage="fetch",
            handler=handler,
            limit=1,
            lease_seconds=60,
            lease_owner="worker-a",
            run_id=loaded.run_id,
        )
    )
    assert result.succeeded == 1
    assert observed_states == ["leased"]
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT stage, state, attempt_count, lease_owner, lease_token "
                "FROM ingest.url_work_items"
            )
        ).mappings().one()
    assert dict(row) == {
        "stage": "parse",
        "state": "ready",
        "attempt_count": 0,
        "lease_owner": None,
        "lease_token": None,
    }

    lease = repository.claim(
        stage="parse",
        limit=1,
        lease_seconds=60,
        lease_owner="worker-b",
        run_id=loaded.run_id,
    )[0]
    with pytest.raises(LeaseLostError):
        repository.complete(replace(lease, lease_token="stale"), StageSuccess("enrich"))


def test_parse_detects_the_fetched_redirect_target_before_the_observed_url() -> None:
    lease = WorkLease(
        id=1,
        run_id=1,
        raw_observation_id=1,
        normalized_url="https://careers.example.com/jobs",
        stage="parse",
        attempt_count=1,
        max_attempts=4,
        lease_owner="parser",
        lease_token="token",
        lease_expires_at=datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        artifact_uri="disk-http-cache://key/hash",
        http_status=200,
        content_type="text/html",
        content_hash="d" * 64,
        result={
            "fetch": {
                "final_url": "https://job-boards.greenhouse.io/redirected/jobs/1"
            }
        },
        observation_payload={},
        parser_version="parser-1",
        normalizer_version="normalizer-1",
    )

    result = SourceParseHandler()(lease)

    assert result.result["source"]["provider"] == "greenhouse"
    assert result.result["source"]["external_source_id"] == "redirected"
    assert result.result["source"]["detected_from_url"].endswith("/redirected/jobs/1")
    assert result.result["source"]["original_observed_url"] == lease.normalized_url


def test_expired_worker_cannot_publish_a_failure(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    clock = MutableClock()
    repository = StagingRepository(engine, clock=clock)
    loaded = repository.load(
        source="test",
        run_key="expired",
        observations=[Observation(url="https://job-boards.greenhouse.io/expired")],
        max_attempts=1,
    )
    lease = repository.claim(
        stage="fetch",
        limit=1,
        lease_seconds=1,
        lease_owner="slow-worker",
        run_id=loaded.run_id,
    )[0]
    clock.advance(seconds=2)

    with pytest.raises(LeaseLostError):
        repository.fail(
            lease,
            {"kind": "late", "message": "too late"},
            retryable=True,
            backoff_seconds=0,
        )
    assert repository.claim(
        stage="fetch",
        limit=1,
        lease_seconds=1,
        lease_owner="replacement-worker",
        run_id=loaded.run_id,
    ) == []
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT state FROM ingest.url_work_items")) == "dead"


def test_retry_backoff_is_bounded_and_dead_work_can_be_requeued(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    clock = MutableClock()
    repository = StagingRepository(engine, clock=clock)
    loaded = repository.load(
        source="test",
        run_key="retry",
        observations=[Observation(url="https://job-boards.greenhouse.io/retry")],
        max_attempts=2,
    )

    def fail(_lease) -> StageSuccess:
        raise WorkItemError("temporary", "try again", retryable=True)

    worker = StagingWorker(repository, base_backoff_seconds=1, max_backoff_seconds=2)
    first = asyncio.run(
        worker.work(
            stage="fetch",
            handler=fail,
            limit=1,
            lease_seconds=30,
            lease_owner="worker",
            run_id=loaded.run_id,
        )
    )
    assert first.retried == 1
    clock.advance(seconds=1)
    second = asyncio.run(
        worker.work(
            stage="fetch",
            handler=fail,
            limit=1,
            lease_seconds=30,
            lease_owner="worker",
            run_id=loaded.run_id,
        )
    )
    assert second.dead == 1
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT state FROM ingest.url_work_items")) == "dead"
        assert connection.scalar(text("SELECT status FROM ingest.runs")) == "partial"

    assert repository.requeue(
        run_id=loaded.run_id,
        include_states=["dead"],
    ) == {"expired": 0, "manual": 1}
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT state, attempt_count FROM ingest.url_work_items")
        ).one()
    assert tuple(row) == ("ready", 0)


def test_fetch_uses_disk_artifact_pointer_instead_of_database_body(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = engine_from_url(postgres_database_url)
    repository = StagingRepository(engine)
    loaded = repository.load(
        source="test",
        run_key="fetch",
        observations=[Observation(url="https://job-boards.greenhouse.io/cache-test")],
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>cached body</html>", request=request)
    )
    client = httpx.AsyncClient(transport=transport)
    cache = DiskHttpCache(tmp_path / "http")
    try:
        result = asyncio.run(
            StagingWorker(repository).work(
                stage="fetch",
                handler=UrlFetchHandler(cache, client=client),
                limit=1,
                lease_seconds=60,
                lease_owner="fetcher",
                run_id=loaded.run_id,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert result.succeeded == 1
    with engine.connect() as connection:
        work = connection.execute(
            text("SELECT artifact_uri, content_hash, result FROM ingest.url_work_items")
        ).mappings().one()
    assert str(work["artifact_uri"]).startswith("disk-http-cache://")
    assert len(str(work["content_hash"])) == 64
    assert "cached body" not in str(work["result"])
    assert cache.load("https://job-boards.greenhouse.io/cache-test")["text"] == (
        "<html>cached body</html>"
    )


def test_fetch_aborts_stream_as_soon_as_body_limit_is_crossed(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = engine_from_url(postgres_database_url)
    repository = StagingRepository(engine)
    loaded = repository.load(
        source="test",
        run_key="stream-limit",
        observations=[Observation(url="https://job-boards.greenhouse.io/oversized")],
    )
    stream = CountingByteStream([b"abc", b"def", b"must-not-be-read"])
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream, request=request)
    )
    client = httpx.AsyncClient(transport=transport)
    cache = DiskHttpCache(tmp_path / "stream-limit")
    try:
        result = asyncio.run(
            StagingWorker(repository).work(
                stage="fetch",
                handler=UrlFetchHandler(cache, client=client, max_body_bytes=5),
                limit=1,
                lease_seconds=60,
                lease_owner="fetcher",
                run_id=loaded.run_id,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert result.quarantined == 1
    assert stream.yielded == 2
    assert stream.closed is True
    assert cache.metrics["stores"] == 0


def test_fetch_preserves_bounded_retry_after(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = engine_from_url(postgres_database_url)
    clock = MutableClock()
    repository = StagingRepository(engine, clock=clock)
    loaded = repository.load(
        source="test",
        run_key="retry-after",
        observations=[Observation(url="https://job-boards.greenhouse.io/busy")],
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            429,
            headers={"Retry-After": "900"},
            request=request,
        )
    )
    client = httpx.AsyncClient(transport=transport)
    try:
        result = asyncio.run(
            StagingWorker(repository).work(
                stage="fetch",
                handler=UrlFetchHandler(DiskHttpCache(tmp_path / "retry"), client=client),
                limit=1,
                lease_seconds=60,
                lease_owner="fetcher",
                run_id=loaded.run_id,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert result.retried == 1
    with engine.connect() as connection:
        available_at = connection.scalar(
            text("SELECT available_at FROM ingest.url_work_items")
        )
    assert available_at == clock.value + timedelta(seconds=300)


def test_complete_promotion_populates_typed_candidates_and_partial_scan_never_closes(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = engine_from_url(postgres_database_url)
    company_id = CompanyRegistry(engine).register_company(
        name="Acme",
        website="https://acme.example",
    ).company_id
    repository = StagingRepository(engine)
    complete_run = repository.load(
        source="test",
        run_key="complete",
        observations=[
            Observation(
                url="https://job-boards.greenhouse.io/acme",
                payload={"company_id": company_id},
            )
        ],
    )
    _advance_to_promote(repository, complete_run.run_id, tmp_path / "complete")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM company_sources")) == 0

    job = NormalizedJob(
        external_job_id="job-1",
        title="Senior Software Engineer",
        posting_url="https://job-boards.greenhouse.io/acme/jobs/1",
        location="Remote",
        description_text="This role is remote from anywhere in the world.",
        content_hash="a" * 64,
        raw_payload={"large_provider_shape": "kept out of staging payload"},
    )
    complete_snapshot = SourceSnapshot(
        provider="greenhouse",
        external_source_id="acme",
        adapter_version="test-1",
        is_complete=True,
        jobs=[job],
        http_status=200,
    )
    complete_adapter = SnapshotAdapter(complete_snapshot)
    complete_result = asyncio.run(
        SnapshotPromoter(
            repository,
            providers=JobSourceProviderRegistry([complete_adapter]),
        ).promote(
            limit=1,
            lease_seconds=60,
            lease_owner="promoter",
            run_id=complete_run.run_id,
        )
    )
    assert complete_result.succeeded == 1
    assert complete_adapter.fetches == 1

    with engine.connect() as connection:
        candidate = connection.execute(
            text(
                "SELECT status, title, content_hash, promoted_job_id, payload "
                "FROM ingest.job_candidates"
            )
        ).mappings().one()
        canonical = connection.execute(
            text("SELECT status, consecutive_complete_misses FROM jobs")
        ).one()
    assert candidate["status"] == "promoted"
    assert candidate["title"] == "Senior Software Engineer"
    assert candidate["content_hash"] == "a" * 64
    assert candidate["promoted_job_id"] is not None
    assert "large_provider_shape" not in candidate["payload"]
    assert tuple(canonical) == ("active", 0)
    assert FunnelReporter(engine).report(run_id=complete_run.run_id) == {
        "observations": 1,
        "unique_url_work": 1,
        "verified_sources": 1,
        "promoted_sources": 1,
        "active_jobs": 1,
        "engineering_jobs": 1,
        "pakistan_explicit": 0,
        "global_explicit": 1,
        "remote_unclear": 0,
    }

    repeat_run = repository.load(
        source="second-vendor",
        run_key="same-promoted-url",
        observations=[Observation(url="https://job-boards.greenhouse.io/acme")],
    )
    repeat_funnel = FunnelReporter(engine).report(run_id=repeat_run.run_id)
    assert repeat_funnel["unique_url_work"] == 1
    assert repeat_funnel["promoted_sources"] == 1
    assert repeat_funnel["active_jobs"] == 1
    assert repeat_funnel["engineering_jobs"] == 1
    assert repeat_funnel["global_explicit"] == 1

    partial_run = repository.load(
        source="test",
        run_key="partial",
        parser_version="url-source-v2",
        observations=[
            Observation(
                url="https://job-boards.greenhouse.io/acme",
                payload={"company_id": company_id},
            )
        ],
    )
    _advance_to_promote(repository, partial_run.run_id, tmp_path / "partial")
    partial_snapshot = SourceSnapshot(
        provider="greenhouse",
        external_source_id="acme",
        adapter_version="test-1",
        is_complete=False,
        jobs=[job],
        http_status=200,
        errors=[{"kind": "partial", "message": "incomplete page"}],
    )
    partial_result = asyncio.run(
        SnapshotPromoter(
            repository,
            providers=JobSourceProviderRegistry([SnapshotAdapter(partial_snapshot)]),
        ).promote(
            limit=1,
            lease_seconds=60,
            lease_owner="promoter",
            run_id=partial_run.run_id,
        )
    )
    assert partial_result.quarantined == 1
    with engine.connect() as connection:
        canonical = connection.execute(
            text("SELECT status, consecutive_complete_misses FROM jobs")
        ).one()
        work_state = connection.scalar(
            text(
                "SELECT state FROM ingest.url_work_items "
                "WHERE parser_version = 'url-source-v2'"
            )
        )
        sync_status = connection.scalar(
            text("SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1")
        )
        candidate_status = connection.scalar(
            text(
                "SELECT status FROM ingest.job_candidates "
                "WHERE run_id = :run_id"
            ),
            {"run_id": partial_run.run_id},
        )
    assert tuple(canonical) == ("active", 0)
    assert work_state == "quarantined"
    assert sync_status == "partial"
    assert candidate_status == "quarantined"


def test_enrich_quarantines_shared_board_with_multiple_company_candidates(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    first_company_id = CompanyRegistry(engine).register_company(
        name="First Candidate",
        website="https://first-candidate.example",
    ).company_id
    second_company_id = CompanyRegistry(engine).register_company(
        name="Second Candidate",
        website="https://second-candidate.example",
    ).company_id
    JobSourceRegistry(engine).register_url(
        company_id=first_company_id,
        source_url="https://job-boards.greenhouse.io/shared-board",
    )
    repository = StagingRepository(engine)
    loaded = repository.load(
        source="aggregator",
        run_key="ambiguous-enrich",
        observations=[
            Observation(
                url="https://job-boards.greenhouse.io/shared-board",
                observation_key="first",
                payload={"company_id": first_company_id},
            ),
            Observation(
                url="https://job-boards.greenhouse.io/shared-board",
                observation_key="second",
                payload={"company_id": second_company_id},
            ),
        ],
    )
    _advance_to_enrich(repository, loaded.run_id, external_source_id="shared-board")

    result = asyncio.run(
        StagingWorker(repository).work(
            stage="enrich",
            handler=SourceEnrichHandler(engine),
            limit=1,
            lease_seconds=60,
            lease_owner="enricher",
            run_id=loaded.run_id,
        )
    )

    assert result.quarantined == 1
    with engine.connect() as connection:
        work = connection.execute(
            text("SELECT stage, state, last_error FROM ingest.url_work_items")
        ).mappings().one()
        assert connection.scalar(text("SELECT count(*) FROM company_sources")) == 1
    assert work["stage"] == "enrich"
    assert work["state"] == "quarantined"
    assert work["last_error"]["kind"] == "ambiguous_company_identity"


def test_promoter_rechecks_company_ambiguity_before_fetching_snapshot(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    first_company_id = CompanyRegistry(engine).register_company(
        name="First Candidate",
        website="https://first-candidate.example",
    ).company_id
    second_company_id = CompanyRegistry(engine).register_company(
        name="Second Candidate",
        website="https://second-candidate.example",
    ).company_id
    existing_source_id = JobSourceRegistry(engine).register_url(
        company_id=first_company_id,
        source_url="https://job-boards.greenhouse.io/shared-board",
    ).company_source_id
    repository = StagingRepository(engine)
    loaded = repository.load(
        source="aggregator",
        run_key="ambiguous-promote",
        observations=[
            Observation(
                url="https://job-boards.greenhouse.io/shared-board",
                observation_key="first",
                payload={"company_id": first_company_id},
            ),
            Observation(
                url="https://job-boards.greenhouse.io/shared-board",
                observation_key="second",
                payload={"company_id": second_company_id},
            ),
        ],
    )
    _advance_to_enrich(repository, loaded.run_id, external_source_id="shared-board")
    enrich_lease = repository.claim(
        stage="enrich",
        limit=1,
        lease_seconds=60,
        lease_owner="legacy-enricher",
        run_id=loaded.run_id,
    )[0]
    repository.complete(
        enrich_lease,
        StageSuccess(
            next_stage="promote",
            result={
                "company_source_id": existing_source_id,
                "proposed_company_id": second_company_id,
                "source": _source_evidence("shared-board"),
            },
        ),
    )
    snapshot = SourceSnapshot(
        provider="greenhouse",
        external_source_id="shared-board",
        adapter_version="test-1",
        is_complete=True,
        jobs=[
            NormalizedJob(
                external_job_id="job-1",
                title="Backend Engineer",
                content_hash="a" * 64,
            )
        ],
        http_status=200,
    )
    adapter = SnapshotAdapter(snapshot)

    result = asyncio.run(
        SnapshotPromoter(
            repository,
            providers=JobSourceProviderRegistry([adapter]),
        ).promote(
            limit=1,
            lease_seconds=60,
            lease_owner="promoter",
            run_id=loaded.run_id,
        )
    )

    assert result.quarantined == 1
    assert adapter.fetches == 0
    with engine.connect() as connection:
        work = connection.execute(
            text("SELECT stage, state, last_error FROM ingest.url_work_items")
        ).mappings().one()
        assert connection.scalar(text("SELECT count(*) FROM company_sources")) == 1
    assert work["stage"] == "promote"
    assert work["state"] == "quarantined"
    assert work["last_error"]["kind"] == "ambiguous_company_identity"


def test_invalid_complete_snapshot_quarantines_candidates_and_finalizes_sync_run(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = engine_from_url(postgres_database_url)
    company_id = CompanyRegistry(engine).register_company(
        name="Invalid Snapshot Co",
        website="https://invalid-snapshot.example",
    ).company_id
    JobSourceRegistry(engine).register_url(
        company_id=company_id,
        source_url="https://job-boards.greenhouse.io/invalid-board",
    )
    repository = StagingRepository(engine)
    loaded = repository.load(
        source="test",
        run_key="invalid-complete",
        observations=[
            Observation(url="https://job-boards.greenhouse.io/invalid-board")
        ],
    )
    _advance_to_promote(repository, loaded.run_id, tmp_path / "invalid-complete")
    snapshot = SourceSnapshot(
        provider="greenhouse",
        external_source_id="invalid-board",
        adapter_version="test-1",
        is_complete=True,
        http_status=200,
        jobs=[
            NormalizedJob(
                external_job_id=" ",
                title=" ",
                content_hash="a" * 64,
            ),
            NormalizedJob(
                external_job_id="duplicate",
                title="Backend Engineer",
                content_hash="b" * 64,
            ),
            NormalizedJob(
                external_job_id="duplicate",
                title="Frontend Engineer",
                content_hash="c" * 64,
            ),
        ],
    )

    result = asyncio.run(
        SnapshotPromoter(
            repository,
            providers=JobSourceProviderRegistry([SnapshotAdapter(snapshot)]),
        ).promote(
            limit=1,
            lease_seconds=60,
            lease_owner="promoter",
            run_id=loaded.run_id,
        )
    )

    assert result.quarantined == 1
    with engine.connect() as connection:
        candidates = connection.execute(
            text(
                "SELECT status, external_job_id, title, quality_flags "
                "FROM ingest.job_candidates ORDER BY id"
            )
        ).mappings().all()
        sync_run = connection.execute(
            text("SELECT status, completed_at FROM sync_runs")
        ).mappings().one()
        canonical_jobs = connection.scalar(text("SELECT count(*) FROM jobs"))
    assert len(candidates) == 3
    assert candidates[0]["external_job_id"] is None
    assert candidates[0]["title"] is None
    assert all(candidate["status"] == "quarantined" for candidate in candidates)
    assert all("invalid_snapshot" in candidate["quality_flags"] for candidate in candidates)
    assert sync_run["status"] == "failed"
    assert sync_run["completed_at"] is not None
    assert canonical_jobs == 0


def test_complete_snapshot_can_create_a_provisional_company_from_consistent_provider_name(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = engine_from_url(postgres_database_url)
    repository = StagingRepository(engine)
    loaded = repository.load(
        source="test",
        run_key="provider-name",
        observations=[Observation(url="https://job-boards.greenhouse.io/named-board")],
    )
    _advance_to_promote(repository, loaded.run_id, tmp_path / "named")
    job = NormalizedJob(
        external_job_id="named-1",
        title="Backend Engineer",
        content_hash="b" * 64,
        raw_payload={"company_name": "Provider Named Company"},
    )
    snapshot = SourceSnapshot(
        provider="greenhouse",
        external_source_id="named-board",
        adapter_version="test-1",
        is_complete=True,
        jobs=[job],
        http_status=200,
    )

    result = asyncio.run(
        SnapshotPromoter(
            repository,
            providers=JobSourceProviderRegistry([SnapshotAdapter(snapshot)]),
        ).promote(
            limit=1,
            lease_seconds=60,
            lease_owner="promoter",
            run_id=loaded.run_id,
        )
    )

    assert result.succeeded == 1
    with engine.connect() as connection:
        company = connection.execute(
            text("SELECT name, identity_state FROM companies")
        ).one()
        source = connection.execute(
            text("SELECT provider, external_id FROM company_sources")
        ).one()
    assert tuple(company) == ("Provider Named Company", "provisional")
    assert tuple(source) == ("greenhouse", "named-board")


def _source_evidence(external_source_id: str) -> dict[str, str]:
    url = f"https://job-boards.greenhouse.io/{external_source_id}"
    return {
        "provider": "greenhouse",
        "source_kind": "ats_board",
        "external_source_id": external_source_id,
        "canonical_url": url,
        "detected_from_url": url,
        "original_observed_url": url,
    }


def _advance_to_enrich(
    repository: StagingRepository,
    run_id: int,
    *,
    external_source_id: str,
) -> None:
    fetch_lease = repository.claim(
        stage="fetch",
        limit=1,
        lease_seconds=60,
        lease_owner="fetcher",
        run_id=run_id,
    )[0]
    repository.complete(fetch_lease, StageSuccess(next_stage="parse"))
    parse_lease = repository.claim(
        stage="parse",
        limit=1,
        lease_seconds=60,
        lease_owner="parser",
        run_id=run_id,
    )[0]
    repository.complete(
        parse_lease,
        StageSuccess(
            next_stage="enrich",
            result={"source": _source_evidence(external_source_id)},
        ),
    )


def _advance_to_promote(repository: StagingRepository, run_id: int, cache_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>board</html>", request=request)
    )
    client = httpx.AsyncClient(transport=transport)
    try:
        fetch = asyncio.run(
            StagingWorker(repository).work(
                stage="fetch",
                handler=UrlFetchHandler(DiskHttpCache(cache_path), client=client),
                limit=1,
                lease_seconds=60,
                lease_owner="fetcher",
                run_id=run_id,
            )
        )
    finally:
        asyncio.run(client.aclose())
    assert fetch.succeeded == 1
    parse = asyncio.run(
        StagingWorker(repository).work(
            stage="parse",
            handler=SourceParseHandler(),
            limit=1,
            lease_seconds=60,
            lease_owner="parser",
            run_id=run_id,
        )
    )
    assert parse.succeeded == 1
    enrich = asyncio.run(
        StagingWorker(repository).work(
            stage="enrich",
            handler=SourceEnrichHandler(repository.engine),
            limit=1,
            lease_seconds=60,
            lease_owner="enricher",
            run_id=run_id,
        )
    )
    assert enrich.succeeded == 1
