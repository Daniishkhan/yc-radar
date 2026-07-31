#!/usr/bin/env python3
"""Run discovery, then independent ATS and classification branches locally."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yc_radar.core.config import get_settings
from yc_radar.services.run_status import read_status, process_outcome, stage_finished, stage_started, write_status

SCRIPTS_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run local pipeline branches without serializing ATS sync behind classification.")
    parser.add_argument("--discovery-limit", type=int)
    parser.add_argument("--classification-limit", type=int, default=50)
    parser.add_argument("--sync-limit", type=int)
    parser.add_argument("--status-dir", type=Path)
    parser.add_argument("--run-key", help="Optional stable prefix passed to the provider sync stage.")
    args = parser.parse_args()
    if args.status_dir is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        args.status_dir = settings.runs_dir / f"pipeline-{stamp}"
    return args


async def run_child(stage: str, command: list[str], status_dir: Path) -> dict[str, Any]:
    status_file = status_dir / f"{stage}.json"
    process = await asyncio.create_subprocess_exec(*command, "--status-file", str(status_file))
    return_code = await process.wait()
    outcome = process_outcome(return_code)
    # A killed child never reaches its normal final write. Preserve its last
    # atomic checkpoint instead of replacing progress counters with zeroes. A
    # successful process may still have a meaningful partial stage result.
    prior = read_status(status_file) or stage_started(stage, command=command)
    state = (
        str(prior.get("state"))
        if return_code == 0 and prior.get("state") in {"completed", "partial"}
        else "completed" if return_code == 0 else "failed"
    )
    payload = stage_finished(prior, state=state, **outcome)
    write_status(status_file, payload)
    write_status(status_dir / f"{stage}.process.json", payload)
    return payload


async def run_pipeline(args: argparse.Namespace) -> int:
    status_dir = args.status_dir
    status_dir.mkdir(parents=True, exist_ok=True)
    pipeline = stage_started("pipeline")
    write_status(status_dir / "pipeline.json", pipeline)

    discovery_command = [sys.executable, str(SCRIPTS_DIR / "discover_career_urls.py")]
    if args.discovery_limit is not None:
        discovery_command.extend(["--limit", str(args.discovery_limit)])
    discovery = await run_child("discovery", discovery_command, status_dir)
    outcomes: dict[str, Any] = {"discovery": discovery}
    if discovery["raw_return_code"] != 0:
        write_status(
            status_dir / "pipeline.json",
            stage_finished(pipeline, state="failed", stages=outcomes, **process_outcome(discovery["raw_return_code"])),
        )
        return int(discovery["shell_exit_code"] or 1)

    classification_command = [
        sys.executable,
        str(SCRIPTS_DIR / "classify_discovered_urls.py"),
        "--limit",
        str(args.classification_limit),
    ]
    registration_command = [
        sys.executable,
        str(SCRIPTS_DIR / "sync_job_sources.py"),
        "discover",
    ]
    classification_task = asyncio.create_task(
        run_child("classification", classification_command, status_dir)
    )
    registration = await run_child("ats-registration", registration_command, status_dir)
    outcomes["ats_registration"] = registration
    if registration["raw_return_code"] == 0:
        sync_command = [
            sys.executable,
            str(SCRIPTS_DIR / "sync_job_sources.py"),
            "sync",
            "--checkpoint-file",
            str(status_dir / "ats-sync-checkpoint.json"),
        ]
        if args.sync_limit is not None:
            sync_command.extend(["--limit", str(args.sync_limit)])
        if args.run_key:
            sync_command.extend(["--run-key", args.run_key])
        outcomes["ats_sync"] = await run_child("ats-sync", sync_command, status_dir)
    else:
        outcomes["ats_sync"] = {"state": "not_started", "reason": "ats_registration_failed"}
    outcomes["classification"] = await classification_task

    failed = [
        outcome
        for outcome in outcomes.values()
        if outcome.get("raw_return_code") not in {None, 0}
    ]
    # A signal is the most actionable process outcome (for example, rc 137/143),
    # so it wins over an earlier generic branch failure while all failures remain
    # in the stage map.
    chosen_failure = next(
        (
            outcome
            for outcome in failed
            if outcome.get("signal") or int(outcome.get("raw_return_code") or 0) < 0
        ),
        failed[0] if failed else None,
    )
    final_raw_return_code = 0 if chosen_failure is None else int(chosen_failure["raw_return_code"])
    final_code = int(process_outcome(final_raw_return_code)["shell_exit_code"] or 0)
    has_partial_stage = any(
        outcome.get("state") in {"failed", "not_started", "partial"}
        for outcome in outcomes.values()
    )
    write_status(
        status_dir / "pipeline.json",
        stage_finished(
            pipeline,
            state="partial" if final_code or has_partial_stage else "completed",
            stages=outcomes,
            **process_outcome(final_raw_return_code),
        ),
    )
    return final_code


def main() -> None:
    raise SystemExit(asyncio.run(run_pipeline(parse_args())))


if __name__ == "__main__":
    main()
