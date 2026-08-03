#!/usr/bin/env python3
"""Synchronize configured company job sources sequentially."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from yc_radar.adapters.base import JobSourceAdapter
from yc_radar.domain.job_sources import SyncResult
from yc_radar.services.database import create_schema, engine_from_url
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_source_registry import (
    JobSourceProviderRegistry,
    default_job_source_providers,
)
from yc_radar.services.job_sync_service import JobSyncService


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch complete snapshots for configured company job sources."
    )
    parser.add_argument(
        "--provider",
        choices=default_job_source_providers().providers,
        help="Restrict synchronization to one provider.",
    )
    parser.add_argument("--company-id", type=int)
    parser.add_argument(
        "--source-id",
        dest="source_ids",
        type=int,
        action="append",
        help="Restrict synchronization to a company source ID; repeat as needed.",
    )
    parser.add_argument("--limit", type=positive_int)
    parser.add_argument("--delay-seconds", type=non_negative_float, default=1.0)
    parser.add_argument(
        "--run-key",
        help=(
            "Stable batch key. Completed source attempts replay safely; use a new key to "
            "retry a failed attempt. Defaults to the current UTC timestamp."
        ),
    )
    return parser.parse_args(argv)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


async def sync_sources(
    engine,
    args: argparse.Namespace,
    *,
    providers: JobSourceProviderRegistry | None = None,
    adapter: JobSourceAdapter | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[SyncResult]:
    """Fetch selected sources in ID order with one durable database run per source."""
    if providers is not None and adapter is not None:
        raise ValueError("pass providers or adapter, not both")
    providers = providers or (
        JobSourceProviderRegistry([adapter])
        if adapter is not None
        else default_job_source_providers()
    )
    provider_filter = getattr(args, "provider", None)
    if provider_filter:
        providers.adapter_for(provider_filter)

    repository = JobRepository(engine)
    service = JobSyncService(engine)
    sources = repository.active_sources(
        provider=provider_filter,
        company_id=getattr(args, "company_id", None),
        source_ids=getattr(args, "source_ids", None),
        limit=None,
    )
    supported = set(providers.providers)
    sources = [source for source in sources if str(source["provider"]) in supported]
    limit = getattr(args, "limit", None)
    if limit is not None:
        sources = sources[:limit]
    batch_key = getattr(args, "run_key", None) or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    delay_seconds = float(getattr(args, "delay_seconds", 1.0))
    results: list[SyncResult] = []
    fetched_count = 0

    for source in sources:
        source_id = int(source["id"])
        run_key = f"{batch_key}:{source_id}"
        existing = service.existing_run_result(
            company_source_id=source_id,
            run_key=run_key,
        )
        if existing is not None:
            results.append(existing)
            state = "replayed" if existing.status == "completed" else "not retried"
            print(f"source={source_id} run_key={run_key} already {existing.status}; {state}.")
            continue

        source_adapter = providers.adapter_for(str(source["provider"]))
        started = service.start_run(
            company_source_id=source_id,
            run_key=run_key,
            provider=source_adapter.provider,
            adapter_version=source_adapter.adapter_version,
        )
        if isinstance(started, SyncResult):
            results.append(started)
            continue
        if fetched_count and delay_seconds:
            await sleeper(delay_seconds)
        try:
            snapshot = await source_adapter.fetch_snapshot(str(source["external_id"]))
        except Exception as exc:
            result = service.fail_started_run(started=started, error=exc)
        else:
            result = service.apply_snapshot(started=started, snapshot=snapshot)
        fetched_count += 1
        results.append(result)

    return results


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    engine = engine_from_url()
    create_schema(engine)
    try:
        results = asyncio.run(sync_sources(engine, args))
    finally:
        engine.dispose()

    for result in results:
        print(
            f"source={result.company_source_id} status={result.status} "
            f"added={result.jobs_added} updated={result.jobs_updated} "
            f"closed={result.jobs_closed}"
        )
    unsuccessful = [result for result in results if result.status != "completed"]
    print(f"Processed {len(results)} sources; non-completed runs: {len(unsuccessful)}.")
    if unsuccessful:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
