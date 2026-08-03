#!/usr/bin/env python3
"""Attach one supported public ATS/feed source to an existing neutral company."""

from __future__ import annotations

import argparse

from yc_radar.services.database import engine_from_url, fetch_company_row
from yc_radar.services.job_source_registry import JobSourceRegistry, default_job_source_providers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register a public job source for a company.")
    company = parser.add_mutually_exclusive_group(required=True)
    company.add_argument("--company-id", type=int)
    company.add_argument("--company-slug")
    parser.add_argument("--provider", choices=default_job_source_providers().providers)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--discovered-from-url")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = engine_from_url()
    company_id = args.company_id
    if company_id is None:
        company = fetch_company_row(engine, args.company_slug)
        if company is None:
            raise SystemExit(f"Unknown company slug: {args.company_slug}")
        company_id = int(company["id"])
    result = JobSourceRegistry(engine).register_url(
        company_id=company_id,
        provider=args.provider,
        source_url=args.source_url,
        discovered_from_url=args.discovered_from_url,
    )
    print(
        f"company_id={result.company_id} company_source_id={result.company_source_id} "
        f"provider={result.provider} external_id={result.external_id} "
        f"created={result.created}"
    )


if __name__ == "__main__":
    main()
