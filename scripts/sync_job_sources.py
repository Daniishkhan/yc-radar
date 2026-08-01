#!/usr/bin/env python3
"""Discover and synchronise supported public job-board sources sequentially."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from yc_radar.adapters.base import JobSourceAdapter
from yc_radar.domain.job_sources import SyncResult
from yc_radar.services.database import create_schema, engine_from_url
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_source_registry import (
    JobSourceProviderRegistry,
    default_job_source_providers,
)
from yc_radar.services.job_sync_service import JobSyncService, RunKeyReuseError
from yc_radar.services.run_status import (
    read_status,
    stage_checkpoint,
    stage_finished,
    stage_started,
    write_status,
)
from yc_radar.services.source_discovery import discover_job_sources


TERMINAL_CHECKPOINT_STATES = frozenset({"completed", "terminal_failed"})
PERMANENT_HTTP_FAILURES = frozenset({400, 401, 403, 404, 410})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover or sync read-only public job sources.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    supported = default_job_source_providers().providers
    discover = subcommands.add_parser(
        "discover",
        help="Register supported ATS/feed sources from all company career-page evidence.",
    )
    discover.add_argument("--provider", choices=supported)
    discover.add_argument("--status-file", type=Path, help="Atomic local stage-status JSON output.")
    sync = subcommands.add_parser("sync", help="Fetch and apply configured sources.")
    sync.add_argument("--status-file", type=Path, help="Atomic local stage-status JSON output.")
    sync.add_argument(
        "--provider",
        choices=supported,
        help="Optionally sync one provider; defaults to every registered provider.",
    )
    source_scope = sync.add_mutually_exclusive_group()
    source_scope.add_argument("--company-id", type=int)
    source_scope.add_argument(
        "--source-id",
        dest="source_ids",
        action="append",
        type=int,
        help="Sync one career source ID; repeat to select multiple newly registered sources.",
    )
    source_scope.add_argument(
        "--min-source-id",
        type=int,
        help="Sync sources at or above this ID; useful after a bulk registration run.",
    )
    sync.add_argument("--limit", type=int)
    sync.add_argument(
        "--delay-seconds",
        type=non_negative_float,
        default=1.0,
        help="Polite delay between source requests; sources are always fetched sequentially.",
    )
    sync.add_argument(
        "--run-key",
        help=(
            "Optional logical batch key. With --checkpoint-file, retries receive distinct "
            "audited attempt keys under this batch."
        ),
    )
    sync.add_argument(
        "--checkpoint-file",
        type=Path,
        help=(
            "Durable batch manifest. Restarts reuse the original source set, skip completed "
            "sources, and retry failed/interrupted sources."
        ),
    )
    sync.add_argument(
        "--max-attempts",
        type=positive_int,
        default=4,
        help="Maximum process-level attempts for each source in a checkpointed batch.",
    )
    return parser.parse_args()


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main() -> None:
    args = parse_args()
    status_file = getattr(args, "status_file", None)
    stage_name = "ats_registration" if args.command == "discover" else "ats_sync"
    status = stage_started(stage_name)
    write_status(status_file, status)
    try:
        engine = engine_from_url()
        create_schema(engine)
        if args.command == "discover":
            result = discover_job_sources(engine, provider=args.provider)
            scope = args.provider or "supported job"
            print(
                f"Registered {result['registered']} new {scope} sources; "
                f"already registered {result['existing']}; skipped {result['skipped']}; "
                f"conflicts {len(result['conflicts'])}."
            )
            for conflict in result["conflicts"]:
                print(f"Conflict: {conflict}")
            write_status(
                status_file,
                stage_finished(
                    status,
                    state="completed",
                    selected=result["registered"] + result["existing"] + result["skipped"],
                    processed=result["registered"] + result["existing"] + result["skipped"],
                    succeeded=result["registered"] + result["existing"],
                    failed=0,
                    conflicts=len(result["conflicts"]),
                ),
            )
            return
        results = asyncio.run(sync_sources(engine, args))
        unsuccessful = [result for result in results if result.status != "completed"]
        batch_checkpoint = read_status(getattr(args, "checkpoint_file", None))
        checkpoint_summary = (
            summarize_sync_checkpoint(batch_checkpoint, max_attempts=args.max_attempts)
            if batch_checkpoint
            else None
        )
        batch_incomplete = bool(checkpoint_summary and checkpoint_summary["retryable"])
        selected = int(checkpoint_summary["selected"]) if checkpoint_summary else len(results)
        processed = int(checkpoint_summary["processed"]) if checkpoint_summary else len(results)
        succeeded = (
            int(checkpoint_summary["succeeded"])
            if checkpoint_summary
            else len(results) - len(unsuccessful)
        )
        failed = int(checkpoint_summary["failed"]) if checkpoint_summary else len(unsuccessful)
        print(
            f"Processed {len(results)} {args.provider or 'supported'} sources; "
            f"non-completed runs: {len(unsuccessful)}; batch_incomplete={batch_incomplete}; "
            f"checkpoint_succeeded={succeeded}; checkpoint_failed={failed}."
        )
        for result in results:
            print(
                f"source={result.career_source_id} status={result.status} "
                f"added={result.jobs_added} updated={result.jobs_updated} closed={result.jobs_closed}"
            )
        write_status(
            status_file,
            stage_finished(
                status,
                state="completed" if not batch_incomplete and not failed else "partial",
                selected=selected,
                processed=processed,
                succeeded=succeeded,
                failed=failed,
                retryable=int(checkpoint_summary["retryable"]) if checkpoint_summary else 0,
                terminal_failures=int(checkpoint_summary["terminal_failures"])
                if checkpoint_summary
                else 0,
                exhausted_failures=int(checkpoint_summary["exhausted_failures"])
                if checkpoint_summary
                else 0,
                source_run_statuses={
                    result.status: sum(item.status == result.status for item in results)
                    for result in results
                },
            ),
        )
        # A checkpointed batch is process-complete once every source either succeeded,
        # failed permanently, or exhausted its bounded attempts. The source failures
        # remain explicit in the checkpoint/status and never apply lifecycle changes,
        # but they must not make systemd restart an empty batch forever.
        if batch_incomplete or (batch_checkpoint is None and unsuccessful):
            raise SystemExit(1)
    except Exception as exc:
        write_status(status_file, stage_finished(status, state="failed", error=exc))
        raise


async def sync_sources(
    engine,
    args: argparse.Namespace,
    *,
    providers: JobSourceProviderRegistry | None = None,
    adapter: JobSourceAdapter | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[SyncResult]:
    repository = JobRepository(engine)
    service = JobSyncService(engine)
    if providers is not None and adapter is not None:
        raise ValueError("pass providers or adapter, not both")
    providers = providers or (
        JobSourceProviderRegistry([adapter]) if adapter is not None else default_job_source_providers()
    )
    provider_filter = getattr(args, "provider", None)
    if provider_filter is not None:
        providers.adapter_for(provider_filter)
    results: list[SyncResult] = []
    checkpoint_file = getattr(args, "checkpoint_file", None)
    checkpoint = read_status(checkpoint_file)
    prefix = (
        str(checkpoint["batch_key"])
        if checkpoint and checkpoint.get("batch_key")
        else args.run_key or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    delay_seconds = float(getattr(args, "delay_seconds", 1.0))
    max_attempts = int(getattr(args, "max_attempts", 4))
    fetched_source = False
    sources = repository.active_career_sources(
        provider=provider_filter,
        company_id=args.company_id,
        source_ids=getattr(args, "source_ids", None),
        min_source_id=getattr(args, "min_source_id", None),
        limit=None if checkpoint_file else args.limit,
    )
    if checkpoint_file:
        checkpoint = prepare_sync_checkpoint(
            checkpoint_file,
            checkpoint=checkpoint,
            prefix=prefix,
            args=args,
            source_ids=[int(source["id"]) for source in sources],
        )
        source_by_id = {int(source["id"]): source for source in sources}
        unavailable_changed = mark_unavailable_checkpoint_sources(
            checkpoint,
            available_source_ids=set(source_by_id),
        )
        if unavailable_changed:
            write_status(checkpoint_file, checkpoint)
            write_sync_stage_checkpoint(
                getattr(args, "status_file", None),
                checkpoint,
                max_attempts=max_attempts,
            )
        pending_ids = [
            source_id
            for source_id in checkpoint["source_ids"]
            if (
                checkpoint["sources"].get(str(source_id), {}).get("state") == "running"
                or (
                    checkpoint["sources"].get(str(source_id), {}).get("state")
                    not in TERMINAL_CHECKPOINT_STATES
                    and int(
                        checkpoint["sources"].get(str(source_id), {}).get("attempts") or 0
                    )
                    < max_attempts
                )
            )
        ]
        if args.limit is not None:
            pending_ids = pending_ids[: args.limit]
        sources = [source_by_id[source_id] for source_id in pending_ids if source_id in source_by_id]

    for source in sources:
        source_adapter = providers.adapter_for(str(source["provider"]))
        source_id = int(source["id"])
        source_state = (
            checkpoint["sources"].setdefault(str(source_id), {"attempts": 0, "state": "pending"})
            if checkpoint_file and checkpoint is not None
            else None
        )
        if source_state is not None and source_state.get("state") == "running":
            if recover_completed_attempt(
                service,
                checkpoint_file,
                checkpoint,
                source_id=source_id,
                source_state=source_state,
            ):
                continue
        attempt = int(source_state.get("attempts") or 0) + 1 if source_state is not None else 1
        run_key = (
            f"{prefix}:{source_id}:attempt-{attempt}"
            if source_state is not None
            else f"{prefix}:{source_id}"
        )
        if source_state is not None:
            source_state.update({"attempts": attempt, "state": "running", "run_key": run_key})
            write_status(checkpoint_file, checkpoint)

        lock_connection = engine.connect()
        lock_key = 1_380_007_321 * 4_294_967_296 + source_id
        acquired = bool(
            lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
        )
        if not acquired:
            lock_connection.close()
            if source_state is not None:
                source_state.update(
                    {
                        "state": "failed",
                        "error": "another worker is already syncing this source",
                    }
                )
                write_status(checkpoint_file, checkpoint)
            else:
                raise RuntimeError(f"another worker is already syncing source {source_id}")
            continue
        try:
            result = await sync_one_source(
                service=service,
                source=source,
                source_adapter=source_adapter,
                run_key=run_key,
                fetched_source=fetched_source,
                delay_seconds=delay_seconds,
                sleeper=sleeper,
            )
        finally:
            lock_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": lock_key},
            )
            lock_connection.close()
        fetched_source = True
        results.append(result)
        if source_state is not None:
            retryable, diagnostics = sync_result_retryability(
                repository,
                career_source_id=source_id,
                run_key=run_key,
                result=result,
            )
            source_state["state"] = (
                "completed"
                if result.status == "completed"
                else result.status
                if retryable
                else "terminal_failed"
            )
            source_state["retryable"] = retryable
            source_state["diagnostics"] = diagnostics
            source_state["result"] = sync_result_counts(result)
            write_status(checkpoint_file, checkpoint)
            write_sync_stage_checkpoint(
                getattr(args, "status_file", None),
                checkpoint,
                max_attempts=max_attempts,
            )
    return results


def mark_unavailable_checkpoint_sources(
    checkpoint: dict,
    *,
    available_source_ids: set[int],
) -> bool:
    """Terminate frozen source entries that are no longer active in the registry.

    Checkpoint source sets are immutable, while operators may explicitly disable a
    career source between attempts. Without this transition, a disabled source stays
    pending forever and every detached restart becomes an empty retry loop.
    """
    changed = False
    for source_id in checkpoint.get("source_ids", []):
        if int(source_id) in available_source_ids:
            continue
        entry = checkpoint.get("sources", {}).get(str(source_id))
        if not isinstance(entry, dict) or entry.get("state") in TERMINAL_CHECKPOINT_STATES:
            continue
        entry.update(
            {
                "state": "terminal_failed",
                "retryable": False,
                "diagnostics": {
                    "reason": "career_source_not_active",
                    "career_source_id": int(source_id),
                },
                "error": "career source is no longer active in the frozen batch scope",
            }
        )
        changed = True
    return changed


async def sync_one_source(
    *,
    service: JobSyncService,
    source: dict,
    source_adapter: JobSourceAdapter,
    run_key: str,
    fetched_source: bool,
    delay_seconds: float,
    sleeper: Callable[[float], Awaitable[None]],
) -> SyncResult:
    source_id = int(source["id"])
    existing = service.existing_run_result(career_source_id=source_id, run_key=run_key)
    if existing is not None:
        if existing.status == "completed":
            print(f"source={source_id} run_key={run_key} already completed; replaying without fetch.")
        else:
            print(
                f"source={source_id} run_key={run_key} already {existing.status}; "
                "skipping fetch. Use a new --run-key for a new attempt."
            )
        return existing
    try:
        started = service.start_run(
            career_source_id=source_id,
            run_key=run_key,
            provider=source_adapter.provider,
            adapter_version=source_adapter.adapter_version,
        )
    except RunKeyReuseError as exc:
        print(str(exc))
        existing = service.existing_run_result(career_source_id=source_id, run_key=run_key)
        if existing is None:
            raise
        return existing
    if isinstance(started, SyncResult):
        return started
    if fetched_source and delay_seconds:
        await sleeper(delay_seconds)
    try:
        snapshot = await source_adapter.fetch_snapshot(str(source["external_source_id"]))
    except Exception as exc:
        return service.fail_started_run(started=started, error=exc)
    return service.apply_snapshot(started=started, snapshot=snapshot)


def recover_completed_attempt(
    service: JobSyncService,
    checkpoint_file: Path,
    checkpoint: dict,
    *,
    source_id: int,
    source_state: dict,
) -> bool:
    prior_run_key = str(source_state.get("run_key") or "")
    prior = (
        service.existing_run_result(career_source_id=source_id, run_key=prior_run_key)
        if prior_run_key
        else None
    )
    if prior is not None and prior.status == "completed":
        source_state["state"] = "completed"
        source_state["result"] = sync_result_counts(prior)
        write_status(checkpoint_file, checkpoint)
        return True
    if prior_run_key:
        service.interrupt_running_run(career_source_id=source_id, run_key=prior_run_key)
    return False


def prepare_sync_checkpoint(
    path: Path,
    *,
    checkpoint: dict | None,
    prefix: str,
    args: argparse.Namespace,
    source_ids: list[int],
) -> dict:
    scope = {
        "provider": getattr(args, "provider", None),
        "company_id": getattr(args, "company_id", None),
        "source_ids": getattr(args, "source_ids", None),
        "min_source_id": getattr(args, "min_source_id", None),
    }
    if checkpoint is not None:
        if checkpoint.get("schema_version") != 1 or checkpoint.get("scope") != scope:
            raise ValueError(f"sync checkpoint scope does not match this command: {path}")
        return checkpoint
    checkpoint = {
        "schema_version": 1,
        "batch_key": prefix,
        "scope": scope,
        "source_ids": source_ids,
        "sources": {
            str(source_id): {"attempts": 0, "state": "pending"} for source_id in source_ids
        },
    }
    write_status(path, checkpoint)
    return checkpoint


def sync_result_counts(result: SyncResult) -> dict[str, int | str]:
    return {
        "status": result.status,
        "jobs_added": result.jobs_added,
        "jobs_updated": result.jobs_updated,
        "jobs_closed": result.jobs_closed,
    }


def sync_result_retryability(
    repository: JobRepository,
    *,
    career_source_id: int,
    run_key: str,
    result: SyncResult,
) -> tuple[bool, dict[str, object]]:
    """Return whether a failed attempt merits another process-level attempt.

    Provider adapters already perform bounded request-level retries. A missing or
    forbidden public board is therefore terminal for this immutable batch; transient
    transport, rate-limit, 5xx, and valid-HTTP partial failures remain retryable.
    """
    with repository.engine.connect() as connection:
        run = repository.get_run(connection, career_source_id, run_key)
    if run is None:
        return True, {"reason": "sync_run_missing"}
    errors = run.get("errors") if isinstance(run.get("errors"), list) else []
    http_status = run.get("http_status")
    diagnostics: dict[str, object] = {
        "http_status": http_status,
        "error_kinds": sorted(
            {
                str(error.get("kind"))
                for error in errors
                if isinstance(error, dict) and error.get("kind")
            }
        ),
    }
    if result.status == "completed":
        return False, diagnostics
    if isinstance(http_status, int) and http_status in PERMANENT_HTTP_FAILURES:
        diagnostics["reason"] = "permanent_http_status"
        return False, diagnostics
    diagnostics["reason"] = "retryable_or_unclassified_failure"
    return True, diagnostics


def summarize_sync_checkpoint(checkpoint: dict, *, max_attempts: int) -> dict[str, int]:
    entries = list(checkpoint.get("sources", {}).values())
    succeeded = sum(entry.get("state") == "completed" for entry in entries)
    terminal_failures = sum(entry.get("state") == "terminal_failed" for entry in entries)
    exhausted_failures = sum(
        entry.get("state") in {"failed", "partial"}
        and int(entry.get("attempts") or 0) >= max_attempts
        for entry in entries
    )
    retryable = sum(
        entry.get("state") == "running"
        or (
            entry.get("state") not in TERMINAL_CHECKPOINT_STATES
            and int(entry.get("attempts") or 0) < max_attempts
        )
        for entry in entries
    )
    processed = sum(entry.get("state") != "pending" for entry in entries)
    return {
        "selected": len(entries),
        "processed": processed,
        "succeeded": succeeded,
        "failed": terminal_failures + exhausted_failures,
        "terminal_failures": terminal_failures,
        "exhausted_failures": exhausted_failures,
        "retryable": retryable,
    }


def write_sync_stage_checkpoint(
    status_file: Path | None,
    checkpoint: dict,
    *,
    max_attempts: int,
) -> None:
    prior = read_status(status_file) or stage_started("ats_sync")
    summary = summarize_sync_checkpoint(checkpoint, max_attempts=max_attempts)
    write_status(
        status_file,
        stage_checkpoint(
            prior,
            selected=summary["selected"],
            processed=summary["processed"],
            succeeded=summary["succeeded"],
            failed=summary["failed"],
            retryable=summary["retryable"],
            terminal_failures=summary["terminal_failures"],
            exhausted_failures=summary["exhausted_failures"],
            batch_key=checkpoint["batch_key"],
            checkpointed_sources=summary["selected"],
        ),
    )


if __name__ == "__main__":
    main()
