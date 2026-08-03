#!/usr/bin/env python3
"""Run the durable URL-to-provider staging queue."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import socket
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from yc_radar.services.database import create_schema, engine_from_url
from yc_radar.services.http_cache import DiskHttpCache
from yc_radar.services.staging import (
    NORMALIZER_VERSION,
    PARSER_VERSION,
    FunnelReporter,
    SnapshotPromoter,
    SourceEnrichHandler,
    SourceParseHandler,
    StagingRepository,
    StagingWorker,
    UrlFetchHandler,
    default_cache_path,
    iter_observations,
    run_async,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stage observed URLs, work them through leased provider detection, and promote only "
            "complete provider snapshots."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="Idempotently load CSV/JSON/JSONL observations.")
    load.add_argument("--input", type=Path, required=True)
    load.add_argument("--run-key", required=True)
    load.add_argument("--source", required=True)
    load.add_argument("--parser-version", default=PARSER_VERSION)
    load.add_argument("--normalizer-version", default=NORMALIZER_VERSION)
    load.add_argument("--max-attempts", type=positive_int, default=4)
    load.add_argument("--batch-size", type=positive_int, default=500)

    work = subparsers.add_parser("work", help="Claim and process one non-promotion stage.")
    work.add_argument("--stage", choices=("fetch", "parse", "enrich"), required=True)
    add_worker_arguments(work)
    work.add_argument("--cache-dir", type=Path, default=default_cache_path())

    status = subparsers.add_parser("status", help="Show queue state and the compact job funnel.")
    status.add_argument("--run-key")
    status.add_argument("--source")

    requeue = subparsers.add_parser(
        "requeue",
        help="Recover expired leases and optionally requeue terminal failures.",
    )
    requeue.add_argument("--run-key")
    requeue.add_argument("--source")
    requeue.add_argument(
        "--include",
        action="append",
        choices=("quarantined", "dead"),
        default=[],
    )
    requeue.add_argument("--reset-attempts", action="store_true")

    promote = subparsers.add_parser(
        "promote",
        help="Fetch verified provider snapshots and apply complete scans to canonical jobs.",
    )
    add_worker_arguments(promote)
    return parser.parse_args(argv)


def add_worker_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-key")
    parser.add_argument("--source")
    parser.add_argument("--limit", type=positive_int, default=25)
    parser.add_argument("--lease-seconds", type=positive_int, default=300)
    parser.add_argument("--worker-id", default=default_worker_id())


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    engine = engine_from_url()
    create_schema(engine)
    repository = StagingRepository(engine)
    try:
        payload = dispatch(repository, args)
    finally:
        engine.dispose()
    print(json.dumps(payload, sort_keys=True, indent=2, default=str))


def dispatch(repository: StagingRepository, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "load":
        result = repository.load_stream(
            run_key=args.run_key,
            source=args.source,
            observations=iter_observations(iter_rows(args.input)),
            parser_version=args.parser_version,
            normalizer_version=args.normalizer_version,
            max_attempts=args.max_attempts,
            input_uri=str(args.input.resolve()),
            input_sha256=file_sha256(args.input),
            batch_size=args.batch_size,
        )
        return asdict(result)

    run_key = getattr(args, "run_key", None)
    source = getattr(args, "source", None)
    if bool(run_key) != bool(source):
        raise ValueError("--source and --run-key must be supplied together")
    run_id = repository.run_id(run_key, source=source)
    if args.command == "status":
        return {
            **repository.status(run_id=run_id),
            "funnel": FunnelReporter(repository.engine).report(run_id=run_id),
        }
    if args.command == "requeue":
        return repository.requeue(
            run_id=run_id,
            include_states=args.include,
            reset_attempts=args.reset_attempts,
        )
    if args.command == "promote":
        result = run_async(
            SnapshotPromoter(repository).promote(
                limit=args.limit,
                lease_seconds=args.lease_seconds,
                lease_owner=args.worker_id,
                run_id=run_id,
            )
        )
        return asdict(result)
    if args.command == "work":
        if args.stage == "fetch":
            handler = UrlFetchHandler(DiskHttpCache(args.cache_dir))
        elif args.stage == "parse":
            handler = SourceParseHandler()
        else:
            handler = SourceEnrichHandler(repository.engine)
        result = run_async(
            StagingWorker(repository).work(
                stage=args.stage,
                handler=handler,
                limit=args.limit,
                lease_seconds=args.lease_seconds,
                lease_owner=args.worker_id,
                run_id=run_id,
            )
        )
        return asdict(result)
    raise AssertionError(f"unhandled command: {args.command}")


def iter_rows(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as source:
            yield from (dict(row) for row in csv.DictReader(source))
        return
    if suffix in {".jsonl", ".ndjson"}:
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"line {line_number} is not a JSON object")
                yield value
        return
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("JSON input must be an array of objects")
    yield from value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


if __name__ == "__main__":
    main()
