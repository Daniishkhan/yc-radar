#!/usr/bin/env python3
"""Dry-run or apply one guarded canonical company-source identity repair."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict

from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.database import engine_from_url


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair one provider source's company ownership, or disable a source that cannot "
            "represent one employer. The default is a read-only dry run."
        )
    )
    parser.add_argument("--provider", required=True)
    parser.add_argument("--external-id", required=True)
    parser.add_argument("--expected-company-id", required=True, type=positive_int)
    parser.add_argument("--reason", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--target-company-id", type=positive_int)
    action.add_argument("--new-company-name")
    action.add_argument(
        "--disable-source",
        action="store_true",
        help="Disable the source instead of assigning it to another company.",
    )
    parser.add_argument(
        "--new-company-slug",
        help="Optional preferred slug; valid only with --new-company-name.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply the repair. Without this flag the command performs a dry run.",
    )
    args = parser.parse_args(argv)
    if args.new_company_slug and not args.new_company_name:
        parser.error("--new-company-slug requires --new-company-name")
    if args.new_company_name is not None and not args.new_company_name.strip():
        parser.error("--new-company-name cannot be blank")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    engine = engine_from_url()
    try:
        registry = CompanyRegistry(engine)
        common = {
            "provider": args.provider,
            "external_id": args.external_id,
            "expected_company_id": args.expected_company_id,
            "reason": args.reason,
            "apply": args.yes,
        }
        if args.disable_source:
            result = registry.disable_source_identity(**common)
        else:
            result = registry.reassign_source_identity(
                **common,
                target_company_id=args.target_company_id,
                new_company_name=args.new_company_name,
                new_company_slug=args.new_company_slug,
            )
    finally:
        engine.dispose()

    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
