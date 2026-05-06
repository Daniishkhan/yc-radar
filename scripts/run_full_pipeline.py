#!/usr/bin/env python3
"""Run the local YC Radar pipeline from snapshots through queued classification."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load YC snapshots, discover career URLs, and queue page classification."
    )
    parser.add_argument("--skip-load-snapshots", action="store_true")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--skip-classification", action="store_true")
    parser.add_argument("--discovery-limit", type=int, default=None)
    parser.add_argument("--discovery-concurrency", type=int, default=20)
    parser.add_argument("--discovery-batch-size", type=int, default=25)
    parser.add_argument("--classification-limit", type=int, default=None)
    parser.add_argument("--classification-queue", default="classification")
    parser.add_argument(
        "--wait-classification",
        action="store_true",
        help="Wait for queued classification tasks to finish.",
    )
    parser.add_argument("--classification-timeout", type=int, default=86_400)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]

    if not args.skip_load_snapshots:
        run([sys.executable, "scripts/load_snapshots.py"], cwd=repo_root)

    if not args.skip_discovery:
        discovery_command = [
            sys.executable,
            "scripts/discover_career_urls.py",
            "--concurrency",
            str(args.discovery_concurrency),
            "--batch-size",
            str(args.discovery_batch_size),
        ]
        if args.discovery_limit is not None:
            discovery_command.extend(["--limit", str(args.discovery_limit)])
        run(discovery_command, cwd=repo_root)

    if not args.skip_classification:
        classification_command = [
            sys.executable,
            "scripts/enqueue_classification_tasks.py",
            "--queue",
            args.classification_queue,
        ]
        if args.classification_limit is not None:
            classification_command.extend(["--limit", str(args.classification_limit)])
        if args.wait_classification:
            classification_command.extend(
                ["--wait", "--timeout", str(args.classification_timeout)]
            )
        run(classification_command, cwd=repo_root)


def run(command: list[str], *, cwd: Path) -> None:
    printable = " ".join(command)
    print(f"\n$ {printable}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


if __name__ == "__main__":
    main()

