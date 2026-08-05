#!/usr/bin/env python3
"""Preview, fetch, and replay a credit-bounded TheirStack job plan."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from yc_radar.core.config import get_settings
from yc_radar.services.database import (
    company_sources_table,
    create_schema,
    engine_from_url,
    jobs_table,
)
from yc_radar.services.job_repository import DEFAULT_OBSERVATION_MAX_AGE_DAYS
from yc_radar.services.run_status import read_status, write_status
from yc_radar.services.staging import FunnelReporter, StagingRepository
from yc_radar.services.theirstack import (
    DEFAULT_RESERVE_SIZE,
    DEFAULT_PREVIEW_PAGES,
    DEFAULT_SEARCH_STRATA,
    FREE_PLAN_MAX_PAGES,
    IMPORTER_VERSION,
    THEIRSTACK_PROVIDER,
    import_result_dict,
    import_theirstack_jobs,
    paid_search_body,
    plan_digest,
    preview_search_body,
    quota_by_stratum,
    select_preview_jobs,
)
from yc_radar.services.theirstack_client import (
    CreditBalance,
    TheirStackClient,
    TheirStackRequestCache,
)


SEARCH_URL = "https://api.theirstack.com/v1/jobs/search"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_REQUEST_DELAY_SECONDS = 6.2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use free blurred previews to freeze a job-ID plan, fetch it with an explicit "
            "credit guard, then replay cached full records into observation-mode sources."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser(
        "preview",
        help="Build or replay a free blurred preview plan without consuming job credits.",
    )
    add_artifact_arguments(preview)
    preview.add_argument("--credit-budget", type=positive_int)
    preview.add_argument(
        "--pages-per-stratum",
        type=preview_pages,
        default=DEFAULT_PREVIEW_PAGES,
    )
    preview.add_argument("--reserve-size", type=non_negative_int, default=DEFAULT_RESERVE_SIZE)
    preview.add_argument("--exclude-job-id", action="append", type=non_negative_int, default=[])
    preview.add_argument("--exclude-job-ids-file", type=Path)
    preview.add_argument(
        "--request-delay-seconds",
        type=non_negative_float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
    )
    preview.add_argument(
        "--preview-cache-max-age-hours",
        type=non_negative_float,
        default=6.0,
        help="Reuse free blurred previews for at most this many hours (default: 6).",
    )
    preview.add_argument(
        "--refresh",
        action="store_true",
        help="Re-run free previews. Refuses to replace a manifest that has paid batches.",
    )

    fetch = subparsers.add_parser(
        "fetch",
        help="Fetch the frozen IDs; this is the only subcommand allowed to spend credits.",
    )
    add_artifact_arguments(fetch)
    fetch.add_argument("--max-credits", type=positive_int, required=True)
    fetch.add_argument("--yes-spend-credits", action="store_true")
    fetch.add_argument(
        "--request-delay-seconds",
        type=non_negative_float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
    )
    fetch.add_argument(
        "--retry-uncertain-spend",
        action="store_true",
        help=(
            "Retry a batch left in requesting state only when the enlarged max-credit guard "
            "also covers the possibly billed first attempt."
        ),
    )

    apply = subparsers.add_parser(
        "apply",
        help="Replay cached full responses into Postgres; never calls TheirStack.",
    )
    add_artifact_arguments(apply)
    apply.add_argument(
        "--no-stage-urls",
        action="store_true",
        help="Preserve raw observations without enqueueing their URLs for ATS detection.",
    )

    status = subparsers.add_parser(
        "status",
        help="Report local manifest/cache/database progress without vendor network calls.",
    )
    add_artifact_arguments(status)
    return parser.parse_args(argv)


def add_artifact_arguments(parser: argparse.ArgumentParser) -> None:
    settings = get_settings()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=settings.runs_dir / "theirstack" / date.today().isoformat() / "manifest.json",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=settings.local_dir / "cache" / "theirstack",
    )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def preview_pages(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= FREE_PLAN_MAX_PAGES:
        raise argparse.ArgumentTypeError("must be between 1 and 5")
    return parsed


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "preview":
        payload = preview_command(args)
    elif args.command == "fetch":
        payload = fetch_command(args)
    elif args.command == "apply":
        payload = apply_command(args)
    elif args.command == "status":
        payload = status_command(args)
    else:  # pragma: no cover - argparse enforces a known command
        raise AssertionError(f"unknown command: {args.command}")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def preview_command(
    args: argparse.Namespace,
    *,
    client: TheirStackClient | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    prior = load_manifest(args.manifest, required=False)
    if prior is not None:
        validate_manifest(prior)
        if not args.refresh:
            return manifest_summary(prior, replayed=True)
        if _has_paid_progress(prior):
            raise SystemExit(
                "Refusing to refresh a manifest with paid progress; use a new --manifest."
            )

    settings = get_settings()
    api_client = client or _client_from_settings(args.cache_dir, settings.theirstack_api_key)
    balance = api_client.credit_balance()
    budget = args.credit_budget or balance.remaining
    if budget < 1:
        raise SystemExit("TheirStack has no API credits available.")
    if budget > balance.remaining:
        raise SystemExit(
            f"Requested {budget} credits, but the current balance is {balance.remaining}."
        )

    excluded = set(args.exclude_job_id)
    excluded.update(read_job_id_file(args.exclude_job_ids_file))
    excluded.update(existing_theirstack_job_ids())
    previews: dict[str, list[dict[str, Any]]] = {}
    request_records: list[dict[str, Any]] = []
    network_request_seen = False
    cache = api_client.cache
    cache_max_age_seconds = args.preview_cache_max_age_hours * 60 * 60

    for stratum in DEFAULT_SEARCH_STRATA:
        rows: list[dict[str, Any]] = []
        for page in range(args.pages_per_stratum):
            body = preview_search_body(stratum, page=page, excluded_job_ids=excluded)
            will_use_cache = not args.refresh and (
                cache.load(
                    "POST",
                    SEARCH_URL,
                    body,
                    max_age_seconds=cache_max_age_seconds,
                )
                is not None
            )
            if network_request_seen and not will_use_cache and args.request_delay_seconds:
                sleeper(args.request_delay_seconds)
            result = api_client.search(
                body,
                cache_max_age_seconds=cache_max_age_seconds,
                force_refresh=args.refresh,
            )
            if result.cache_source == "network":
                network_request_seen = True
            data = [dict(row) for row in result.payload["data"] if isinstance(row, Mapping)]
            rows.extend(data)
            metadata = dict(result.payload.get("metadata") or {})
            request_records.append(
                {
                    "stratum": stratum.name,
                    "page": page,
                    "request_hash": result.request_hash,
                    "cache_source": result.cache_source,
                    "rows": len(data),
                    "total_results": metadata.get("total_results"),
                    "total_companies": metadata.get("total_companies"),
                }
            )
            if len(data) < 25:
                break
        previews[stratum.name] = rows

    selection = select_preview_jobs(
        previews,
        credit_budget=budget,
        reserve_size=args.reserve_size,
        excluded_job_ids=excluded,
    )
    quotas = quota_by_stratum(budget)
    created_at = datetime.now(UTC).isoformat()
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "importer_version": IMPORTER_VERSION,
        "state": "previewed",
        "created_at": created_at,
        "updated_at": created_at,
        "observation_time": created_at,
        "credit_budget": budget,
        "balance_before": {
            "api_credits": balance.api_credits,
            "used_api_credits": balance.used_api_credits,
        },
        "excluded_job_ids": sorted(excluded),
        "selected_job_ids": list(selection.selected_job_ids),
        "reserve_job_ids": list(selection.reserve_job_ids),
        "selected_by_stratum": selection.selected_by_stratum,
        "candidates_seen": selection.candidates_seen,
        "strata": [
            {
                "name": stratum.name,
                "quota": quotas[stratum.name],
                "posted_at_max_age_days": stratum.posted_at_max_age_days,
                "title_patterns": list(stratum.title_patterns),
                "description_patterns": list(stratum.description_patterns),
            }
            for stratum in DEFAULT_SEARCH_STRATA
        ],
        "preview_requests": request_records,
        "batches": [
            {
                "index": index,
                "job_ids": list(job_ids),
                "body": paid_search_body(job_ids),
                "state": "pending",
            }
            for index, job_ids in enumerate(chunks(selection.selected_job_ids, 25), start=1)
        ],
        "top_up_batches": [],
    }
    manifest["plan_id"] = plan_digest(manifest)
    write_status(args.manifest, manifest)
    return manifest_summary(manifest, replayed=False)


def fetch_command(
    args: argparse.Namespace,
    *,
    client: TheirStackClient | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if not args.yes_spend_credits:
        raise SystemExit("Paid fetch requires --yes-spend-credits.")
    manifest = load_manifest(args.manifest)
    assert manifest is not None
    validate_manifest(manifest)
    selected_count = len(manifest["selected_job_ids"])
    if args.max_credits < selected_count:
        raise SystemExit(
            f"--max-credits {args.max_credits} is below the frozen {selected_count}-job plan."
        )

    settings = get_settings()
    api_client = client or _client_from_settings(args.cache_dir, settings.theirstack_api_key)
    balance = api_client.credit_balance()
    fetched_ids = _fetched_job_ids(manifest, api_client.cache)
    all_batches = [*manifest.get("batches", []), *manifest.get("top_up_batches", [])]
    possible_uncertain = sum(
        len(batch.get("job_ids") or [])
        for batch in all_batches
        if batch.get("state") == "requesting"
        and api_client.cache.load("POST", SEARCH_URL, batch.get("body") or {}) is None
    )
    if possible_uncertain and not args.retry_uncertain_spend:
        raise SystemExit(
            "A paid batch is in requesting state without a cached response. Refusing an "
            "automatic retry; inspect provider request history or pass "
            "--retry-uncertain-spend with a guard large enough for both attempts."
        )
    worst_case_spend = len(fetched_ids) + possible_uncertain
    if worst_case_spend > args.max_credits:
        raise SystemExit("The max-credit guard does not cover known plus uncertain paid results.")

    pending_network_credits = sum(
        len(batch.get("job_ids") or [])
        for batch in all_batches
        if batch.get("state") != "fetched"
        and api_client.cache.load("POST", SEARCH_URL, batch.get("body") or {}) is None
    )
    if worst_case_spend + pending_network_credits > args.max_credits:
        raise SystemExit(
            "The max-credit guard does not cover all cached, uncertain, and pending batches."
        )
    if pending_network_credits > balance.remaining:
        raise SystemExit(
            f"Current balance {balance.remaining} is too small for all pending paid "
            f"requests ({pending_network_credits} possible credits)."
        )

    network_request_seen = False
    for batch in manifest["batches"]:
        if batch.get("state") == "fetched":
            continue
        body = dict(batch["body"])
        cached = api_client.cache.load("POST", SEARCH_URL, body) is not None
        if not cached:
            possible = worst_case_spend + len(batch["job_ids"])
            if batch.get("state") == "requesting":
                possible += len(batch["job_ids"])
            if possible > args.max_credits:
                raise SystemExit(
                    f"Batch {batch['index']} could exceed --max-credits={args.max_credits}."
                )
            if balance.remaining < len(batch["job_ids"]):
                raise SystemExit(
                    f"Current balance {balance.remaining} is too small for batch "
                    f"{batch['index']} ({len(batch['job_ids'])} IDs)."
                )
            if network_request_seen and args.request_delay_seconds:
                sleeper(args.request_delay_seconds)
            batch["state"] = "requesting"
            batch["request_started_at"] = datetime.now(UTC).isoformat()
            _touch_manifest(args.manifest, manifest)
        result = api_client.search(body, allow_paid=True)
        if result.cache_source == "network":
            network_request_seen = True
        returned = _response_job_ids(result.payload)
        requested = {int(job_id) for job_id in batch["job_ids"]}
        if not returned.issubset(requested):
            raise SystemExit(f"Batch {batch['index']} returned unrequested job IDs.")
        batch.update(
            state="fetched",
            request_hash=result.request_hash,
            cache_source=result.cache_source,
            returned_job_ids=sorted(returned),
            returned_count=len(returned),
            fetched_at=datetime.now(UTC).isoformat(),
        )
        worst_case_spend = max(worst_case_spend, len(_manifest_returned_ids(manifest)))
        _touch_manifest(args.manifest, manifest)

    returned = _manifest_returned_ids(manifest)
    target = min(args.max_credits, int(manifest["credit_budget"]))
    missing = max(0, target - len(returned))
    if missing and manifest.get("top_up_batches"):
        top_up = manifest["top_up_batches"][0]
        if top_up.get("state") != "fetched":
            _fetch_top_up(
                args,
                api_client=api_client,
                balance=balance,
                manifest=manifest,
                top_up=top_up,
                returned=returned,
                possible_uncertain=possible_uncertain,
                network_request_seen=network_request_seen,
                sleeper=sleeper,
            )
    elif missing:
        reserve = [
            int(job_id)
            for job_id in manifest.get("reserve_job_ids", [])
            if int(job_id) not in returned
        ][: min(missing, 25)]
        if reserve:
            top_up = _top_up_batch(manifest, reserve)
            _touch_manifest(args.manifest, manifest)
            _fetch_top_up(
                args,
                api_client=api_client,
                balance=balance,
                manifest=manifest,
                top_up=top_up,
                returned=returned,
                possible_uncertain=possible_uncertain,
                network_request_seen=network_request_seen,
                sleeper=sleeper,
            )

    balance_after = api_client.credit_balance()
    manifest["state"] = "fetched"
    manifest["balance_after"] = {
        "api_credits": balance_after.api_credits,
        "used_api_credits": balance_after.used_api_credits,
    }
    manifest["returned_job_ids"] = sorted(_manifest_returned_ids(manifest))
    _touch_manifest(args.manifest, manifest)
    return manifest_summary(manifest, replayed=False)


def apply_command(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.manifest)
    assert manifest is not None
    validate_manifest(manifest)
    incomplete = [
        batch["index"]
        for batch in [*manifest.get("batches", []), *manifest.get("top_up_batches", [])]
        if batch.get("state") != "fetched"
    ]
    if incomplete:
        raise SystemExit(f"Paid batches are not fully fetched: {incomplete}")
    cache = TheirStackRequestCache(args.cache_dir)
    jobs = cached_jobs(manifest, cache)
    if not jobs:
        raise SystemExit("The manifest has no cached full jobs to apply.")
    observation_time = parse_timestamp(manifest["observation_time"])
    engine = engine_from_url()
    try:
        result = import_theirstack_jobs(
            engine,
            jobs,
            plan_id=str(manifest["plan_id"]),
            stage_urls=not args.no_stage_urls,
            now=observation_time,
        )
    finally:
        engine.dispose()
    manifest["state"] = "applied"
    manifest["apply_result"] = import_result_dict(result)
    manifest["applied_at"] = datetime.now(UTC).isoformat()
    _touch_manifest(args.manifest, manifest)
    return manifest_summary(manifest, replayed=False)


def _fetch_top_up(
    args: argparse.Namespace,
    *,
    api_client: TheirStackClient,
    balance: CreditBalance,
    manifest: dict[str, Any],
    top_up: dict[str, Any],
    returned: set[int],
    possible_uncertain: int,
    network_request_seen: bool,
    sleeper: Callable[[float], None],
) -> None:
    body = dict(top_up["body"])
    requested = {int(job_id) for job_id in top_up["job_ids"]}
    cached = api_client.cache.load("POST", SEARCH_URL, body) is not None
    if not cached:
        if len(returned) + possible_uncertain + len(requested) > args.max_credits:
            raise SystemExit("Reserve top-up could exceed the max-credit guard.")
        current_balance = api_client.credit_balance() if network_request_seen else balance
        if current_balance.remaining < len(requested):
            raise SystemExit("Current balance is too small for the reserve top-up.")
        if network_request_seen and args.request_delay_seconds:
            sleeper(args.request_delay_seconds)
        top_up["state"] = "requesting"
        top_up["request_started_at"] = datetime.now(UTC).isoformat()
        _touch_manifest(args.manifest, manifest)
    result = api_client.search(body, allow_paid=True)
    returned_top_up = _response_job_ids(result.payload)
    if not returned_top_up.issubset(requested):
        raise SystemExit("Reserve top-up returned unrequested job IDs.")
    top_up.update(
        state="fetched",
        request_hash=result.request_hash,
        cache_source=result.cache_source,
        returned_job_ids=sorted(returned_top_up),
        returned_count=len(returned_top_up),
        fetched_at=datetime.now(UTC).isoformat(),
    )
    _touch_manifest(args.manifest, manifest)


def status_command(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.manifest)
    assert manifest is not None
    validate_manifest(manifest)
    cache = TheirStackRequestCache(args.cache_dir)
    batch_status = []
    for batch in [*manifest.get("batches", []), *manifest.get("top_up_batches", [])]:
        body = dict(batch.get("body") or {})
        batch_status.append(
            {
                "index": batch.get("index"),
                "state": batch.get("state"),
                "job_ids": len(batch.get("job_ids") or []),
                "returned_count": batch.get("returned_count", 0),
                "cached": cache.load("POST", SEARCH_URL, body) is not None,
            }
        )
    engine = engine_from_url()
    create_schema(engine)
    try:
        with engine.connect() as connection:
            source_count = int(
                connection.scalar(
                    select(func.count()).select_from(company_sources_table).where(
                        company_sources_table.c.provider == THEIRSTACK_PROVIDER
                    )
                )
                or 0
            )
            job_count = int(
                connection.scalar(
                    select(func.count())
                    .select_from(jobs_table)
                    .join(
                        company_sources_table,
                        company_sources_table.c.id == jobs_table.c.company_source_id,
                    )
                    .where(company_sources_table.c.provider == THEIRSTACK_PROVIDER)
                )
                or 0
            )
        staging = StagingRepository(engine)
        try:
            run_id = staging.run_id(
                f"theirstack:{manifest['plan_id']}",
                source=THEIRSTACK_PROVIDER,
            )
        except ValueError:
            run_id = None
        staging_status = staging.status(run_id=run_id) if run_id is not None else None
        funnel = FunnelReporter(engine).report(run_id=run_id) if run_id is not None else None
    finally:
        engine.dispose()
    return {
        **manifest_summary(manifest, replayed=True),
        "batches": batch_status,
        "database": {
            "theirstack_sources": source_count,
            "theirstack_jobs": job_count,
            "observation_freshness_days": DEFAULT_OBSERVATION_MAX_AGE_DAYS,
        },
        "staging": staging_status,
        "funnel": funnel,
    }


def cached_jobs(
    manifest: Mapping[str, Any],
    cache: TheirStackRequestCache,
) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    allowed = {
        int(job_id)
        for job_id in [
            *manifest.get("selected_job_ids", []),
            *manifest.get("reserve_job_ids", []),
        ]
    }
    for batch in [*manifest.get("batches", []), *manifest.get("top_up_batches", [])]:
        if batch.get("state") != "fetched":
            continue
        body = dict(batch.get("body") or {})
        payload = cache.load("POST", SEARCH_URL, body)
        if payload is None:
            raise SystemExit(f"Cached response is missing for batch {batch.get('index')}.")
        for row in payload.get("data", []):
            if not isinstance(row, Mapping):
                continue
            try:
                job_id = int(row.get("id"))
            except (TypeError, ValueError):
                raise SystemExit("Cached response contains a job without an integer ID.") from None
            if job_id not in allowed:
                raise SystemExit(f"Cached response contains unplanned job ID {job_id}.")
            if row.get("has_blurred_data") is True:
                raise SystemExit(f"Cached paid job {job_id} is still blurred.")
            by_id[job_id] = dict(row)
    if len(by_id) > int(manifest["credit_budget"]):
        raise SystemExit("Cached jobs exceed the frozen credit budget.")
    return [by_id[job_id] for job_id in sorted(by_id)]


def existing_theirstack_job_ids() -> set[int]:
    engine = engine_from_url()
    try:
        create_schema(engine)
        with engine.connect() as connection:
            values = connection.scalars(
                select(jobs_table.c.external_job_id)
                .join(
                    company_sources_table,
                    company_sources_table.c.id == jobs_table.c.company_source_id,
                )
                .where(company_sources_table.c.provider == THEIRSTACK_PROVIDER)
            )
            result: set[int] = set()
            for value in values:
                try:
                    result.add(int(value))
                except (TypeError, ValueError):
                    continue
            return result
    finally:
        engine.dispose()


def read_job_id_file(path: Path | None) -> set[int]:
    if path is None:
        return set()
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        values = [
            value
            for line in text.splitlines()
            for value in line.replace(",", " ").split()
        ]
    else:
        if isinstance(payload, dict):
            payload = payload.get("job_ids")
        if not isinstance(payload, list):
            raise SystemExit("Excluded job-ID file must be a JSON list/object or plain ID lines.")
        values = payload
    try:
        return {int(value) for value in values}
    except (TypeError, ValueError):
        raise SystemExit("Excluded job-ID file contains a non-integer value.") from None


def load_manifest(path: Path, *, required: bool = True) -> dict[str, Any] | None:
    payload = read_status(path)
    if payload is None and (required or path.exists()):
        raise SystemExit(f"Manifest is missing or malformed: {path}")
    return payload


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise SystemExit("Unsupported TheirStack manifest schema version.")
    if manifest.get("importer_version") != IMPORTER_VERSION:
        raise SystemExit("TheirStack manifest was created by a different importer version.")
    expected = plan_digest(manifest)
    if manifest.get("plan_id") != expected:
        raise SystemExit("TheirStack manifest plan scope has changed; refusing replay.")
    observation_time = parse_timestamp(manifest.get("observation_time"))
    if observation_time > datetime.now(UTC) + timedelta(minutes=5):
        raise SystemExit("TheirStack manifest observation_time cannot be in the future.")
    batches = manifest.get("batches")
    if not isinstance(batches, list):
        raise SystemExit("TheirStack manifest has no paid batch list.")
    selected = _manifest_id_list(manifest.get("selected_job_ids"), "selected_job_ids")
    reserve = _manifest_id_list(manifest.get("reserve_job_ids"), "reserve_job_ids")
    budget = manifest.get("credit_budget")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise SystemExit("TheirStack manifest has an invalid credit budget.")
    if len(selected) > budget:
        raise SystemExit("TheirStack selected jobs exceed the frozen credit budget.")
    if len(selected) != len(set(selected)) or len(reserve) != len(set(reserve)):
        raise SystemExit("TheirStack manifest job-ID lists contain duplicates.")
    if set(selected) & set(reserve):
        raise SystemExit("TheirStack selected and reserve job IDs overlap.")

    expected_chunks = chunks(selected, 25)
    if len(batches) != len(expected_chunks):
        raise SystemExit("TheirStack paid batch scope differs from the frozen selection.")
    for index, (batch, expected_ids) in enumerate(zip(batches, expected_chunks), start=1):
        _validate_paid_batch(
            batch,
            expected_index=index,
            expected_ids=list(expected_ids),
            label=f"paid batch {index}",
        )

    topups = manifest.get("top_up_batches", [])
    if not isinstance(topups, list) or len(topups) > 1:
        raise SystemExit("TheirStack manifest has an invalid reserve top-up list.")
    if topups:
        top_up = topups[0]
        if not isinstance(top_up, Mapping):
            raise SystemExit("TheirStack reserve top-up is malformed.")
        top_up_ids = _manifest_id_list(top_up.get("job_ids"), "top-up job_ids")
        if not top_up_ids or len(top_up_ids) > 25 or not set(top_up_ids).issubset(reserve):
            raise SystemExit("TheirStack reserve top-up differs from the frozen reserve.")
        _validate_paid_batch(
            top_up,
            expected_index="top-up-1",
            expected_ids=top_up_ids,
            label="reserve top-up",
        )


def _manifest_id_list(value: Any, label: str) -> list[int]:
    if not isinstance(value, list):
        raise SystemExit(f"TheirStack manifest {label} must be a list.")
    result: list[int] = []
    for job_id in value:
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 0:
            raise SystemExit(f"TheirStack manifest {label} contains an invalid job ID.")
        result.append(job_id)
    return result


def _validate_paid_batch(
    batch: Any,
    *,
    expected_index: int | str,
    expected_ids: list[int],
    label: str,
) -> None:
    if not isinstance(batch, Mapping):
        raise SystemExit(f"TheirStack {label} is malformed.")
    if batch.get("index") != expected_index:
        raise SystemExit(f"TheirStack {label} index differs from the frozen plan.")
    actual_ids = _manifest_id_list(batch.get("job_ids"), f"{label} job_ids")
    if actual_ids != expected_ids:
        raise SystemExit(f"TheirStack {label} scope differs from the frozen plan.")
    body = batch.get("body")
    if not isinstance(body, Mapping) or dict(body) != paid_search_body(expected_ids):
        raise SystemExit(f"TheirStack {label} request differs from the frozen plan.")
    if batch.get("state") not in {"pending", "requesting", "fetched"}:
        raise SystemExit(f"TheirStack {label} has an invalid state.")
    if "returned_job_ids" in batch:
        returned = _manifest_id_list(batch.get("returned_job_ids"), f"{label} returned_job_ids")
        if len(returned) != len(set(returned)) or not set(returned).issubset(expected_ids):
            raise SystemExit(f"TheirStack {label} returned IDs differ from its request scope.")


def manifest_summary(manifest: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
    batches = list(manifest.get("batches") or [])
    topups = list(manifest.get("top_up_batches") or [])
    return {
        "manifest_state": manifest.get("state"),
        "plan_id": manifest.get("plan_id"),
        "credit_budget": manifest.get("credit_budget"),
        "selected_jobs": len(manifest.get("selected_job_ids") or []),
        "reserve_jobs": len(manifest.get("reserve_job_ids") or []),
        "paid_batches": len(batches),
        "fetched_batches": sum(batch.get("state") == "fetched" for batch in batches),
        "top_up_batches": len(topups),
        "returned_jobs": len(_manifest_returned_ids(manifest)),
        "replayed": replayed,
        "apply_result": manifest.get("apply_result"),
    }


def chunks(values: Sequence[int], size: int) -> list[tuple[int, ...]]:
    return [tuple(values[index : index + size]) for index in range(0, len(values), size)]


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise SystemExit("Manifest observation_time must be an ISO-8601 string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit("Manifest observation_time is invalid.") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _client_from_settings(cache_dir: Path, api_key: str | None) -> TheirStackClient:
    if not api_key or not api_key.strip():
        raise SystemExit("THEIRSTACK_API_KEY is not set in the environment or ignored .env file.")
    return TheirStackClient(api_key, TheirStackRequestCache(cache_dir))


def _response_job_ids(payload: Mapping[str, Any]) -> set[int]:
    result: set[int] = set()
    for row in payload.get("data", []):
        if not isinstance(row, Mapping):
            continue
        try:
            result.add(int(row.get("id")))
        except (TypeError, ValueError):
            raise SystemExit("TheirStack returned a job without an integer ID.") from None
    return result


def _manifest_returned_ids(manifest: Mapping[str, Any]) -> set[int]:
    return {
        int(job_id)
        for batch in [*manifest.get("batches", []), *manifest.get("top_up_batches", [])]
        for job_id in batch.get("returned_job_ids", [])
    }


def _fetched_job_ids(
    manifest: Mapping[str, Any],
    cache: TheirStackRequestCache,
) -> set[int]:
    result = _manifest_returned_ids(manifest)
    for batch in [*manifest.get("batches", []), *manifest.get("top_up_batches", [])]:
        payload = cache.load("POST", SEARCH_URL, batch.get("body") or {})
        if payload is not None:
            result.update(_response_job_ids(payload))
    return result


def _top_up_batch(manifest: dict[str, Any], reserve: list[int]) -> dict[str, Any]:
    topups = manifest.setdefault("top_up_batches", [])
    if topups:
        existing = topups[0]
        if existing.get("job_ids") != reserve and existing.get("state") != "fetched":
            raise SystemExit("Existing reserve top-up scope differs; use a new manifest.")
        return existing
    batch = {
        "index": "top-up-1",
        "job_ids": reserve,
        "body": paid_search_body(reserve),
        "state": "pending",
    }
    topups.append(batch)
    return batch


def _has_paid_progress(manifest: Mapping[str, Any]) -> bool:
    return any(
        batch.get("state") != "pending"
        for batch in [*manifest.get("batches", []), *manifest.get("top_up_batches", [])]
    )


def _touch_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    write_status(path, manifest)


if __name__ == "__main__":
    main()
