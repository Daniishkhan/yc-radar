#!/usr/bin/env python3
"""Truncate all YC Radar Postgres tables through the Alembic-managed schema."""

from __future__ import annotations

import argparse

from yc_radar.services.database import engine_from_url, rebuild_database, truncate_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Truncate all YC Radar Postgres tables.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the destructive reset.",
    )
    parser.add_argument(
        "--rebuild-schema",
        action="store_true",
        help="Destructively downgrade to base and upgrade Alembic migrations instead of truncating rows.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.yes:
        raise SystemExit("Refusing to reset database without --yes.")
    engine = engine_from_url()
    if args.rebuild_schema:
        rebuild_database(engine)
        print(f"Dropped and recreated all YC Radar tables in {engine.url.database}.")
    else:
        truncate_database(engine)
        print(f"Truncated all YC Radar tables in {engine.url.database}.")


if __name__ == "__main__":
    main()
