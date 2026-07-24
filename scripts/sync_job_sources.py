#!/usr/bin/env python3
"""Discover and synchronise supported public job-board sources sequentially."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from yc_radar.adapters.greenhouse import GreenhouseAdapter
from yc_radar.domain.job_sources import SyncResult
from yc_radar.services.database import create_schema, engine_from_url
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_sync_service import JobSyncService, RunKeyReuseError
from yc_radar.services.source_discovery import discover_greenhouse_sources


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover or sync read-only public job sources.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("discover-greenhouse", help="Register Greenhouse boards from career pages.")
    sync = subcommands.add_parser("sync", help="Fetch and apply configured sources.")
    sync.add_argument("--provider", default="greenhouse", choices=("greenhouse",))
    sync.add_argument("--company-id", type=int)
    sync.add_argument("--limit", type=int)
    sync.add_argument(
        "--run-key",
        help=(
            "Optional stable idempotency prefix. Completed keys replay without a fetch; "
            "failed, partial, or running keys require a new prefix for a new attempt."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = engine_from_url()
    create_schema(engine)
    if args.command == "discover-greenhouse":
        result = discover_greenhouse_sources(engine)
        print(
            f"Registered {result['registered']} new Greenhouse sources; "
            f"already registered {result['existing']}; skipped {result['skipped']}; "
            f"conflicts {len(result['conflicts'])}."
        )
        for conflict in result["conflicts"]:
            print(f"Conflict: {conflict}")
        return
    results = asyncio.run(sync_sources(engine, args))
    unsuccessful = [result for result in results if result.status != "completed"]
    print(
        f"Processed {len(results)} {args.provider} sources; "
        f"non-completed runs: {len(unsuccessful)}."
    )
    for result in results:
        print(
            f"source={result.career_source_id} status={result.status} "
            f"added={result.jobs_added} updated={result.jobs_updated} closed={result.jobs_closed}"
        )
    if unsuccessful:
        raise SystemExit(1)


async def sync_sources(
    engine,
    args: argparse.Namespace,
    *,
    adapter: GreenhouseAdapter | None = None,
) -> list[SyncResult]:
    repository = JobRepository(engine)
    service = JobSyncService(engine)
    adapter = adapter or GreenhouseAdapter()
    results: list[SyncResult] = []
    prefix = args.run_key or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for source in repository.active_career_sources(
        provider=args.provider,
        company_id=args.company_id,
        limit=args.limit,
    ):
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
                provider=adapter.provider,
                adapter_version=adapter.adapter_version,
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
        try:
            snapshot = await adapter.fetch_snapshot(str(source["external_source_id"]))
        except Exception as exc:
            results.append(service.fail_started_run(started=started, error=exc))
            continue
        results.append(service.apply_snapshot(started=started, snapshot=snapshot))
    return results


if __name__ == "__main__":
    main()
