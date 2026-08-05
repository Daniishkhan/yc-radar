#!/usr/bin/env python3
"""Exit nonzero when canonical complete-snapshot synchronization is stale."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from yc_radar.services.database import engine_from_url
from yc_radar.services.pipeline_freshness import inspect_pipeline_freshness
from yc_radar.services.run_status import write_status


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check the newest successful complete source snapshot without mutating the database."
        )
    )
    parser.add_argument(
        "--max-age-hours",
        type=positive_float,
        default=24.0,
        help="Fail when no complete successful sync finished within this window (default: 24).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optionally write the same JSON report atomically to this path.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    engine = engine_from_url()
    try:
        report = inspect_pipeline_freshness(
            engine,
            max_age=timedelta(hours=args.max_age_hours),
        )
    except SQLAlchemyError as exc:
        report = {
            "schema_version": 1,
            "status": "error",
            "failures": [
                {
                    "code": "database_error",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
        }
        exit_code = 2
    else:
        exit_code = 0 if report["status"] == "healthy" else 1
    finally:
        engine.dispose()

    write_status(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
