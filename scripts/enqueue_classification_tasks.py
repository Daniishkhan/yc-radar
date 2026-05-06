#!/usr/bin/env python3
"""Enqueue one Celery classification task per discovered URL."""

from __future__ import annotations

import argparse
import time
from collections import Counter
from typing import Any

from celery.result import AsyncResult

from yc_radar.services.database import engine_from_url, fetch_discovered_url_rows
from yc_radar.tasks.page_classification import classify_discovered_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Queue discovered URL classification tasks.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum URLs to queue. Omit to queue all matching URLs.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Queue URLs even when they already have a page classification.",
    )
    parser.add_argument("--queue", default="classification")
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for queued tasks and print a completion summary.",
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = engine_from_url()
    rows = fetch_discovered_url_rows(
        engine,
        limit=args.limit,
        only_unclassified=not args.force,
    )
    if not rows:
        print("No discovered URLs need classification.")
        return

    results = [
        classify_discovered_url.apply_async(args=[row["id"]], queue=args.queue)
        for row in rows
    ]
    print(f"Queued {len(results)} classification tasks on queue '{args.queue}'.")
    print(f"First task id: {results[0].id}")

    if args.wait:
        summary = wait_for_results(results, timeout=args.timeout, poll_interval=args.poll_interval)
        print(
            "Completed queued classification tasks: "
            f"success={summary['success']} failed={summary['failed']} "
            f"pending={summary['pending']} timeout={summary['timeout']}"
        )
        if summary["page_counts"]:
            counts = Counter(summary["page_counts"])
            print(
                "Classified page kinds: "
                + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
            )


def wait_for_results(
    results: list[AsyncResult],
    *,
    timeout: int,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    pending = {result.id: result for result in results}
    success = 0
    failed = 0
    page_counts: Counter[str] = Counter()

    while pending and time.monotonic() < deadline:
        for task_id, result in list(pending.items()):
            if not result.ready():
                continue
            pending.pop(task_id)
            if result.successful():
                payload = result.get(timeout=1)
                success += 1
                page_kind = payload.get("page_kind")
                if page_kind:
                    page_counts[str(page_kind)] += 1
            else:
                failed += 1
                print(f"Task failed: {task_id} state={result.state} info={result.info}")
        if pending:
            time.sleep(poll_interval)

    return {
        "success": success,
        "failed": failed,
        "pending": len(pending),
        "timeout": bool(pending),
        "page_counts": dict(page_counts),
    }


if __name__ == "__main__":
    main()
