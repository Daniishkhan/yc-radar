#!/usr/bin/env python3
"""Register one verified employer independently from any company or job source."""

from __future__ import annotations

import argparse

from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.database import engine_from_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register a source-neutral company identity.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--website", required=True, help="Verified employer homepage URL.")
    parser.add_argument("--slug", help="Optional preferred local slug.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = CompanyRegistry(engine_from_url()).register_company(
        name=args.name,
        website=args.website,
        requested_slug=args.slug,
    )
    print(
        f"company_id={result.company_id} company_created={result.company_created} "
        f"matched_by={result.matched_by}"
    )


if __name__ == "__main__":
    main()
