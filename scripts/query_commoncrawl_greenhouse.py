#!/usr/bin/env python3
"""Query one Common Crawl URL Index partition for public Greenhouse boards."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from yc_radar.services.commoncrawl_greenhouse import (
    CRAWL_RE,
    IDENTIFIER_RE,
    build_candidate_query,
    build_partition_query,
)

COLLECTIONS_URL = "https://index.commoncrawl.org/collinfo.json"
DEFAULT_DATABASE = "radar_commoncrawl"
DEFAULT_WORKGROUP = "radar-commoncrawl"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export public Greenhouse board candidates from one Common Crawl partition."
    )
    parser.add_argument("--profile", default="radar-athena")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--workgroup", default=DEFAULT_WORKGROUP)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument(
        "--crawl",
        help="Common Crawl ID such as CC-MAIN-2026-30; defaults to the latest published crawl.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if shutil.which("aws") is None:
        raise SystemExit("AWS CLI v2 is required. Install it and run `aws login --profile ...`.")
    if not IDENTIFIER_RE.fullmatch(args.database):
        raise SystemExit("--database must be a simple Athena identifier")
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be positive")
    crawl = args.crawl or latest_crawl()
    if not CRAWL_RE.fullmatch(crawl):
        raise SystemExit("--crawl must look like CC-MAIN-2026-30")
    output = args.output or Path(
        f"data/local/debug/greenhouse_board_candidates_{crawl}.csv"
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    common = {
        "profile": args.profile,
        "region": args.region,
        "workgroup": args.workgroup,
        "database": args.database,
        "poll_seconds": args.poll_seconds,
    }
    table_sql = (
        Path("sql/commoncrawl/create_url_index.sql")
        .read_text(encoding="utf-8")
        .replace("radar_commoncrawl.", f"{args.database}.")
    )
    run_query(table_sql, **common)
    run_query(build_partition_query(args.database, crawl), **common)
    result = run_query(build_candidate_query(args.database, crawl), **common)
    output_location = str(result["ResultConfiguration"]["OutputLocation"])
    run_aws(
        [
            "s3",
            "cp",
            output_location,
            str(output),
            "--profile",
            args.profile,
            "--region",
            args.region,
        ],
        expect_json=False,
    )
    bytes_scanned = int(result.get("Statistics", {}).get("DataScannedInBytes") or 0)
    estimated_usd = bytes_scanned / 1_000_000_000_000 * 5
    print(
        f"crawl={crawl} bytes_scanned={bytes_scanned} "
        f"estimated_query_usd={estimated_usd:.6f} output={output}"
    )


def latest_crawl() -> str:
    response = httpx.get(
        COLLECTIONS_URL,
        timeout=15,
        headers={
            "User-Agent": (
                "yc-radar/0.2 (+https://github.com/Daniishkhan/yc-radar; "
                "commoncrawl-index-discovery)"
            ),
            "Accept": "application/json",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError("Common Crawl collection list has an unexpected shape")
    crawl = str(payload[0].get("id") or "")
    if not CRAWL_RE.fullmatch(crawl):
        raise ValueError("Common Crawl returned an invalid latest crawl ID")
    return crawl


def run_query(
    query: str,
    *,
    profile: str,
    region: str,
    workgroup: str,
    database: str,
    poll_seconds: float,
) -> dict[str, Any]:
    started = run_aws(
        [
            "athena",
            "start-query-execution",
            "--query-string",
            query,
            "--query-execution-context",
            f"Database={database},Catalog=AwsDataCatalog",
            "--work-group",
            workgroup,
            "--region",
            region,
            "--profile",
            profile,
        ]
    )
    query_id = str(started["QueryExecutionId"])
    while True:
        result = run_aws(
            [
                "athena",
                "get-query-execution",
                "--query-execution-id",
                query_id,
                "--region",
                region,
                "--profile",
                profile,
            ]
        )["QueryExecution"]
        state = result["Status"]["State"]
        if state == "SUCCEEDED":
            return result
        if state in {"FAILED", "CANCELLED"}:
            reason = result["Status"].get("StateChangeReason") or "unknown Athena error"
            raise RuntimeError(f"Athena query {query_id} {state.lower()}: {reason}")
        time.sleep(poll_seconds)


def run_aws(arguments: list[str], *, expect_json: bool = True) -> dict[str, Any]:
    completed = subprocess.run(
        ["aws", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"AWS CLI failed: {message}")
    output = completed.stdout.strip()
    return json.loads(output) if output and expect_json else {}


if __name__ == "__main__":
    main()
