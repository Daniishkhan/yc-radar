from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from yc_radar.adapters.base import JobSourceAdapter
from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot, SyncResult
from yc_radar.services.candidate_fit import classify_remote_eligibility, classify_role_text
from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.http_cache import DiskHttpCache
from yc_radar.services.job_source_registry import (
    JobSourceProviderRegistry,
    JobSourceRegistry,
    default_job_source_providers,
)
from yc_radar.services.job_sync_service import JobSyncService


PARSER_VERSION = "url-source-v1"
NORMALIZER_VERSION = "job-source-v1"
WORK_STAGES = ("fetch", "parse", "enrich", "promote", "done")
WORK_STATES = (
    "ready",
    "leased",
    "retry",
    "verified",
    "promoted",
    "quarantined",
    "dead",
)
TERMINAL_STATES = frozenset({"promoted", "quarantined", "dead"})
MAX_ERROR_MESSAGE_LENGTH = 500
MAX_RAW_OBSERVATION_PAYLOAD_BYTES = 900_000


class LeaseLostError(RuntimeError):
    """Raised when a worker tries to publish after its database lease is no longer valid."""


class WorkItemError(RuntimeError):
    """A bounded, serializable work failure with an explicit retry policy."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Observation:
    url: str
    observation_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime | None = None
    priority: int = 0


@dataclass(frozen=True)
class LoadResult:
    run_id: int
    run_key: str
    observations_seen: int
    observations_inserted: int
    work_items_inserted: int
    observations_rejected: int = 0


@dataclass(frozen=True)
class WorkLease:
    id: int
    run_id: int
    raw_observation_id: int
    normalized_url: str
    stage: str
    attempt_count: int
    max_attempts: int
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime
    artifact_uri: str | None
    http_status: int | None
    content_type: str | None
    content_hash: str | None
    result: dict[str, Any]
    observation_payload: dict[str, Any]
    parser_version: str
    normalizer_version: str


@dataclass(frozen=True)
class StageSuccess:
    next_stage: str
    next_state: str = "ready"
    result: dict[str, Any] = field(default_factory=dict)
    artifact_uri: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    content_hash: str | None = None


@dataclass(frozen=True)
class BatchResult:
    claimed: int
    succeeded: int
    retried: int
    quarantined: int
    dead: int
    lease_lost: int


class StageHandler(Protocol):
    def __call__(self, lease: WorkLease) -> StageSuccess | Awaitable[StageSuccess]: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_work_url(value: str) -> str:
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid URL: {value!r}") from exc
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"URL must be an absolute public HTTP(S) URL: {value!r}")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError(f"URL host is not public: {host}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError(f"URL IP address is not globally routable: {host}")
    authority_host = f"[{host}]" if ":" in host else host
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        authority = authority_host
    else:
        authority = f"{authority_host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    normalized = urlunsplit((scheme, authority, path, query, ""))
    if len(normalized.encode("utf-8")) > 2048:
        raise ValueError("normalized URL exceeds 2048 bytes")
    return normalized


def observation_key_for(source: str, observation: Observation, normalized_url: str) -> str:
    if observation.observation_key:
        return observation.observation_key.strip()
    payload = {
        "source": source,
        "url": normalized_url,
        "payload": observation.payload,
        "observed_at": observation.observed_at.isoformat() if observation.observed_at else None,
        "priority": observation.priority,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def bounded_error(exc: BaseException, *, kind: str | None = None) -> dict[str, str]:
    return {
        "kind": (kind or type(exc).__name__)[:100],
        "message": str(exc)[:MAX_ERROR_MESSAGE_LENGTH],
        "at": utc_now().isoformat(),
    }


class StagingRepository:
    """Short Postgres transactions for the durable ingest queue and its leases."""

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.engine = engine
        self._clock = clock

    def load(
        self,
        *,
        run_key: str,
        source: str,
        observations: Sequence[Observation],
        parser_version: str = PARSER_VERSION,
        normalizer_version: str = NORMALIZER_VERSION,
        max_attempts: int = 4,
        input_uri: str | None = None,
        input_sha256: str | None = None,
        _cursor_rows_committed: int | None = None,
    ) -> LoadResult:
        _require_bounded_text(run_key, field="run_key", max_bytes=512)
        _require_bounded_text(source, field="source", max_bytes=128)
        _require_bounded_text(parser_version, field="parser_version", max_bytes=128)
        _require_bounded_text(normalizer_version, field="normalizer_version", max_bytes=128)
        if input_uri is not None:
            _require_bounded_text(input_uri, field="input_uri", max_bytes=8192)
        if input_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
            raise ValueError("input_sha256 must be a lowercase SHA-256 hex digest")
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        now = self._clock()
        prepared: list[tuple[Observation, str | None, str | None, str, dict[str, str] | None]] = []
        for observation in observations:
            ingest_error = None
            try:
                normalized_url = normalize_work_url(observation.url)
                if not -1_000_000 <= observation.priority <= 1_000_000:
                    raise ValueError("observation priority must be between -1000000 and 1000000")
                host = urlsplit(normalized_url).hostname or ""
            except ValueError as exc:
                normalized_url = None
                host = None
                ingest_error = bounded_error(exc, kind="invalid_observation_url")
            key = observation_key_for(
                source,
                observation,
                normalized_url or observation.url.strip(),
            )
            if not key or len(key) > 512:
                key = hashlib.sha256(
                    f"{source}\0{key}\0{observation.url}".encode(errors="replace")
                ).hexdigest()
                ingest_error = ingest_error or {
                    "kind": "invalid_observation_key",
                    "message": "observation key was blank or exceeded 512 characters",
                    "at": now.isoformat(),
                }
            prepared.append((observation, normalized_url, host, key, ingest_error))

        observations_inserted = 0
        work_items_inserted = 0
        observations_rejected = 0
        with self.engine.begin() as connection:
            run = (
                connection.execute(
                    text(
                        """
                        INSERT INTO ingest.runs (
                            run_key, source, input_uri, input_sha256, status,
                            parser_version, normalizer_version, cursor, stats,
                            started_at, completed_at
                        ) VALUES (
                            :run_key, :source, :input_uri, :input_sha256, 'running',
                            :parser_version, :normalizer_version, '{}'::jsonb,
                            '{}'::jsonb, :now, NULL
                        )
                        ON CONFLICT (source, run_key) DO NOTHING
                        RETURNING id, run_key, source, input_uri, input_sha256,
                                  parser_version, normalizer_version
                        """
                    ),
                    {
                        "run_key": run_key,
                        "source": source,
                        "parser_version": parser_version,
                        "normalizer_version": normalizer_version,
                        "input_uri": input_uri,
                        "input_sha256": input_sha256,
                        "now": now,
                    },
                )
                .mappings()
                .first()
            )
            if run is None:
                run = (
                    connection.execute(
                        text(
                            """
                            SELECT id, run_key, source, input_uri, input_sha256,
                                   parser_version, normalizer_version
                            FROM ingest.runs
                            WHERE source = :source AND run_key = :run_key
                            FOR UPDATE
                            """
                        ),
                        {"source": source, "run_key": run_key},
                    )
                    .mappings()
                    .one()
                )
            expected = (source, input_uri, input_sha256, parser_version, normalizer_version)
            actual = (
                run["source"],
                run["input_uri"],
                run["input_sha256"],
                run["parser_version"],
                run["normalizer_version"],
            )
            if actual != expected:
                raise ValueError(
                    "source/run_key already exists with different input or version metadata"
                )
            run_id = int(run["id"])

            for observation, normalized_url, host, key, ingest_error in prepared:
                observed_at = observation.observed_at or now
                work = (
                    connection.execute(
                        text(
                            """
                            INSERT INTO ingest.url_work_items (
                                run_id, normalized_url, host, stage, state, priority,
                                attempt_count, max_attempts, available_at, lease_owner,
                                lease_token, lease_expires_at, artifact_uri, http_status,
                                content_type, content_hash, result, last_error,
                                parser_version, normalizer_version, created_at, updated_at
                            ) VALUES (
                                :run_id, :normalized_url, :host, 'fetch', 'ready', :priority,
                                0, :max_attempts, :now, NULL, NULL, NULL, NULL, NULL,
                                NULL, NULL, '{}'::jsonb, '{}'::jsonb,
                                :parser_version, :normalizer_version, :now, :now
                            )
                            ON CONFLICT (
                                normalized_url, parser_version, normalizer_version
                            ) DO NOTHING
                            RETURNING id
                            """
                        ),
                        {
                            "run_id": run_id,
                            "normalized_url": normalized_url,
                            "host": host,
                            "priority": observation.priority,
                            "max_attempts": max_attempts,
                            "parser_version": parser_version,
                            "normalizer_version": normalizer_version,
                            "now": now,
                        },
                    )
                    .mappings()
                    .first()
                    if normalized_url is not None and host is not None
                    else None
                )
                if work is not None:
                    work_id = int(work["id"])
                    work_items_inserted += 1
                elif normalized_url is not None:
                    existing_work = (
                        connection.execute(
                            text(
                                """
                                SELECT id
                                FROM ingest.url_work_items
                                WHERE normalized_url = :normalized_url
                                  AND parser_version = :parser_version
                                  AND normalizer_version = :normalizer_version
                                """
                            ),
                            {
                                "normalized_url": normalized_url,
                                "parser_version": parser_version,
                                "normalizer_version": normalizer_version,
                            },
                        )
                        .mappings()
                        .one()
                    )
                    work_id = int(existing_work["id"])
                    connection.execute(
                        text(
                            """
                            UPDATE ingest.url_work_items
                            SET priority = GREATEST(priority, :priority),
                                max_attempts = GREATEST(max_attempts, :max_attempts),
                                updated_at = :now
                            WHERE id = :work_id
                            """
                        ),
                        {
                            "work_id": work_id,
                            "priority": observation.priority,
                            "max_attempts": max_attempts,
                            "now": now,
                        },
                    )

                else:
                    work_id = None
                    observations_rejected += 1

                raw_payload = _bounded_observation_payload(observation.payload)
                if ingest_error is not None:
                    raw_payload["ingest_error"] = ingest_error
                    raw_payload["observed_url_sha256"] = hashlib.sha256(
                        observation.url.encode(errors="replace")
                    ).hexdigest()
                stored_observed_url = observation.url[:8192] or None
                raw = (
                    connection.execute(
                        text(
                            """
                            INSERT INTO ingest.raw_observations (
                                run_id, url_work_item_id, observation_key,
                                observed_url, payload, observed_at
                            ) VALUES (
                                :run_id, :work_item_id, :observation_key,
                                :observed_url, CAST(:payload AS jsonb), :observed_at
                            )
                            ON CONFLICT (run_id, observation_key) DO NOTHING
                            RETURNING id
                            """
                        ),
                        {
                            "run_id": run_id,
                            "work_item_id": work_id,
                            "observation_key": key,
                            "observed_url": stored_observed_url,
                            "payload": _json(raw_payload),
                            "observed_at": observed_at,
                        },
                    )
                    .mappings()
                    .first()
                )
                if raw is not None:
                    observations_inserted += 1
                else:
                    existing_raw = (
                        connection.execute(
                            text(
                                """
                                SELECT url_work_item_id, observed_url, payload, observed_at
                                FROM ingest.raw_observations
                                WHERE run_id = :run_id AND observation_key = :observation_key
                                """
                            ),
                            {"run_id": run_id, "observation_key": key},
                        )
                        .mappings()
                        .one()
                    )
                    same_time = (
                        observation.observed_at is None
                        or existing_raw["observed_at"] == observation.observed_at
                    )
                    if (
                        existing_raw["url_work_item_id"] != work_id
                        or existing_raw["observed_url"] != stored_observed_url
                        or dict(existing_raw["payload"] or {}) != raw_payload
                        or not same_time
                    ):
                        raise ValueError(
                            f"observation key {key!r} was reused with different evidence"
                        )

            if _cursor_rows_committed is not None:
                connection.execute(
                    text(
                        """
                        UPDATE ingest.runs
                        SET cursor = cursor || jsonb_build_object(
                                'input_rows_committed', :rows_committed
                            )
                        WHERE id = :run_id
                        """
                    ),
                    {"run_id": run_id, "rows_committed": _cursor_rows_committed},
                )

        self.refresh_run(run_id)
        return LoadResult(
            run_id=run_id,
            run_key=run_key,
            observations_seen=len(prepared),
            observations_inserted=observations_inserted,
            work_items_inserted=work_items_inserted,
            observations_rejected=observations_rejected,
        )

    def load_stream(
        self,
        *,
        run_key: str,
        source: str,
        observations: Iterable[Observation],
        parser_version: str = PARSER_VERSION,
        normalizer_version: str = NORMALIZER_VERSION,
        max_attempts: int = 4,
        input_uri: str | None = None,
        input_sha256: str | None = None,
        batch_size: int = 500,
    ) -> LoadResult:
        """Load an immutable input in committed chunks and resume from its durable cursor."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if not input_uri or not input_sha256:
            raise ValueError("stream loading requires input_uri and input_sha256")
        initial = self.load(
            run_key=run_key,
            source=source,
            observations=[],
            parser_version=parser_version,
            normalizer_version=normalizer_version,
            max_attempts=max_attempts,
            input_uri=input_uri,
            input_sha256=input_sha256,
        )
        with self.engine.connect() as connection:
            cursor = connection.scalar(
                text("SELECT cursor FROM ingest.runs WHERE id = :run_id"),
                {"run_id": initial.run_id},
            )
        committed = int(dict(cursor or {}).get("input_rows_committed") or 0)
        iterator = iter(observations)
        if committed:
            skipped = sum(1 for _ in islice(iterator, committed))
            if skipped != committed:
                raise ValueError("input is shorter than the committed ingest cursor")

        seen = inserted = rejected = work_inserted = 0
        while batch := list(islice(iterator, batch_size)):
            committed += len(batch)
            result = self.load(
                run_key=run_key,
                source=source,
                observations=batch,
                parser_version=parser_version,
                normalizer_version=normalizer_version,
                max_attempts=max_attempts,
                input_uri=input_uri,
                input_sha256=input_sha256,
                _cursor_rows_committed=committed,
            )
            seen += result.observations_seen
            inserted += result.observations_inserted
            rejected += result.observations_rejected
            work_inserted += result.work_items_inserted
        return LoadResult(
            run_id=initial.run_id,
            run_key=run_key,
            observations_seen=seen,
            observations_inserted=inserted,
            work_items_inserted=work_inserted,
            observations_rejected=rejected,
        )

    def claim(
        self,
        *,
        stage: str,
        limit: int,
        lease_seconds: int,
        lease_owner: str,
        run_id: int | None = None,
    ) -> list[WorkLease]:
        if stage not in WORK_STAGES[:-1]:
            raise ValueError(f"unsupported work stage: {stage}")
        if limit < 1 or lease_seconds < 1 or not lease_owner.strip():
            raise ValueError("limit, lease_seconds, and lease_owner must be valid")
        now = self._clock()
        expires_at = now + timedelta(seconds=lease_seconds)
        batch_token = uuid.uuid4().hex
        eligible_states = (
            ["ready", "verified", "retry"] if stage == "promote" else ["ready", "retry"]
        )
        with self.engine.begin() as connection:
            expired_ids = [
                int(value)
                for value in connection.scalars(
                    text(
                        """
                        UPDATE ingest.url_work_items AS work
                        SET state = CASE
                                WHEN attempt_count >= max_attempts THEN 'dead'
                                ELSE 'retry'
                            END,
                            available_at = :now,
                            lease_owner = NULL,
                            lease_token = NULL,
                            lease_expires_at = NULL,
                            last_error = jsonb_build_object(
                                'kind', 'lease_expired',
                                'message', 'worker lease expired before publication',
                                'at', CAST(:now_text AS text)
                            ),
                            updated_at = :now
                        WHERE state = 'leased'
                          AND lease_expires_at <= :now
                          AND (
                              CAST(:run_id AS bigint) IS NULL OR EXISTS (
                                  SELECT 1 FROM ingest.raw_observations AS observation
                                  WHERE observation.url_work_item_id = work.id
                                    AND observation.run_id = CAST(:run_id AS bigint)
                              )
                          )
                        RETURNING id
                        """
                    ),
                    {"now": now, "now_text": now.isoformat(), "run_id": run_id},
                )
            ]
            rows = (
                connection.execute(
                    text(
                        """
                        WITH claimable AS (
                            SELECT work.id
                            FROM ingest.url_work_items AS work
                            WHERE work.stage = :stage
                              AND EXISTS (
                                  SELECT 1
                                  FROM ingest.raw_observations AS observation
                                  WHERE observation.url_work_item_id = work.id
                                    AND (
                                        CAST(:run_id AS bigint) IS NULL
                                        OR observation.run_id = CAST(:run_id AS bigint)
                                    )
                              )
                              AND work.available_at <= :now
                              AND (
                                  work.state = ANY(CAST(:eligible_states AS text[]))
                                  OR (
                                      work.state = 'leased'
                                      AND work.lease_expires_at <= :now
                                  )
                              )
                              AND work.attempt_count < work.max_attempts
                            ORDER BY work.priority DESC, work.available_at, work.id
                            FOR UPDATE SKIP LOCKED
                            LIMIT :limit
                        )
                        UPDATE ingest.url_work_items AS work
                        SET state = 'leased',
                            attempt_count = work.attempt_count + 1,
                            lease_owner = :lease_owner,
                            lease_token = :batch_token || ':' || work.id::text,
                            lease_expires_at = :expires_at,
                            updated_at = :now
                        FROM claimable
                        WHERE work.id = claimable.id
                        RETURNING work.*
                        """
                    ),
                    {
                        "stage": stage,
                        "run_id": run_id,
                        "eligible_states": eligible_states,
                        "now": now,
                        "expires_at": expires_at,
                        "batch_token": batch_token,
                        "lease_owner": lease_owner,
                        "limit": limit,
                    },
                )
                .mappings()
                .all()
            )
            work_ids = [int(row["id"]) for row in rows]
            contexts: dict[int, dict[str, Any]] = {}
            if work_ids:
                contexts = {
                    int(row["url_work_item_id"]): dict(row)
                    for row in connection.execute(
                        text(
                            """
                            SELECT DISTINCT ON (url_work_item_id)
                                   url_work_item_id, id, run_id, payload
                            FROM ingest.raw_observations
                            WHERE url_work_item_id = ANY(CAST(:work_ids AS bigint[]))
                              AND (
                                  CAST(:run_id AS bigint) IS NULL
                                  OR run_id = CAST(:run_id AS bigint)
                              )
                            ORDER BY url_work_item_id, observed_at DESC, id DESC
                            """
                        ),
                        {"work_ids": work_ids, "run_id": run_id},
                    ).mappings()
                }
        for work_item_id in expired_ids:
            self._refresh_linked_runs(work_item_id)
        return [
            WorkLease(
                id=int(row["id"]),
                run_id=int(contexts[int(row["id"])]["run_id"]),
                raw_observation_id=int(contexts[int(row["id"])]["id"]),
                normalized_url=str(row["normalized_url"]),
                stage=str(row["stage"]),
                attempt_count=int(row["attempt_count"]),
                max_attempts=int(row["max_attempts"]),
                lease_owner=str(row["lease_owner"]),
                lease_token=str(row["lease_token"]),
                lease_expires_at=row["lease_expires_at"],
                artifact_uri=row["artifact_uri"],
                http_status=row["http_status"],
                content_type=row["content_type"],
                content_hash=row["content_hash"],
                result=dict(row["result"] or {}),
                observation_payload=dict(contexts[int(row["id"])]["payload"] or {}),
                parser_version=str(row["parser_version"]),
                normalizer_version=str(row["normalizer_version"]),
            )
            for row in rows
        ]

    def lease_is_current(self, lease: WorkLease) -> bool:
        now = self._clock()
        with self.engine.connect() as connection:
            return bool(
                connection.scalar(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM ingest.url_work_items
                            WHERE id = :id
                              AND state = 'leased'
                              AND lease_owner = :lease_owner
                              AND lease_token = :lease_token
                              AND lease_expires_at > :now
                        )
                        """
                    ),
                    {
                        "id": lease.id,
                        "lease_owner": lease.lease_owner,
                        "lease_token": lease.lease_token,
                        "now": now,
                    },
                )
            )

    def complete(self, lease: WorkLease, success: StageSuccess) -> None:
        if success.next_stage not in WORK_STAGES or success.next_state not in WORK_STATES:
            raise ValueError("invalid staging transition")
        now = self._clock()
        with self.engine.begin() as connection:
            updated = connection.scalar(
                text(
                    """
                    UPDATE ingest.url_work_items
                    SET stage = :next_stage,
                        state = :next_state,
                        attempt_count = 0,
                        result = COALESCE(result, '{}'::jsonb) || CAST(:result AS jsonb),
                        artifact_uri = COALESCE(:artifact_uri, artifact_uri),
                        http_status = COALESCE(:http_status, http_status),
                        content_type = COALESCE(:content_type, content_type),
                        content_hash = COALESCE(:content_hash, content_hash),
                        last_error = '{}'::jsonb,
                        available_at = :now,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = :now
                    WHERE id = :id
                      AND state = 'leased'
                      AND lease_owner = :lease_owner
                      AND lease_token = :lease_token
                      AND lease_expires_at > :now
                    RETURNING id
                    """
                ),
                {
                    "id": lease.id,
                    "lease_owner": lease.lease_owner,
                    "lease_token": lease.lease_token,
                    "next_stage": success.next_stage,
                    "next_state": success.next_state,
                    "result": _json(success.result),
                    "artifact_uri": success.artifact_uri,
                    "http_status": success.http_status,
                    "content_type": success.content_type,
                    "content_hash": success.content_hash,
                    "now": now,
                },
            )
            if updated is None:
                raise LeaseLostError(f"lease lost for work item {lease.id}")
        self._refresh_linked_runs(lease.id)

    def fail(
        self,
        lease: WorkLease,
        error: Mapping[str, Any],
        *,
        retryable: bool,
        backoff_seconds: float,
    ) -> str:
        now = self._clock()
        exhausted = lease.attempt_count >= lease.max_attempts
        next_state = "dead" if retryable and exhausted else "retry" if retryable else "quarantined"
        available_at = now + timedelta(seconds=max(0.0, backoff_seconds))
        with self.engine.begin() as connection:
            updated = connection.scalar(
                text(
                    """
                    UPDATE ingest.url_work_items
                    SET state = :next_state,
                        last_error = CAST(:last_error AS jsonb),
                        available_at = :available_at,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = :now
                    WHERE id = :id
                      AND state = 'leased'
                      AND lease_owner = :lease_owner
                      AND lease_token = :lease_token
                      AND lease_expires_at > :now
                    RETURNING id
                    """
                ),
                {
                    "id": lease.id,
                    "lease_owner": lease.lease_owner,
                    "lease_token": lease.lease_token,
                    "next_state": next_state,
                    "last_error": _json(dict(error)),
                    "available_at": available_at,
                    "now": now,
                },
            )
            if updated is None:
                raise LeaseLostError(f"lease lost for work item {lease.id}")
        self._refresh_linked_runs(lease.id)
        return next_state

    def requeue(
        self,
        *,
        run_id: int | None = None,
        include_states: Sequence[str] = (),
        reset_attempts: bool = False,
    ) -> dict[str, int]:
        invalid = set(include_states).difference({"quarantined", "dead"})
        if invalid:
            raise ValueError(f"unsupported manual requeue states: {sorted(invalid)}")
        now = self._clock()
        with self.engine.begin() as connection:
            expired_ids = [
                int(value)
                for value in connection.scalars(
                    text(
                        """
                        UPDATE ingest.url_work_items AS work
                        SET state = CASE
                                WHEN attempt_count >= max_attempts THEN 'dead'
                                ELSE 'retry'
                            END,
                            available_at = :now,
                            lease_owner = NULL,
                            lease_token = NULL,
                            lease_expires_at = NULL,
                            last_error = jsonb_build_object(
                                'kind', 'lease_expired',
                                'message', 'worker lease expired before publication',
                                'at', CAST(:now_text AS text)
                            ),
                            updated_at = :now
                        WHERE state = 'leased'
                          AND lease_expires_at <= :now
                          AND (
                              CAST(:run_id AS bigint) IS NULL OR EXISTS (
                                  SELECT 1 FROM ingest.raw_observations AS observation
                                  WHERE observation.url_work_item_id = work.id
                                    AND observation.run_id = CAST(:run_id AS bigint)
                              )
                          )
                        RETURNING id
                        """
                    ),
                    {"now": now, "now_text": now.isoformat(), "run_id": run_id},
                )
            ]
            manual_ids: list[int] = []
            if include_states:
                manual_ids = [
                    int(value)
                    for value in connection.scalars(
                        text(
                            """
                            UPDATE ingest.url_work_items AS work
                            SET state = CASE
                                    WHEN stage = 'promote' THEN 'verified'
                                    ELSE 'ready'
                                END,
                                attempt_count = CASE
                                    WHEN :reset_attempts OR state = 'dead' THEN 0
                                    ELSE attempt_count
                                END,
                                available_at = :now,
                                lease_owner = NULL,
                                lease_token = NULL,
                                lease_expires_at = NULL,
                                last_error = '{}'::jsonb,
                                updated_at = :now
                            WHERE state = ANY(CAST(:states AS text[]))
                              AND (
                                  CAST(:run_id AS bigint) IS NULL OR EXISTS (
                                      SELECT 1 FROM ingest.raw_observations AS observation
                                      WHERE observation.url_work_item_id = work.id
                                        AND observation.run_id = CAST(:run_id AS bigint)
                                  )
                              )
                            RETURNING id
                            """
                        ),
                        {
                            "states": list(include_states),
                            "run_id": run_id,
                            "reset_attempts": reset_attempts,
                            "now": now,
                        },
                    )
                ]
        for work_id in set(expired_ids + manual_ids):
            self._refresh_linked_runs(work_id)
        return {"expired": len(expired_ids), "manual": len(manual_ids)}

    def refresh_run(self, run_id: int) -> str:
        now = self._clock()
        with self.engine.begin() as connection:
            counts = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    text(
                        """
                        SELECT work.state, count(DISTINCT work.id) AS count
                        FROM ingest.raw_observations AS observation
                        JOIN ingest.url_work_items AS work
                          ON work.id = observation.url_work_item_id
                        WHERE observation.run_id = :run_id
                        GROUP BY work.state
                        """
                    ),
                    {"run_id": run_id},
                ).mappings()
            }
            observation_stats = connection.execute(
                text(
                    """
                    SELECT count(*) AS observations,
                           count(*) FILTER (
                               WHERE url_work_item_id IS NULL AND payload ? 'ingest_error'
                           ) AS invalid_observations
                    FROM ingest.raw_observations
                    WHERE run_id = :run_id
                    """
                ),
                {"run_id": run_id},
            ).mappings().one()
            invalid_observations = int(observation_stats["invalid_observations"] or 0)
            total = sum(counts.values())
            terminal = sum(counts.get(state, 0) for state in TERMINAL_STATES)
            failures = (
                counts.get("quarantined", 0)
                + counts.get("dead", 0)
                + invalid_observations
            )
            if total == 0:
                status = "partial" if failures else "completed"
                completed_at = now
            elif terminal < total:
                status = "running"
                completed_at = None
            elif failures:
                status = "partial"
                completed_at = now
            else:
                status = "completed"
                completed_at = now
            connection.execute(
                text(
                    """
                    UPDATE ingest.runs
                    SET status = :status,
                        stats = CAST(:stats AS jsonb),
                        completed_at = :completed_at
                    WHERE id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "status": status,
                    "stats": _json(
                        {
                            "observations": int(observation_stats["observations"] or 0),
                            "invalid_observations": invalid_observations,
                            "unique_url_work": total,
                            "states": counts,
                        }
                    ),
                    "completed_at": completed_at,
                },
            )
        return status

    def _refresh_linked_runs(self, work_item_id: int) -> None:
        with self.engine.connect() as connection:
            run_ids = [
                int(value)
                for value in connection.scalars(
                    text(
                        """
                        SELECT DISTINCT run_id
                        FROM ingest.raw_observations
                        WHERE url_work_item_id = :work_item_id
                        """
                    ),
                    {"work_item_id": work_item_id},
                )
            ]
        for run_id in run_ids:
            self.refresh_run(run_id)

    def run_id(self, run_key: str | None, *, source: str | None = None) -> int | None:
        if run_key is None:
            return None
        if source is None:
            raise ValueError("source is required when selecting an ingest run_key")
        with self.engine.connect() as connection:
            value = connection.scalar(
                text(
                    "SELECT id FROM ingest.runs WHERE source = :source AND run_key = :run_key"
                ),
                {"source": source, "run_key": run_key},
            )
        if value is None:
            raise ValueError(f"unknown ingest run: source={source!r} run_key={run_key!r}")
        return int(value)

    def status(self, *, run_id: int | None = None) -> dict[str, Any]:
        with self.engine.connect() as connection:
            run_rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT id, run_key, source, input_uri, input_sha256, status,
                               parser_version, normalizer_version, cursor, stats,
                               started_at, completed_at
                        FROM ingest.runs
                        WHERE (
                            CAST(:run_id AS bigint) IS NULL
                            OR id = CAST(:run_id AS bigint)
                        )
                        ORDER BY id DESC
                        """
                    ),
                    {"run_id": run_id},
                ).mappings()
            ]
            queue = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT work.stage, work.state, count(DISTINCT work.id) AS count
                        FROM ingest.url_work_items AS work
                        WHERE CAST(:run_id AS bigint) IS NULL OR EXISTS (
                            SELECT 1 FROM ingest.raw_observations AS observation
                            WHERE observation.url_work_item_id = work.id
                              AND observation.run_id = CAST(:run_id AS bigint)
                        )
                        GROUP BY work.stage, work.state
                        ORDER BY work.stage, work.state
                        """
                    ),
                    {"run_id": run_id},
                ).mappings()
            ]
            due_retries = int(
                connection.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM ingest.url_work_items AS work
                        WHERE work.state = 'retry'
                          AND work.available_at <= :now
                          AND (
                              CAST(:run_id AS bigint) IS NULL OR EXISTS (
                                  SELECT 1 FROM ingest.raw_observations AS observation
                                  WHERE observation.url_work_item_id = work.id
                                    AND observation.run_id = CAST(:run_id AS bigint)
                              )
                          )
                        """
                    ),
                    {"run_id": run_id, "now": self._clock()},
                )
                or 0
            )
        return {"runs": run_rows, "queue": queue, "due_retries": due_retries}

    def insert_snapshot_candidates(
        self,
        lease: WorkLease,
        snapshot: SourceSnapshot,
        *,
        status: str | None = None,
        quality_flags: Sequence[str] = (),
    ) -> int:
        now = self._clock()
        inserted = 0
        candidate_status = status or ("ready" if snapshot.is_complete else "normalized")
        if candidate_status not in {"normalized", "ready", "quarantined"}:
            raise ValueError(f"unsupported candidate insert status: {candidate_status}")
        normalized_external_ids = [
            _optional_text(job.external_job_id) for job in snapshot.jobs
        ]
        external_id_counts = Counter(normalized_external_ids)
        with self.engine.begin() as connection:
            for index, job in enumerate(snapshot.jobs):
                external_job_id = normalized_external_ids[index]
                discriminator = external_job_id or f"missing-id-{index}"
                if external_job_id is not None and external_id_counts[external_job_id] > 1:
                    discriminator = f"{external_job_id}:duplicate-{index}"
                candidate_key = (
                    f"{_optional_text(snapshot.provider) or 'unknown'}:"
                    f"{_optional_text(snapshot.external_source_id) or 'unknown'}:"
                    f"{discriminator}"
                )
                value = connection.scalar(
                    text(
                        """
                        INSERT INTO ingest.job_candidates (
                            run_id, raw_observation_id, work_item_id, candidate_key,
                            company_source_id, provider, external_source_id, external_job_id,
                            snapshot_complete, payload, status, parser_version,
                            normalizer_version, error, promoted_job_id, title, posting_url,
                            apply_url, description_text, location, department, employment_type,
                            content_hash, source_published_at, source_updated_at,
                            field_provenance, quality_flags, created_at, updated_at
                        ) VALUES (
                            :run_id, :raw_observation_id, :work_item_id, :candidate_key,
                            :company_source_id, :provider, :external_source_id,
                            :external_job_id, :snapshot_complete, CAST(:payload AS jsonb),
                            :status, :parser_version, :normalizer_version, '{}'::jsonb,
                            NULL, :title, :posting_url, :apply_url, :description_text,
                            :location, :department, :employment_type, :content_hash,
                            :source_published_at, :source_updated_at,
                            CAST(:field_provenance AS jsonb), CAST(:quality_flags AS jsonb),
                            :now, :now
                        )
                        ON CONFLICT (run_id, candidate_key) DO UPDATE
                        SET payload = EXCLUDED.payload,
                            raw_observation_id = EXCLUDED.raw_observation_id,
                            work_item_id = EXCLUDED.work_item_id,
                            company_source_id = EXCLUDED.company_source_id,
                            provider = EXCLUDED.provider,
                            external_source_id = EXCLUDED.external_source_id,
                            external_job_id = EXCLUDED.external_job_id,
                            snapshot_complete = EXCLUDED.snapshot_complete,
                            status = EXCLUDED.status,
                            parser_version = EXCLUDED.parser_version,
                            normalizer_version = EXCLUDED.normalizer_version,
                            title = EXCLUDED.title,
                            posting_url = EXCLUDED.posting_url,
                            apply_url = EXCLUDED.apply_url,
                            description_text = EXCLUDED.description_text,
                            location = EXCLUDED.location,
                            department = EXCLUDED.department,
                            employment_type = EXCLUDED.employment_type,
                            content_hash = EXCLUDED.content_hash,
                            source_published_at = EXCLUDED.source_published_at,
                            source_updated_at = EXCLUDED.source_updated_at,
                            field_provenance = EXCLUDED.field_provenance,
                            quality_flags = EXCLUDED.quality_flags,
                            updated_at = EXCLUDED.updated_at
                        RETURNING id
                        """
                    ),
                    {
                        "run_id": lease.run_id,
                        "raw_observation_id": lease.raw_observation_id,
                        "work_item_id": lease.id,
                        "candidate_key": candidate_key,
                        "company_source_id": _optional_int(lease.result.get("company_source_id")),
                        "provider": _optional_text(snapshot.provider),
                        "external_source_id": _optional_text(snapshot.external_source_id),
                        "external_job_id": external_job_id,
                        "snapshot_complete": snapshot.is_complete,
                        "payload": _json(_candidate_payload(job)),
                        "status": candidate_status,
                        "parser_version": lease.parser_version,
                        "normalizer_version": lease.normalizer_version,
                        "title": _optional_text(job.title),
                        "posting_url": _optional_text(job.posting_url),
                        "apply_url": _optional_text(job.apply_url),
                        "description_text": job.description_text,
                        "location": _optional_text(job.location),
                        "department": _optional_text(job.department),
                        "employment_type": _optional_text(job.employment_type),
                        "content_hash": _optional_text(job.content_hash),
                        "source_published_at": job.source_published_at,
                        "source_updated_at": job.source_updated_at,
                        "field_provenance": _json(
                            {
                                field: "complete_provider_snapshot"
                                for field in (
                                    "title",
                                    "posting_url",
                                    "apply_url",
                                    "description_text",
                                    "location",
                                    "department",
                                    "employment_type",
                                )
                                if getattr(job, field) is not None
                            }
                        ),
                        "quality_flags": _json(
                            list(quality_flags)
                            or (
                                []
                                if snapshot.is_complete
                                else ["incomplete_snapshot"]
                            )
                        ),
                        "now": now,
                    },
                )
                inserted += int(value is not None)
        return inserted

    def mark_candidates_promoted(self, lease: WorkLease) -> None:
        now = self._clock()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE ingest.job_candidates AS candidate
                    SET status = 'promoted',
                        promoted_job_id = job.id,
                        updated_at = :now
                    FROM jobs AS job
                    WHERE candidate.run_id = :run_id
                      AND candidate.work_item_id = :work_item_id
                      AND candidate.company_source_id = job.company_source_id
                      AND candidate.external_job_id = job.external_job_id
                    """
                ),
                {"run_id": lease.run_id, "work_item_id": lease.id, "now": now},
            )

    def quarantine_candidates(self, lease: WorkLease, error: Mapping[str, Any]) -> None:
        now = self._clock()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE ingest.job_candidates
                    SET status = 'quarantined',
                        error = CAST(:error AS jsonb),
                        updated_at = :now
                    WHERE run_id = :run_id
                      AND work_item_id = :work_item_id
                      AND status <> 'promoted'
                    """
                ),
                {
                    "run_id": lease.run_id,
                    "work_item_id": lease.id,
                    "error": _json(dict(error)),
                    "now": now,
                },
            )


class UrlFetchHandler:
    """Fetch one URL and publish only a pointer into the existing disk cache."""

    request_headers = {
        "User-Agent": (
            "yc-radar/0.2 (+https://github.com/Daniishkhan/yc-radar; "
            "read-only-staging-fetch)"
        ),
        "Accept": "application/json,text/html,text/plain;q=0.8,*/*;q=0.5",
    }

    def __init__(
        self,
        cache: DiskHttpCache,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 20.0,
        max_body_bytes: int = 5_000_000,
    ) -> None:
        self.cache = cache
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.max_body_bytes = max_body_bytes

    async def __call__(self, lease: WorkLease) -> StageSuccess:
        cached = self.cache.load(lease.normalized_url)
        if cached is not None:
            return self._success(cached, cache_source="disk")
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            headers=self.request_headers,
            follow_redirects=True,
        )
        try:
            async with client.stream(
                "GET",
                lease.normalized_url,
                headers=self.request_headers,
            ) as response:
                if response.status_code != 200:
                    retryable = (
                        response.status_code in {408, 425, 429}
                        or response.status_code >= 500
                    )
                    raise WorkItemError(
                        "http_status",
                        f"HTTP {response.status_code} for {lease.normalized_url}",
                        retryable=retryable,
                        retry_after_seconds=_bounded_retry_after(response),
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = None
                    if declared_length is not None and declared_length > self.max_body_bytes:
                        raise WorkItemError(
                            "body_too_large",
                            f"response exceeds {self.max_body_bytes} bytes",
                            retryable=False,
                        )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.max_body_bytes:
                        raise WorkItemError(
                            "body_too_large",
                            f"response exceeds {self.max_body_bytes} bytes",
                            retryable=False,
                        )
                    body.extend(chunk)
                body_text = bytes(body).decode(
                    response.encoding or "utf-8",
                    errors="replace",
                )
                metadata = {
                    "status_code": response.status_code,
                    "final_url": str(response.url),
                    "content_type": response.headers.get("content-type"),
                    "fetched_at": utc_now().isoformat(),
                    "retryable": False,
                }
            self.cache.store(lease.normalized_url, metadata=metadata, text=body_text)
            cached = self.cache.load(lease.normalized_url)
            if cached is None:
                raise WorkItemError(
                    "cache_publication_failed",
                    "response body was not readable after cache publication",
                    retryable=True,
                )
            return self._success(cached, cache_source="network")
        except WorkItemError:
            raise
        except httpx.HTTPError as exc:
            raise WorkItemError(type(exc).__name__, str(exc), retryable=True) from exc
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _success(cached: Mapping[str, Any], *, cache_source: str) -> StageSuccess:
        cache_key = DiskHttpCache.key_for_url(str(cached["requested_url"]))
        artifact_uri = f"disk-http-cache://{cache_key}/{cached['content_hash']}"
        return StageSuccess(
            next_stage="parse",
            result={
                "fetch": {
                    "final_url": cached.get("final_url"),
                    "cache_source": cache_source,
                    "content_hash": cached["content_hash"],
                }
            },
            artifact_uri=artifact_uri,
            http_status=int(cached.get("status_code") or 200),
            content_type=str(cached.get("content_type") or "") or None,
            content_hash=str(cached["content_hash"]),
        )


class SourceParseHandler:
    """Recognize only URLs owned by configured provider adapters."""

    def __init__(self, providers: JobSourceProviderRegistry | None = None) -> None:
        self.providers = providers or default_job_source_providers()

    def __call__(self, lease: WorkLease) -> StageSuccess:
        fetch_result = lease.result.get("fetch")
        final_url = (
            str(fetch_result.get("final_url") or "")
            if isinstance(fetch_result, Mapping)
            else ""
        )
        detected = self.providers.detect(final_url) if final_url else None
        if detected is None:
            detected = self.providers.detect(lease.normalized_url)
        if detected is None:
            raise WorkItemError(
                "unsupported_source_url",
                "no configured provider adapter recognizes this URL",
                retryable=False,
            )
        return StageSuccess(
            next_stage="enrich",
            result={
                "source": {
                    "provider": detected.provider,
                    "source_kind": detected.source_kind,
                    "external_source_id": detected.external_id,
                    "canonical_url": detected.canonical_url,
                    "detected_from_url": detected.observed_url,
                    "original_observed_url": lease.normalized_url,
                }
            },
        )


class SourceEnrichHandler:
    """Resolve a detected provider source without guessing company identity."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def __call__(self, lease: WorkLease) -> StageSuccess:
        source = lease.result.get("source")
        if not isinstance(source, dict):
            raise WorkItemError(
                "missing_source_evidence",
                "parse stage did not produce provider source evidence",
                retryable=False,
            )
        provider = str(source.get("provider") or "")
        external_source_id = str(source.get("external_source_id") or "")
        canonical_url = str(source.get("canonical_url") or "")
        with self.engine.connect() as connection:
            existing = connection.execute(
                text(
                    """
                    SELECT id, company_id
                    FROM company_sources
                    WHERE provider = :provider AND external_id = :external_id
                    """
                ),
                {"provider": provider, "external_id": external_source_id},
            ).mappings().first()
        if existing is not None:
            company_source_id = int(existing["id"])
            company_id = int(existing["company_id"])
            return StageSuccess(
                next_stage="promote",
                next_state="verified",
                result={
                    "company_id": company_id,
                    "company_source_id": company_source_id,
                },
            )

        proposed_company_id = _optional_int(lease.observation_payload.get("company_id"))
        proposed_name = str(lease.observation_payload.get("company_name") or "").strip()
        return StageSuccess(
            next_stage="promote",
            next_state="ready",
            result={
                "proposed_company_id": proposed_company_id,
                "proposed_company_name": proposed_name or None,
                "canonical_url": canonical_url,
            },
        )


class StagingWorker:
    def __init__(
        self,
        repository: StagingRepository,
        *,
        base_backoff_seconds: float = 5.0,
        max_backoff_seconds: float = 300.0,
    ) -> None:
        self.repository = repository
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds

    async def work(
        self,
        *,
        stage: str,
        handler: StageHandler,
        limit: int,
        lease_seconds: int,
        lease_owner: str,
        run_id: int | None = None,
    ) -> BatchResult:
        leases = self.repository.claim(
            stage=stage,
            limit=limit,
            lease_seconds=lease_seconds,
            lease_owner=lease_owner,
            run_id=run_id,
        )
        counts = {key: 0 for key in ("succeeded", "retried", "quarantined", "dead", "lease_lost")}
        for lease in leases:
            try:
                outcome = handler(lease)
                if isinstance(outcome, Awaitable):
                    outcome = await outcome
                self.repository.complete(lease, outcome)
            except LeaseLostError:
                counts["lease_lost"] += 1
                continue
            except WorkItemError as exc:
                state = self._fail(
                    lease,
                    bounded_error(exc, kind=exc.kind),
                    retryable=exc.retryable,
                    retry_after_seconds=exc.retry_after_seconds,
                )
                counts["retried" if state == "retry" else state] += 1
                continue
            except Exception as exc:
                state = self._fail(lease, bounded_error(exc), retryable=True)
                counts["retried" if state == "retry" else state] += 1
                continue
            counts["succeeded"] += 1
        return BatchResult(claimed=len(leases), **counts)

    def _fail(
        self,
        lease: WorkLease,
        error: dict[str, str],
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> str:
        backoff = (
            min(self.max_backoff_seconds, retry_after_seconds)
            if retry_after_seconds is not None
            else min(
                self.max_backoff_seconds,
                self.base_backoff_seconds * (2 ** max(0, lease.attempt_count - 1)),
            )
        )
        try:
            return self.repository.fail(
                lease,
                error,
                retryable=retryable,
                backoff_seconds=backoff,
            )
        except LeaseLostError:
            return "lease_lost"


class SnapshotPromoter:
    """Promote verified provider sources; incomplete snapshots never touch job state."""

    def __init__(
        self,
        repository: StagingRepository,
        *,
        providers: JobSourceProviderRegistry | None = None,
        sync_service: JobSyncService | None = None,
    ) -> None:
        self.repository = repository
        self.providers = providers or default_job_source_providers()
        self.sync_service = sync_service or JobSyncService(repository.engine)

    async def promote(
        self,
        *,
        limit: int,
        lease_seconds: int,
        lease_owner: str,
        run_id: int | None = None,
    ) -> BatchResult:
        leases = self.repository.claim(
            stage="promote",
            limit=limit,
            lease_seconds=lease_seconds,
            lease_owner=lease_owner,
            run_id=run_id,
        )
        counts = {key: 0 for key in ("succeeded", "retried", "quarantined", "dead", "lease_lost")}
        worker = StagingWorker(self.repository)
        for lease in leases:
            try:
                await self._promote_one(lease)
            except LeaseLostError:
                counts["lease_lost"] += 1
            except WorkItemError as exc:
                error = bounded_error(exc, kind=exc.kind)
                state = worker._fail(
                    lease,
                    error,
                    retryable=exc.retryable,
                    retry_after_seconds=exc.retry_after_seconds,
                )
                if state in {"quarantined", "dead"}:
                    self.repository.quarantine_candidates(lease, error)
                counts["retried" if state == "retry" else state] += 1
            except Exception as exc:
                error = bounded_error(exc)
                state = worker._fail(lease, error, retryable=True)
                if state in {"quarantined", "dead"}:
                    self.repository.quarantine_candidates(lease, error)
                counts["retried" if state == "retry" else state] += 1
            else:
                counts["succeeded"] += 1
        return BatchResult(claimed=len(leases), **counts)

    async def _promote_one(self, lease: WorkLease) -> SyncResult:
        source = lease.result.get("source")
        if not isinstance(source, dict):
            raise WorkItemError(
                "missing_source_evidence",
                "promotion requires parsed provider evidence",
                retryable=False,
            )
        provider = str(source.get("provider") or "")
        external_source_id = str(source.get("external_source_id") or "")
        canonical_url = str(source.get("canonical_url") or "")
        if not provider or not external_source_id or not canonical_url:
            raise WorkItemError(
                "incomplete_source_evidence",
                "provider, external source ID, and canonical URL are required",
                retryable=False,
            )
        adapter = self.providers.adapter_for(provider)
        company_source_id = _optional_int(lease.result.get("company_source_id"))
        if company_source_id is None:
            company_source_id = self._existing_company_source_id(provider, external_source_id)
        run_key = (
            f"ingest:{lease.id}:{lease.parser_version}:{lease.normalizer_version}:"
            f"attempt:{lease.attempt_count}"
        )
        active_started = None
        try:
            started = (
                self.sync_service.start_run(
                    company_source_id=company_source_id,
                    run_key=run_key,
                    provider=provider,
                    adapter_version=adapter.adapter_version,
                )
                if company_source_id is not None
                else None
            )
            if isinstance(started, SyncResult):
                result = started
                promoted_lease = replace(
                    lease,
                    result={**lease.result, "company_source_id": company_source_id},
                )
            else:
                active_started = started
                try:
                    snapshot = await _fetch_snapshot(adapter, external_source_id)
                except Exception as exc:
                    raise WorkItemError(
                        type(exc).__name__,
                        str(exc),
                        retryable=True,
                    ) from exc
                if not self.repository.lease_is_current(lease):
                    raise LeaseLostError(f"lease lost for work item {lease.id}")

                validation_error = _snapshot_validation_error(
                    snapshot,
                    provider=provider,
                    external_source_id=external_source_id,
                )
                if validation_error is not None:
                    self.repository.insert_snapshot_candidates(
                        lease,
                        snapshot,
                        status="normalized",
                        quality_flags=["invalid_snapshot"],
                    )
                    if active_started is not None:
                        validation_exception = ValueError(validation_error)
                        if _snapshot_is_safe_partial(
                            snapshot,
                            provider=provider,
                            external_source_id=external_source_id,
                        ):
                            result = self.sync_service.apply_snapshot(
                                started=active_started,
                                snapshot=snapshot,
                            )
                        else:
                            result = self.sync_service.fail_started_run(
                                started=active_started,
                                error=validation_exception,
                            )
                        active_started = None
                    raise WorkItemError(
                        "incomplete_snapshot",
                        f"{validation_error}; canonical jobs were not changed",
                        retryable=_snapshot_failure_is_retryable(snapshot),
                    )

                if company_source_id is None:
                    company_id = _optional_int(lease.result.get("proposed_company_id"))
                    if company_id is None:
                        confirmed_name = _provider_confirmed_company_name(snapshot)
                        if confirmed_name is None:
                            self.repository.insert_snapshot_candidates(lease, snapshot)
                            self.repository.quarantine_candidates(
                                lease,
                                {
                                    "kind": "company_identity_required",
                                    "message": (
                                        "complete provider snapshot did not expose one consistent "
                                        "company name"
                                    ),
                                },
                            )
                            raise WorkItemError(
                                "company_identity_required",
                                "complete source is valid but canonical company identity is unknown",
                                retryable=False,
                            )
                        company_id = CompanyRegistry(
                            self.repository.engine
                        ).register_provisional_company(
                            name=confirmed_name,
                            requested_slug=external_source_id,
                        ).company_id
                    registration = JobSourceRegistry(self.repository.engine).register_url(
                        company_id=company_id,
                        provider=provider,
                        source_url=canonical_url,
                        discovered_from_url=lease.normalized_url,
                        evidence={
                            "ingest_run_id": lease.run_id,
                            "raw_observation_id": lease.raw_observation_id,
                            "parser_version": lease.parser_version,
                            "snapshot_complete": True,
                        },
                    )
                    company_source_id = registration.company_source_id
                promoted_lease = replace(
                    lease,
                    result={
                        **lease.result,
                        "company_source_id": company_source_id,
                    },
                )
                self.repository.insert_snapshot_candidates(promoted_lease, snapshot)
                if active_started is None:
                    started = self.sync_service.start_run(
                        company_source_id=company_source_id,
                        run_key=run_key,
                        provider=provider,
                        adapter_version=adapter.adapter_version,
                    )
                    if isinstance(started, SyncResult):
                        result = started
                    else:
                        active_started = started
                        result = self.sync_service.apply_snapshot(
                            started=active_started,
                            snapshot=snapshot,
                        )
                        active_started = None
                else:
                    result = self.sync_service.apply_snapshot(
                        started=active_started,
                        snapshot=snapshot,
                    )
                    active_started = None
            if result.status != "completed" or not result.is_complete_scan:
                raise WorkItemError(
                    "incomplete_snapshot",
                    f"provider snapshot finished as {result.status}; canonical jobs were not changed",
                    retryable=result.status == "failed",
                )
            if not self.repository.lease_is_current(promoted_lease):
                raise LeaseLostError(f"lease lost for work item {lease.id}")
            self.repository.mark_candidates_promoted(promoted_lease)
            self.repository.complete(
                promoted_lease,
                StageSuccess(
                    next_stage="done",
                    next_state="promoted",
                    result={
                        "company_source_id": company_source_id,
                        "sync_run_id": result.run_id,
                        "sync_run_key": result.run_key,
                    },
                ),
            )
            return result
        except BaseException as exc:
            if active_started is not None:
                self._finalize_started_after_exception(active_started, exc)
            raise

    def _finalize_started_after_exception(self, started: Any, error: BaseException) -> None:
        existing = self.sync_service.existing_run_result(
            company_source_id=started.company_source_id,
            run_key=started.run_key,
        )
        if existing is not None and existing.status != "running":
            return
        exception = error if isinstance(error, Exception) else RuntimeError(str(error))
        self.sync_service.fail_started_run(started=started, error=exception)

    def _existing_company_source_id(self, provider: str, external_source_id: str) -> int | None:
        with self.repository.engine.connect() as connection:
            value = connection.scalar(
                text(
                    """
                    SELECT id FROM company_sources
                    WHERE provider = :provider AND external_id = :external_source_id
                    """
                ),
                {"provider": provider, "external_source_id": external_source_id},
            )
        return int(value) if value is not None else None


class FunnelReporter:
    """Compute the compact discovery-to-action funnel without adding reporting tables."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def report(self, *, run_id: int | None = None) -> dict[str, int]:
        with self.engine.connect() as connection:
            observations = int(
                connection.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM ingest.raw_observations
                        WHERE (
                            CAST(:run_id AS bigint) IS NULL
                            OR run_id = CAST(:run_id AS bigint)
                        )
                        """
                    ),
                    {"run_id": run_id},
                )
                or 0
            )
            unique_urls = int(
                connection.scalar(
                    text(
                        """
                        SELECT count(DISTINCT work.normalized_url)
                        FROM ingest.url_work_items AS work
                        WHERE CAST(:run_id AS bigint) IS NULL OR EXISTS (
                            SELECT 1 FROM ingest.raw_observations AS observation
                            WHERE observation.url_work_item_id = work.id
                              AND observation.run_id = CAST(:run_id AS bigint)
                        )
                        """
                    ),
                    {"run_id": run_id},
                )
                or 0
            )
            source_counts = connection.execute(
                text(
                    """
                    SELECT
                        count(DISTINCT CASE
                            WHEN result->>'company_source_id' ~ '^[0-9]+$'
                             AND state IN ('verified', 'promoted')
                            THEN (result->>'company_source_id')::bigint
                        END) AS verified_sources,
                        count(DISTINCT CASE
                            WHEN result->>'company_source_id' ~ '^[0-9]+$'
                             AND state = 'promoted'
                            THEN (result->>'company_source_id')::bigint
                        END) AS promoted_sources
                    FROM ingest.url_work_items AS work
                    WHERE CAST(:run_id AS bigint) IS NULL OR EXISTS (
                        SELECT 1 FROM ingest.raw_observations AS observation
                        WHERE observation.url_work_item_id = work.id
                          AND observation.run_id = CAST(:run_id AS bigint)
                    )
                    """
                ),
                {"run_id": run_id},
            ).mappings().one()
            if run_id is None:
                jobs = [
                    dict(row)
                    for row in connection.execute(
                        text(
                            """
                            SELECT title, location, description_text, department,
                                   employment_type, structured_evidence
                            FROM jobs
                            WHERE status = 'active'
                            """
                        )
                    ).mappings()
                ]
            else:
                jobs = [
                    dict(row)
                    for row in connection.execute(
                        text(
                            """
                            WITH linked_sources AS (
                                SELECT DISTINCT
                                       (work.result->>'company_source_id')::bigint AS id
                                FROM ingest.url_work_items AS work
                                JOIN ingest.raw_observations AS observation
                                  ON observation.url_work_item_id = work.id
                                WHERE observation.run_id = :run_id
                                  AND work.result->>'company_source_id' ~ '^[0-9]+$'
                                UNION
                                SELECT DISTINCT candidate.company_source_id
                                FROM ingest.job_candidates AS candidate
                                WHERE candidate.run_id = :run_id
                                  AND candidate.company_source_id IS NOT NULL
                            )
                            SELECT DISTINCT job.title, job.location, job.description_text,
                                   job.department, job.employment_type, job.structured_evidence
                            FROM jobs AS job
                            JOIN linked_sources ON linked_sources.id = job.company_source_id
                            WHERE job.status = 'active'
                            """
                        ),
                        {"run_id": run_id},
                    ).mappings()
                ]

        engineering_jobs: list[dict[str, Any]] = []
        for job in jobs:
            context = " ".join(
                str(job.get(field) or "")
                for field in ("location", "description_text", "department", "employment_type")
            )
            role = classify_role_text(str(job.get("title") or ""), context)
            if role.status in {"strong", "possible"}:
                engineering_jobs.append(job)
        remote = {"pakistan_explicit": 0, "global_explicit": 0, "remote_unclear": 0}
        for job in engineering_jobs:
            category = classify_remote_eligibility(job).status
            if category in remote:
                remote[category] += 1
        return {
            "observations": observations,
            "unique_url_work": unique_urls,
            "verified_sources": int(source_counts["verified_sources"] or 0),
            "promoted_sources": int(source_counts["promoted_sources"] or 0),
            "active_jobs": len(jobs),
            "engineering_jobs": len(engineering_jobs),
            **remote,
        }


def _snapshot_validation_error(
    snapshot: SourceSnapshot,
    *,
    provider: str,
    external_source_id: str,
) -> str | None:
    if snapshot.provider != provider:
        return "snapshot provider does not match parsed source"
    if snapshot.external_source_id != external_source_id:
        return "snapshot external source ID does not match parsed source"
    if not snapshot.is_complete:
        return "provider did not return a complete snapshot"
    if snapshot.errors:
        return "provider reported snapshot errors"
    if snapshot.http_status not in {None, 200}:
        return f"provider returned HTTP {snapshot.http_status}"
    external_ids = [job.external_job_id.strip() for job in snapshot.jobs]
    if any(not value for value in external_ids) or len(external_ids) != len(set(external_ids)):
        return "snapshot job identities are missing or duplicated"
    if any(not job.title.strip() for job in snapshot.jobs):
        return "snapshot contains a job without a title"
    return None


def _snapshot_is_safe_partial(
    snapshot: SourceSnapshot,
    *,
    provider: str,
    external_source_id: str,
) -> bool:
    """Return true only when JobSyncService is guaranteed not to apply this snapshot."""
    if snapshot.provider != provider or snapshot.external_source_id != external_source_id:
        return False
    if snapshot.is_complete and not snapshot.errors:
        return False
    external_ids = [job.external_job_id.strip() for job in snapshot.jobs]
    if any(not value for value in external_ids) or len(external_ids) != len(set(external_ids)):
        return False
    return all(job.title.strip() for job in snapshot.jobs)


def _snapshot_failure_is_retryable(snapshot: SourceSnapshot) -> bool:
    if snapshot.http_status in {408, 425, 429}:
        return True
    if snapshot.http_status is None or snapshot.http_status >= 500:
        return True
    retryable_kinds = {
        "connecterror",
        "connecttimeout",
        "networkerror",
        "ratelimit",
        "readerror",
        "readtimeout",
        "remoteprotocolerror",
    }
    return any(str(error.get("kind") or "").casefold() in retryable_kinds for error in snapshot.errors)


def _bounded_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return min(300.0, seconds) if seconds > 0 else None


def _provider_confirmed_company_name(snapshot: SourceSnapshot) -> str | None:
    names: dict[str, str] = {}

    def collect(payload: Mapping[str, Any]) -> None:
        for key in ("company_name", "companyName", "organization_name", "organizationName"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                names.setdefault(value.strip().casefold(), value.strip())
        company = payload.get("company")
        if isinstance(company, str) and company.strip():
            names.setdefault(company.strip().casefold(), company.strip())
        elif isinstance(company, Mapping):
            value = company.get("name")
            if isinstance(value, str) and value.strip():
                names.setdefault(value.strip().casefold(), value.strip())

    collect(snapshot.request_metadata)
    for job in snapshot.jobs:
        collect(job.raw_payload)
    return next(iter(names.values())) if len(names) == 1 else None


async def _fetch_snapshot(adapter: JobSourceAdapter, external_source_id: str) -> SourceSnapshot:
    return await adapter.fetch_snapshot(external_source_id)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _require_bounded_text(value: str, *, field: str, max_bytes: int) -> None:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds {max_bytes} bytes")


def _bounded_observation_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_payload = dict(payload)
    encoded = _json(raw_payload).encode("utf-8")
    if len(encoded) <= MAX_RAW_OBSERVATION_PAYLOAD_BYTES:
        return raw_payload

    compact: dict[str, Any] = {
        "payload_oversized": True,
        "payload_size_bytes": len(encoded),
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
    }
    if company_id := raw_payload.get("company_id"):
        compact["company_id"] = (
            company_id if isinstance(company_id, int) else str(company_id)[:128]
        )
    if company_name := _optional_text(raw_payload.get("company_name")):
        compact["company_name"] = company_name[:1000]
    return compact


def _candidate_payload(job: NormalizedJob) -> dict[str, Any]:
    """Keep staging JSON compact; large text and provider payloads live in typed/core fields."""
    compact = job.model_dump(
        mode="json",
        exclude={"description_html", "description_text", "raw_payload"},
    )
    raw_payload = _json(job.raw_payload).encode()
    compact["raw_payload_sha256"] = hashlib.sha256(raw_payload).hexdigest()
    return compact


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def run_async(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


def default_cache_path() -> Path:
    return Path("data/local/cache/staging_http")


def iter_observations(rows: Iterable[Mapping[str, Any]]) -> Iterable[Observation]:
    for index, row in enumerate(rows, start=1):
        url = str(row.get("url") or row.get("observed_url") or "").strip()
        key = str(row.get("observation_key") or "").strip() or None
        observed_at = _parse_datetime(row.get("observed_at"), row_number=index)
        try:
            priority = int(row.get("priority") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"row {index} has invalid priority") from exc
        payload = {
            str(name): value
            for name, value in row.items()
            if name
            not in {"url", "observed_url", "observation_key", "observed_at", "priority"}
            and value not in (None, "")
        }
        yield Observation(
            url=url,
            observation_key=key,
            payload=payload,
            observed_at=observed_at,
            priority=priority,
        )


def observations_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[Observation]:
    return list(iter_observations(rows))


def _parse_datetime(value: Any, *, row_number: int) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"row {row_number} has invalid observed_at") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
