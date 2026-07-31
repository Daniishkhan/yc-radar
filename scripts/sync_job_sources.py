#!/usr/bin/env python3
"""Discover and synchronise supported public job-board sources sequentially."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from yc_radar.adapters.base import JobSourceAdapter
from yc_radar.domain.job_sources import SyncResult
from yc_radar.services.database import create_schema, engine_from_url
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_source_registry import (
    JobSourceProviderRegistry,
    default_job_source_providers,
)
from yc_radar.services.job_sync_service import JobSyncService, RunKeyReuseError
from yc_radar.services.run_status import stage_finished, stage_started, write_status
from yc_radar.services.source_discovery import discover_job_sources


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
            "Optional stable idempotency prefix. Completed keys replay without a fetch; "
            "failed, partial, or running keys require a new prefix for a new attempt."
        ),
    )
    return parser.parse_args()


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
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
        print(
            f"Processed {len(results)} {args.provider or 'supported'} sources; "
            f"non-completed runs: {len(unsuccessful)}."
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
                state="completed" if not unsuccessful else "partial",
                selected=len(results),
                processed=len(results),
                succeeded=len(results) - len(unsuccessful),
                failed=len(unsuccessful),
                source_run_statuses={result.status: sum(item.status == result.status for item in results) for result in results},
            ),
        )
        if unsuccessful:
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
    prefix = args.run_key or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    delay_seconds = float(getattr(args, "delay_seconds", 1.0))
    fetched_source = False
    for source in repository.active_career_sources(
        provider=provider_filter,
        company_id=args.company_id,
        source_ids=getattr(args, "source_ids", None),
        min_source_id=getattr(args, "min_source_id", None),
        limit=args.limit,
    ):
        source_adapter = providers.adapter_for(str(source["provider"]))
        source_id = int(source["id"])
        run_key = f"{prefix}:{source_id}"
        existing = service.existing_run_result(career_source_id=source_id, run_key=run_key)
        if existing is not None:
            if existing.status == "completed":
                print(f"source={source_id} run_key={run_key} already completed; replaying without fetch.")
            else:
                print(
                    f"source={source_id} run_key={run_key} already {existing.status}; "
                    "skipping fetch. Use a new --run-key for a new attempt."
                )
            results.append(existing)
            continue
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
            if existing is not None:
                results.append(existing)
            continue
        if isinstance(started, SyncResult):
            results.append(started)
            continue
        if fetched_source and delay_seconds:
            await sleeper(delay_seconds)
        fetched_source = True
        try:
            snapshot = await source_adapter.fetch_snapshot(str(source["external_source_id"]))
        except Exception as exc:
            results.append(service.fail_started_run(started=started, error=exc))
            continue
        results.append(service.apply_snapshot(started=started, snapshot=snapshot))
    return results


if __name__ == "__main__":
    main()
