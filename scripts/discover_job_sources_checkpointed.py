#!/usr/bin/env python3
"""Register one provider's career-page inventory as a resumable frozen batch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from yc_radar.services.database import create_schema, engine_from_url
from yc_radar.services.job_source_registry import default_job_source_providers
from yc_radar.services.run_status import (
    read_status,
    stage_checkpoint,
    stage_finished,
    stage_started,
    write_status,
)
from yc_radar.services.source_activation import (
    activate_discovered_sources,
    summarize_checkpoint,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Register a frozen, provider-filtered career-page inventory with an "
            "atomic restart checkpoint."
        )
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=default_job_source_providers().providers,
    )
    parser.add_argument("--checkpoint-file", required=True, type=Path)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--checkpoint-every", type=positive_int, default=10)
    parser.add_argument("--max-attempts", type=positive_int, default=3)
    return parser.parse_args(argv)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def run(args: argparse.Namespace) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--provider",
        args.provider,
        "--checkpoint-file",
        str(args.checkpoint_file),
        "--checkpoint-every",
        str(args.checkpoint_every),
        "--max-attempts",
        str(args.max_attempts),
    ]
    if args.status_file is not None:
        command.extend(["--status-file", str(args.status_file)])
    status = stage_started("ats_registration", command=command)
    write_status(args.status_file, status)

    def progress(checkpoint: dict[str, Any]) -> None:
        summary = summarize_checkpoint(checkpoint)
        write_status(
            args.status_file,
            stage_checkpoint(
                status,
                selected=summary["selected"],
                processed=summary["processed"],
                succeeded=summary["registered"] + summary["existing"],
                failed=len(summary["conflicts"]),
                provider=summary["provider"],
                registered=summary["registered"],
                existing=summary["existing"],
                pending=summary["pending"],
                skipped=summary["skipped"],
                observed_rows=summary["observed_rows"],
                inventory_sha256=summary["inventory_sha256"],
            ),
        )

    try:
        engine = engine_from_url()
        create_schema(engine)
        result = activate_discovered_sources(
            engine,
            provider=args.provider,
            checkpoint_file=args.checkpoint_file,
            checkpoint_every=args.checkpoint_every,
            max_attempts=args.max_attempts,
            progress=progress,
        )
    except Exception as exc:
        prior = read_status(args.status_file) or status
        write_status(
            args.status_file,
            stage_finished(prior, state="failed", error=exc),
        )
        raise

    final_state = "partial" if result["conflicts"] or result["pending"] else "completed"
    prior = read_status(args.status_file) or status
    write_status(
        args.status_file,
        stage_finished(
            prior,
            state=final_state,
            selected=result["selected"],
            processed=result["processed"],
            succeeded=result["registered"] + result["existing"],
            failed=len(result["conflicts"]),
            provider=result["provider"],
            registered=result["registered"],
            existing=result["existing"],
            pending=result["pending"],
            skipped=result["skipped"],
            observed_rows=result["observed_rows"],
            inventory_sha256=result["inventory_sha256"],
            conflicts=result["conflicts"],
        ),
    )
    print(
        f"provider={result['provider']} selected={result['selected']} "
        f"registered={result['registered']} existing={result['existing']} "
        f"conflicts={len(result['conflicts'])} pending={result['pending']} "
        f"skipped_rows={result['skipped']} checkpoint={args.checkpoint_file}"
    )
    return result


def main() -> None:
    run(parse_args())


if __name__ == "__main__":
    main()
