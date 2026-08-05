#!/usr/bin/env python3
"""Sequentially validate public application URLs from one or more queue artifacts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from yc_radar.core.config import get_settings
from yc_radar.services.application_artifacts import (
    discover_queue_artifacts,
    load_queues,
    parse_queue_spec,
)
from yc_radar.services.application_url_validation import (
    ApplicationUrlValidator,
    validate_queue_rows,
)
from yc_radar.services.http_cache import DiskHttpCache
from yc_radar.services.run_status import write_status


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description=(
            "Validate queue application/posting URLs sequentially with public-target checks, "
            "bounded retries, and a disk cache."
        )
    )
    parser.add_argument(
        "--queue",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Read a named CSV/JSON queue; repeat for multiple queues.",
    )
    parser.add_argument(
        "--input",
        action="append",
        type=Path,
        default=[],
        help="Read a CSV/JSON artifact and infer queue names from its contents or filename.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Discover application, verification, and outreach artifacts in this run directory.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optionally write the JSON validation report atomically.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=settings.local_dir / "cache" / "application-url-validation",
    )
    parser.add_argument("--timeout-seconds", type=positive_float, default=10.0)
    parser.add_argument("--max-attempts", type=positive_int, default=3)
    parser.add_argument("--max-redirects", type=int, default=5)
    parser.add_argument("--delay-seconds", type=non_negative_float, default=1.0)
    parser.add_argument("--max-retry-delay-seconds", type=non_negative_float, default=30.0)
    parser.add_argument("--cache-ttl-hours", type=non_negative_float, default=24.0)
    parser.add_argument("--negative-cache-ttl-hours", type=non_negative_float, default=6.0)
    parser.add_argument("--transient-cache-ttl-minutes", type=non_negative_float, default=15.0)
    parser.add_argument("--refresh", action="store_true", help="Bypass valid cache entries.")
    args = parser.parse_args(argv)
    if args.max_redirects < 0:
        parser.error("--max-redirects must be zero or greater")
    if not args.queue and not args.input and args.run_dir is None:
        parser.error("pass --queue, --input, or --run-dir")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    artifacts: list[tuple[str | None, Path]] = []
    try:
        artifacts.extend(parse_queue_spec(value) for value in args.queue)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    artifacts.extend((None, path) for path in args.input)
    if args.run_dir is not None:
        artifacts.extend(discover_queue_artifacts(args.run_dir))
    if not artifacts:
        raise SystemExit("No queue artifacts were found.")

    queues = load_queues(artifacts)
    with ApplicationUrlValidator(
        DiskHttpCache(args.cache_dir),
        timeout_seconds=args.timeout_seconds,
        max_attempts=args.max_attempts,
        max_redirects=args.max_redirects,
        request_delay_seconds=args.delay_seconds,
        max_retry_delay_seconds=args.max_retry_delay_seconds,
        positive_cache_ttl_seconds=args.cache_ttl_hours * 3600,
        negative_cache_ttl_seconds=args.negative_cache_ttl_hours * 3600,
        transient_cache_ttl_seconds=args.transient_cache_ttl_minutes * 60,
    ) as validator:
        report = validate_queue_rows(queues, validator, refresh=args.refresh)

    report["inputs"] = [
        {"queue": queue_name, "path": str(artifact_path)}
        for queue_name, artifact_path in artifacts
    ]
    write_status(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
