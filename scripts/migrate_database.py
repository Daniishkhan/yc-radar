#!/usr/bin/env python3
"""Verify and migrate YC Radar's Postgres schema without destructive repair."""

from __future__ import annotations

import argparse

from yc_radar.services.database import engine_from_url
from yc_radar.services.migrations import upgrade_database, verify_existing_baseline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify or upgrade the YC Radar Alembic schema.")
    parser.add_argument("command", choices=("verify-existing", "upgrade"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = engine_from_url()
    if args.command == "verify-existing":
        diagnostics = verify_existing_baseline(engine)
        if diagnostics:
            for diagnostic in diagnostics:
                print(diagnostic)
            raise SystemExit("Baseline verification failed; do not stamp this database.")
        print("Baseline verification passed. Next: alembic stamp 0001_baseline, then upgrade head.")
        return
    upgrade_database(engine)
    print("Alembic upgrade completed.")


if __name__ == "__main__":
    main()
