from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Connection, Engine

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot, SyncResult
from yc_radar.services.job_repository import JobRepository


class RunKeyReuseError(RuntimeError):
    """Raised when a non-completed source run key is reused."""

    def __init__(self, career_source_id: int, run_key: str, status: str) -> None:
        super().__init__(
            f"Run key {run_key!r} for source {career_source_id} already has status {status!r}. "
            "Use a new run key for a new attempt."
        )
        self.career_source_id = career_source_id
        self.run_key = run_key
        self.status = status


@dataclass(frozen=True)
class StartedSyncRun:
    """A committed running attempt that can survive a fetch interruption."""

    career_source_id: int
    run_id: int
    run_key: str


class JobSyncService:
    """Apply only complete source snapshots to the canonical job lifecycle."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = JobRepository(engine)
        self._clock = clock or (lambda: datetime.now(UTC))

    def start_run(
        self,
        *,
        career_source_id: int,
        run_key: str,
        provider: str,
        adapter_version: str,
    ) -> StartedSyncRun | SyncResult:
        """Commit a running run before any provider request is made.

        A completed run key is a safe idempotent replay. A failed, partial, or interrupted run
        key is never silently reused: callers must choose a new key for a fresh attempt.
        """
        now = self._clock()
        with self.repository.engine.begin() as connection:
            source = self.repository.get_career_source(connection, career_source_id)
            if source["provider"] != provider:
                raise ValueError("adapter provider differs from career source provider")
            existing_run = self.repository.get_run(connection, career_source_id, run_key)
            if existing_run is not None:
                if existing_run["status"] == "completed":
                    return self._result_from_run(existing_run, idempotent_replay=True)
                raise RunKeyReuseError(career_source_id, run_key, str(existing_run["status"]))
            run_id = self.repository.create_run(
                connection,
                self._initial_run_values(
                    career_source_id=career_source_id,
                    run_key=run_key,
                    provider=provider,
                    adapter_version=adapter_version,
                    now=now,
                ),
            )
        return StartedSyncRun(
            career_source_id=career_source_id,
            run_id=run_id,
            run_key=run_key,
        )

    def existing_run_result(self, *, career_source_id: int, run_key: str) -> SyncResult | None:
        """Return a committed run result for CLI preflight without starting a provider fetch."""
        with self.repository.engine.connect() as connection:
            existing_run = self.repository.get_run(connection, career_source_id, run_key)
        if existing_run is None:
            return None
        return self._result_from_run(existing_run, idempotent_replay=True)

    def interrupt_running_run(
        self,
        *,
        career_source_id: int,
        run_key: str,
        reason: str = "worker restarted before the source attempt completed",
    ) -> SyncResult | None:
        """Close a durable orphaned attempt before a resumed batch creates another one."""
        now = self._clock()
        with self.repository.engine.begin() as connection:
            run = self.repository.get_run(connection, career_source_id, run_key)
            if run is None:
                return None
            if run["status"] == "running":
                self.repository.finalize_run(
                    connection,
                    int(run["id"]),
                    {
                        "status": "failed",
                        "errors_count": 1,
                        "errors": [{"kind": "interrupted", "message": reason[:500]}],
                        "completed_at": now,
                    },
                )
                self.repository.update_career_source_sync_state(
                    connection,
                    career_source_id,
                    status="failed",
                    now=now,
                )
                run = self.repository.get_run(connection, career_source_id, run_key)
                assert run is not None
            return self._result_from_run(run, idempotent_replay=True)

    def sync_snapshot(
        self,
        *,
        career_source_id: int,
        run_key: str,
        snapshot: SourceSnapshot,
    ) -> SyncResult:
        """Compatibility helper for callers that already hold a fetched snapshot."""
        started = self.start_run(
            career_source_id=career_source_id,
            run_key=run_key,
            provider=snapshot.provider,
            adapter_version=snapshot.adapter_version,
        )
        if isinstance(started, SyncResult):
            return started
        return self.apply_snapshot(started=started, snapshot=snapshot)

    def apply_snapshot(self, *, started: StartedSyncRun, snapshot: SourceSnapshot) -> SyncResult:
        """Finalize a committed run and atomically apply a valid complete snapshot."""
        now = self._clock()
        try:
            with self.repository.engine.begin() as connection:
                source = self.repository.get_career_source(connection, started.career_source_id)
                run = self.repository.get_run_by_id(connection, started.run_id)
                if run is None or run["career_source_id"] != started.career_source_id:
                    raise ValueError("source sync run does not belong to career source")
                if run["run_key"] != started.run_key:
                    raise ValueError("source sync run key does not match")
                if run["status"] == "completed":
                    return self._result_from_run(run, idempotent_replay=True)
                if run["status"] != "running":
                    raise RunKeyReuseError(
                        started.career_source_id, started.run_key, str(run["status"])
                    )
                validation_errors = self._snapshot_errors(source, snapshot)
                complete = snapshot.is_complete and not snapshot.errors and not validation_errors
                self.repository.finalize_run(
                    connection,
                    started.run_id,
                    self._snapshot_run_values(snapshot, is_complete_scan=complete),
                )
                if not complete:
                    errors = [*snapshot.errors, *validation_errors]
                    status = "partial" if snapshot.http_status == 200 else "failed"
                    return self._finalize_without_applying(
                        connection,
                        source_id=started.career_source_id,
                        run_id=started.run_id,
                        run_key=started.run_key,
                        snapshot=snapshot,
                        now=now,
                        status=status,
                        errors=errors,
                    )
                return self._apply_complete_snapshot(
                    connection,
                    source=source,
                    run_id=started.run_id,
                    run_key=started.run_key,
                    snapshot=snapshot,
                    now=now,
                )
        except RunKeyReuseError:
            raise
        except Exception as exc:
            return self._finalize_started_run_failure(
                started=started,
                snapshot=snapshot,
                error=exc,
                now=now,
            )

    def fail_started_run(self, *, started: StartedSyncRun, error: Exception) -> SyncResult:
        """Persist a handled provider-fetch failure after a run was made durable."""
        return self._finalize_started_run_failure(
            started=started,
            snapshot=None,
            error=error,
            now=self._clock(),
        )

    @staticmethod
    def _snapshot_errors(source: dict[str, Any], snapshot: SourceSnapshot) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        if source["provider"] != snapshot.provider:
            errors.append({"kind": "provider_mismatch", "message": "snapshot provider differs"})
        if source["external_source_id"] != snapshot.external_source_id:
            errors.append({"kind": "source_mismatch", "message": "snapshot source differs"})
        ids = [job.external_job_id for job in snapshot.jobs]
        if len(ids) != len(set(ids)):
            errors.append({"kind": "duplicate_external_job_id", "message": "duplicate job IDs"})
        return errors

    @staticmethod
    def _initial_run_values(
        *,
        career_source_id: int,
        run_key: str,
        provider: str,
        adapter_version: str,
        now: datetime,
    ) -> dict[str, Any]:
        return {
            "career_source_id": career_source_id,
            "run_key": run_key,
            "provider": provider,
            "adapter_version": adapter_version,
            "status": "running",
            "is_complete_scan": False,
            "http_status": None,
            "jobs_fetched": 0,
            "jobs_added": 0,
            "jobs_updated": 0,
            "jobs_unchanged": 0,
            "jobs_missed": 0,
            "jobs_closed": 0,
            "jobs_reactivated": 0,
            "errors_count": 0,
            "errors": [],
            "request_metadata": {},
            "started_at": now,
        }

    @staticmethod
    def _snapshot_run_values(
        snapshot: SourceSnapshot, *, is_complete_scan: bool
    ) -> dict[str, Any]:
        return {
            "provider": snapshot.provider,
            "adapter_version": snapshot.adapter_version,
            "is_complete_scan": is_complete_scan,
            "http_status": snapshot.http_status,
            "jobs_fetched": len(snapshot.jobs),
            "request_metadata": snapshot.request_metadata,
        }

    def _finalize_without_applying(
        self,
        connection: Connection,
        *,
        source_id: int,
        run_id: int,
        run_key: str,
        snapshot: SourceSnapshot,
        now: datetime,
        status: str,
        errors: list[dict[str, str]],
    ) -> SyncResult:
        self.repository.finalize_run(
            connection,
            run_id,
            {
                "status": status,
                "errors_count": len(errors),
                "errors": errors,
                "completed_at": now,
            },
        )
        self.repository.update_career_source_sync_state(
            connection, source_id, status=status, now=now
        )
        return SyncResult(
            career_source_id=source_id,
            run_id=run_id,
            run_key=run_key,
            status=status,
            is_complete_scan=False,
            jobs_fetched=len(snapshot.jobs),
            errors_count=len(errors),
        )

    def _finalize_started_run_failure(
        self,
        *,
        started: StartedSyncRun,
        snapshot: SourceSnapshot | None,
        error: Exception,
        now: datetime,
    ) -> SyncResult:
        error_payload = [{"kind": type(error).__name__, "message": str(error)[:500]}]
        with self.repository.engine.begin() as connection:
            run = self.repository.get_run_by_id(connection, started.run_id)
            if run is None or run["career_source_id"] != started.career_source_id:
                raise error
            if run["status"] == "completed":
                return self._result_from_run(run, idempotent_replay=True)
            if run["status"] != "running":
                raise RunKeyReuseError(
                    started.career_source_id, started.run_key, str(run["status"])
                )
            values: dict[str, Any] = {
                "status": "failed",
                "is_complete_scan": False,
                "errors_count": len(error_payload),
                "errors": error_payload,
                "completed_at": now,
            }
            jobs_fetched = 0
            if snapshot is not None:
                values.update(self._snapshot_run_values(snapshot, is_complete_scan=False))
                jobs_fetched = len(snapshot.jobs)
            self.repository.finalize_run(connection, started.run_id, values)
            self.repository.update_career_source_sync_state(
                connection, started.career_source_id, status="failed", now=now
            )
        return SyncResult(
            career_source_id=started.career_source_id,
            run_id=started.run_id,
            run_key=started.run_key,
            status="failed",
            is_complete_scan=False,
            jobs_fetched=jobs_fetched,
            errors_count=len(error_payload),
        )

    def _apply_complete_snapshot(
        self,
        connection: Connection,
        *,
        source: dict[str, Any],
        run_id: int,
        run_key: str,
        snapshot: SourceSnapshot,
        now: datetime,
    ) -> SyncResult:
        source_id = int(source["id"])
        current_jobs = self.repository.source_jobs_for_update(connection, source_id)
        returned_ids = {job.external_job_id for job in snapshot.jobs}
        counters = {
            "jobs_added": 0,
            "jobs_updated": 0,
            "jobs_unchanged": 0,
            "jobs_missed": 0,
            "jobs_closed": 0,
            "jobs_reactivated": 0,
        }
        for job in snapshot.jobs:
            existing = current_jobs.get(job.external_job_id)
            if existing is None:
                job_id = self.repository.insert_job(
                    connection,
                    self._new_current_values(source, job, now),
                )
                version_id = self.repository.insert_version(
                    connection, self._version_values(job_id, run_id, job, now)
                )
                self.repository.update_job(connection, job_id, {"current_version_id": version_id})
                self.repository.insert_observation(
                    connection,
                    self._observation_values(
                        job_id,
                        run_id,
                        "seen",
                        "active",
                        "active",
                        job.content_hash,
                        version_id,
                        now,
                        job,
                    ),
                )
                counters["jobs_added"] += 1
                continue
            self._apply_seen_job(
                connection,
                existing=existing,
                job=job,
                run_id=run_id,
                now=now,
                counters=counters,
            )
        for external_job_id, existing in current_jobs.items():
            if external_job_id not in returned_ids:
                self._apply_missed_job(connection, existing, run_id, now, counters)
        self.repository.finalize_run(
            connection,
            run_id,
            {
                "status": "completed",
                "jobs_added": counters["jobs_added"],
                "jobs_updated": counters["jobs_updated"],
                "jobs_unchanged": counters["jobs_unchanged"],
                "jobs_missed": counters["jobs_missed"],
                "jobs_closed": counters["jobs_closed"],
                "jobs_reactivated": counters["jobs_reactivated"],
                "errors_count": 0,
                "errors": [],
                "completed_at": now,
            },
        )
        self.repository.update_career_source_sync_state(
            connection, source_id, status="completed", now=now
        )
        return SyncResult(
            career_source_id=source_id,
            run_id=run_id,
            run_key=run_key,
            status="completed",
            is_complete_scan=True,
            jobs_fetched=len(snapshot.jobs),
            **counters,
        )

    def _apply_seen_job(
        self,
        connection: Connection,
        *,
        existing: dict[str, Any],
        job: NormalizedJob,
        run_id: int,
        now: datetime,
        counters: dict[str, int],
    ) -> None:
        job_id = int(existing["id"])
        before = str(existing["status"])
        changed = existing["content_hash"] != job.content_hash
        reactivated = before == "closed"
        values = self._present_current_values(job, now)
        if changed or reactivated:
            values["last_changed_at"] = now
        if reactivated:
            values.update(status="active", closed_at=None)
            counters["jobs_reactivated"] += 1
        version_id: int | None = None
        if changed:
            version_id = self.repository.insert_version(
                connection, self._version_values(job_id, run_id, job, now)
            )
            values["current_version_id"] = version_id
            counters["jobs_updated"] += 1
        else:
            counters["jobs_unchanged"] += 1
        self.repository.update_job(connection, job_id, values)
        self.repository.insert_observation(
            connection,
            self._observation_values(
                job_id,
                run_id,
                "seen",
                before,
                "active",
                job.content_hash,
                version_id,
                now,
                job,
            ),
        )

    def _apply_missed_job(
        self,
        connection: Connection,
        existing: dict[str, Any],
        run_id: int,
        now: datetime,
        counters: dict[str, int],
    ) -> None:
        job_id = int(existing["id"])
        before = str(existing["status"])
        after = before
        values: dict[str, Any] = {"updated_at": now}
        if before == "active":
            misses = int(existing["consecutive_complete_misses"]) + 1
            values["consecutive_complete_misses"] = misses
            if misses >= 2:
                after = "closed"
                values.update(status=after, closed_at=now, last_changed_at=now)
                counters["jobs_closed"] += 1
            self.repository.update_job(connection, job_id, values)
        self.repository.insert_observation(
            connection,
            self._observation_values(
                job_id, run_id, "missed", before, after, existing["content_hash"], None, now, None
            ),
        )
        counters["jobs_missed"] += 1

    @staticmethod
    def _new_current_values(
        source: dict[str, Any], job: NormalizedJob, now: datetime
    ) -> dict[str, Any]:
        return {
            "career_source_id": source["id"],
            "company_id": source["company_id"],
            "provider": source["provider"],
            "external_job_id": job.external_job_id,
            "status": "active",
            "consecutive_complete_misses": 0,
            "content_hash": job.content_hash,
            "current_version_id": None,
            "first_seen_at": now,
            "last_seen_at": now,
            "last_changed_at": now,
            "closed_at": None,
            "created_at": now,
            "updated_at": now,
            **JobSyncService._public_current_fields(job),
        }

    @staticmethod
    def _present_current_values(job: NormalizedJob, now: datetime) -> dict[str, Any]:
        return {
            "status": "active",
            "consecutive_complete_misses": 0,
            "last_seen_at": now,
            "closed_at": None,
            "updated_at": now,
            **JobSyncService._public_current_fields(job),
        }

    @staticmethod
    def _public_current_fields(job: NormalizedJob) -> dict[str, Any]:
        return {
            "title": job.title,
            "posting_url": job.posting_url,
            "apply_url": job.apply_url,
            "location": job.location,
            "department": job.department,
            "employment_type": job.employment_type,
            "content_hash": job.content_hash,
            "source_published_at": job.source_published_at,
            "source_updated_at": job.source_updated_at,
        }

    @staticmethod
    def _version_values(
        job_id: int, run_id: int, job: NormalizedJob, now: datetime
    ) -> dict[str, Any]:
        return {
            "job_posting_id": job_id,
            "source_sync_run_id": run_id,
            "content_hash": job.content_hash,
            "title": job.title,
            "description_html": job.description_html,
            "description_text": job.description_text,
            "location": job.location,
            "department": job.department,
            "employment_type": job.employment_type,
            "posting_url": job.posting_url,
            "apply_url": job.apply_url,
            "source_published_at": job.source_published_at,
            "source_updated_at": job.source_updated_at,
            "raw_payload": job.raw_payload,
            "observed_at": now,
            "created_at": now,
        }

    @staticmethod
    def _observation_values(
        job_id: int,
        run_id: int,
        observation_kind: str,
        status_before: str,
        status_after: str,
        content_hash: str | None,
        version_id: int | None,
        now: datetime,
        job: NormalizedJob | None,
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        if job is not None:
            evidence = {"external_job_id": job.external_job_id, "title": job.title}
        return {
            "job_posting_id": job_id,
            "source_sync_run_id": run_id,
            "observation_kind": observation_kind,
            "status_before": status_before,
            "status_after": status_after,
            "content_hash": content_hash,
            "job_posting_version_id": version_id,
            "observed_at": now,
            "evidence": evidence,
        }

    @staticmethod
    def _result_from_run(run: dict[str, Any], *, idempotent_replay: bool) -> SyncResult:
        return SyncResult(
            career_source_id=int(run["career_source_id"]),
            run_id=int(run["id"]),
            run_key=str(run["run_key"]),
            status=str(run["status"]),
            is_complete_scan=bool(run["is_complete_scan"]),
            jobs_fetched=int(run["jobs_fetched"]),
            jobs_added=int(run["jobs_added"]),
            jobs_updated=int(run["jobs_updated"]),
            jobs_unchanged=int(run["jobs_unchanged"]),
            jobs_missed=int(run["jobs_missed"]),
            jobs_closed=int(run["jobs_closed"]),
            jobs_reactivated=int(run["jobs_reactivated"]),
            errors_count=int(run["errors_count"]),
            idempotent_replay=idempotent_replay,
        )
