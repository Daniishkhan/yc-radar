#!/usr/bin/env python3
"""Register and sync Ashby career-page evidence as one detached, resumable job."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from yc_radar.services.run_status import (
    process_outcome,
    read_status,
    stage_finished,
    stage_started,
    write_status,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
TOP_LEVEL_STATUS_NAME = "ashby-backfill.status.json"
REGISTRATION_CHECKPOINT_NAME = "ashby-registration.checkpoint.json"
REGISTRATION_STATUS_NAME = "ashby-registration.status.json"
SYNC_CHECKPOINT_NAME = "ashby-sync.checkpoint.json"
SYNC_STATUS_NAME = "ashby-sync.status.json"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Checkpoint Ashby sources from existing career-page evidence, then "
            "sync their current public snapshots sequentially."
        )
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="Unique durable directory for this activation batch.",
    )
    parser.add_argument("--delay-seconds", type=non_negative_float, default=1.0)
    parser.add_argument("--registration-max-attempts", type=positive_int, default=3)
    parser.add_argument("--sync-max-attempts", type=positive_int, default=4)
    parser.add_argument(
        "--run-key",
        help="Stable sync run-key prefix; defaults to an Ashby key derived from --run-dir.",
    )
    return parser.parse_args(argv)


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


def build_commands(args: argparse.Namespace) -> list[tuple[str, list[str], Path]]:
    run_dir = args.run_dir.resolve()
    run_key = args.run_key or f"ashby-backfill:{run_dir.name}"
    registration_status = run_dir / REGISTRATION_STATUS_NAME
    sync_status = run_dir / SYNC_STATUS_NAME
    return [
        (
            "registration",
            [
                sys.executable,
                str(SCRIPTS_DIR / "discover_job_sources_checkpointed.py"),
                "--provider",
                "ashby",
                "--checkpoint-file",
                str(run_dir / REGISTRATION_CHECKPOINT_NAME),
                "--status-file",
                str(registration_status),
                "--checkpoint-every",
                "10",
                "--max-attempts",
                str(args.registration_max_attempts),
            ],
            registration_status,
        ),
        (
            "sync",
            [
                sys.executable,
                str(SCRIPTS_DIR / "sync_job_sources.py"),
                "sync",
                "--provider",
                "ashby",
                "--checkpoint-file",
                str(run_dir / SYNC_CHECKPOINT_NAME),
                "--status-file",
                str(sync_status),
                "--delay-seconds",
                str(args.delay_seconds),
                "--max-attempts",
                str(args.sync_max_attempts),
                "--run-key",
                run_key,
            ],
            sync_status,
        ),
    ]


def run(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    top_status_path = run_dir / TOP_LEVEL_STATUS_NAME
    prior = read_status(top_status_path) or {}
    status = stage_started("ashby_backfill")
    status["attempt_count"] = int(prior.get("attempt_count") or 0) + 1
    status["stages"] = {}
    write_status(top_status_path, status)

    saw_partial = False
    for stage_name, command, child_status_path in build_commands(args):
        child_started_at = datetime.now(UTC).isoformat()
        process = subprocess.run(command, cwd=SCRIPTS_DIR.parent, check=False)
        child_status = read_status(child_status_path) or {}
        outcome = process_outcome(process.returncode)
        state = str(child_status.get("state") or "")
        if process.returncode == 0 and state not in {"completed", "partial"}:
            state = "failed"
            child_status = {
                **child_status,
                "error": {
                    "class": "InvalidChildStatus",
                    "message": (
                        f"{stage_name} exited successfully without a terminal status"
                    ),
                },
            }
            outcome = process_outcome(1)
        stage_record: dict[str, Any] = {
            **child_status,
            **outcome,
            "stage": stage_name,
            "state": state if process.returncode == 0 else "failed",
            "command": command,
            "started_at": child_status.get("started_at") or child_started_at,
            "finished_at": child_status.get("finished_at")
            or datetime.now(UTC).isoformat(),
        }
        status["stages"][stage_name] = stage_record
        write_status(top_status_path, status)
        if process.returncode != 0 or state == "failed":
            write_status(
                top_status_path,
                stage_finished(
                    status,
                    state="failed",
                    failed_stage=stage_name,
                    **outcome,
                ),
            )
            return int(outcome["shell_exit_code"] or 1)
        saw_partial = saw_partial or state == "partial"

    write_status(
        top_status_path,
        stage_finished(
            status,
            state="partial" if saw_partial else "completed",
            **process_outcome(0),
        ),
    )
    return 0


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
