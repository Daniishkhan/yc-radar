#!/usr/bin/env python3
"""Report queue size, provider contribution, and URL liveness metrics."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from yc_radar.services.application_artifacts import (
    discover_queue_artifacts,
    load_queues,
    parse_queue_spec,
)
from yc_radar.services.application_pool_metrics import build_application_pool_metrics
from yc_radar.services.run_status import write_status


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report provider, queue, URL coverage, and dead-link metrics."
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
        "--url-validations",
        type=Path,
        help="Optional JSON report produced by validate_application_urls.py.",
    )
    parser.add_argument("--output", type=Path, help="Atomically write the JSON metrics report.")
    args = parser.parse_args(argv)
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
    validations = _load_validations(args.url_validations)
    report = build_application_pool_metrics(queues, validations=validations)
    report["inputs"] = [
        {"queue": queue_name, "path": str(artifact_path)}
        for queue_name, artifact_path in artifacts
    ]
    report["url_validations_path"] = (
        str(args.url_validations) if args.url_validations is not None else None
    )
    write_status(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


def _load_validations(artifact_path: Path | None) -> list[dict[str, Any]]:
    if artifact_path is None:
        return []
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid URL validation JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("validations"), list):
        raise SystemExit("URL validation artifact must contain a validations list.")
    if not all(isinstance(row, dict) for row in payload["validations"]):
        raise SystemExit("Every URL validation row must be an object.")
    return [dict(row) for row in payload["validations"]]


if __name__ == "__main__":
    raise SystemExit(main())
