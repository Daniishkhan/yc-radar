#!/usr/bin/env python3
"""Query one Common Crawl URL Index partition for public Greenhouse boards."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import boto3
import httpx
from botocore.config import Config

from yc_radar.services.commoncrawl_greenhouse import (
    CRAWL_RE,
    IDENTIFIER_RE,
    build_candidate_query,
    build_partition_query,
)

COLLECTIONS_URL = "https://index.commoncrawl.org/collinfo.json"
DEFAULT_DATABASE = "radar_commoncrawl"
DEFAULT_WORKGROUP = "radar-commoncrawl"
MANIFEST_VERSION = 1
MAX_API_ERRORS = 5
MAX_QUERY_ATTEMPTS = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export public Greenhouse board candidates from one Common Crawl partition."
    )
    parser.add_argument(
        "--profile",
        default=None,
        help=(
            "Optional shared AWS profile. Omit on EC2 to use the instance role or to use the "
            "standard AWS credential chain."
        ),
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--workgroup", default=DEFAULT_WORKGROUP)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument(
        "--crawl",
        help="Common Crawl ID such as CC-MAIN-2026-30; defaults to the latest published crawl.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Checkpoint manifest path; defaults to <output>.manifest.json.",
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
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
    manifest_path = args.manifest or output.with_suffix(f"{output.suffix}.manifest.json")

    scope = {
        "region": args.region,
        "workgroup": args.workgroup,
        "database": args.database,
        "crawl": crawl,
    }
    manifest = load_or_create_manifest(manifest_path, scope)
    athena, s3 = make_aws_clients(profile=args.profile, region=args.region)

    common = {
        "client": athena,
        "manifest": manifest,
        "manifest_path": manifest_path,
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
    run_query(table_sql, stage="create_url_index", **common)
    run_query(
        build_partition_query(args.database, crawl),
        stage="register_partition",
        **common,
    )
    result = run_query(
        build_candidate_query(args.database, crawl),
        stage="export_candidates",
        **common,
    )
    output_location = str(result["ResultConfiguration"]["OutputLocation"])
    download_result(
        s3,
        output_location=output_location,
        output=output,
        manifest=manifest,
        manifest_path=manifest_path,
    )
    bytes_scanned = int(result.get("Statistics", {}).get("DataScannedInBytes") or 0)
    estimated_usd = bytes_scanned / 1_000_000_000_000 * 5
    print(
        f"crawl={crawl} bytes_scanned={bytes_scanned} "
        f"estimated_query_usd={estimated_usd:.6f} output={output} "
        f"manifest={manifest_path}"
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


def make_aws_clients(*, profile: str | None, region: str) -> tuple[Any, Any]:
    """Create clients from an optional profile or the standard EC2-compatible chain."""
    session_options: dict[str, str] = {"region_name": region}
    if profile:
        session_options["profile_name"] = profile
    session = boto3.Session(**session_options)
    client_config = Config(
        retries={"max_attempts": 8, "mode": "standard"},
        user_agent_extra="yc-radar-commoncrawl/1",
    )
    return (
        session.client("athena", region_name=region, config=client_config),
        session.client("s3", region_name=region, config=client_config),
    )


def run_query(
    query: str,
    *,
    stage: str,
    client: Any,
    manifest: dict[str, Any],
    manifest_path: Path,
    region: str,
    workgroup: str,
    database: str,
    poll_seconds: float,
    max_api_errors: int = MAX_API_ERRORS,
    max_query_attempts: int = MAX_QUERY_ATTEMPTS,
) -> dict[str, Any]:
    """Run or resume one idempotent Athena stage and durably record its state."""
    if max_api_errors < 1 or max_query_attempts < 1:
        raise ValueError("retry bounds must be positive")
    identity = stable_digest(
        {
            "kind": "athena-query",
            "stage": stage,
            "region": region,
            "workgroup": workgroup,
            "database": database,
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        }
    )
    stage_checkpoint = ensure_query_stage(manifest, stage=stage, identity=identity)

    while True:
        attempt = current_or_new_attempt(
            stage_checkpoint,
            identity=identity,
            max_query_attempts=max_query_attempts,
        )
        query_id = attempt.get("query_execution_id")
        if not query_id:
            query_id = start_query(
                client,
                query=query,
                database=database,
                workgroup=workgroup,
                attempt=attempt,
                stage_checkpoint=stage_checkpoint,
                manifest=manifest,
                manifest_path=manifest_path,
                poll_seconds=poll_seconds,
                max_api_errors=max_api_errors,
            )

        result = poll_query(
            client,
            query_id=str(query_id),
            attempt=attempt,
            stage_checkpoint=stage_checkpoint,
            manifest=manifest,
            manifest_path=manifest_path,
            poll_seconds=poll_seconds,
            max_api_errors=max_api_errors,
        )
        state = str(result["Status"]["State"])
        if state == "SUCCEEDED":
            return result

        status = result["Status"]
        athena_error = status.get("AthenaError") or {}
        retryable = bool(athena_error.get("Retryable"))
        if retryable and len(stage_checkpoint["attempts"]) < max_query_attempts:
            continue
        reason = status.get("StateChangeReason") or "unknown Athena error"
        raise RuntimeError(f"Athena query {query_id} {state.lower()}: {reason}")


def ensure_query_stage(
    manifest: dict[str, Any], *, stage: str, identity: str
) -> dict[str, Any]:
    stages = manifest.setdefault("stages", {})
    checkpoint = stages.get(stage)
    if checkpoint is None:
        checkpoint = {"identity": identity, "state": "PENDING", "attempts": []}
        stages[stage] = checkpoint
    elif checkpoint.get("identity") != identity:
        raise RuntimeError(
            f"Athena checkpoint identity changed for stage {stage!r}; use a new manifest"
        )
    if not isinstance(checkpoint.get("attempts"), list):
        raise RuntimeError(f"Athena checkpoint stage {stage!r} is malformed")
    return checkpoint


def current_or_new_attempt(
    stage_checkpoint: dict[str, Any],
    *,
    identity: str,
    max_query_attempts: int,
) -> dict[str, Any]:
    attempts = stage_checkpoint["attempts"]
    if attempts:
        attempt = attempts[-1]
        state = attempt.get("state")
        if state not in {"FAILED", "CANCELLED"}:
            return attempt
        retryable = bool(attempt.get("retryable"))
        if not retryable or len(attempts) >= max_query_attempts:
            reason = attempt.get("state_change_reason") or "unknown Athena error"
            query_id = attempt.get("query_execution_id") or "unknown"
            raise RuntimeError(f"Athena query {query_id} {str(state).lower()}: {reason}")

    attempt_number = len(attempts) + 1
    token = stable_digest(
        {
            "kind": "athena-client-request-token",
            "stage_identity": identity,
            "attempt": attempt_number,
        }
    )
    attempt = {
        "attempt": attempt_number,
        "client_request_token": token,
        "state": "STARTING",
    }
    attempts.append(attempt)
    stage_checkpoint["state"] = "STARTING"
    stage_checkpoint["attempt"] = attempt_number
    return attempt


def start_query(
    client: Any,
    *,
    query: str,
    database: str,
    workgroup: str,
    attempt: dict[str, Any],
    stage_checkpoint: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    poll_seconds: float,
    max_api_errors: int,
) -> str:
    # Persist the token first. If the process dies after Athena accepts the
    # request but before the ID is stored, the same token recovers that ID.
    manifest["updated_at"] = utc_now()
    write_manifest(manifest_path, manifest)
    for error_number in range(1, max_api_errors + 1):
        try:
            started = client.start_query_execution(
                QueryString=query,
                QueryExecutionContext={
                    "Database": database,
                    "Catalog": "AwsDataCatalog",
                },
                WorkGroup=workgroup,
                ClientRequestToken=attempt["client_request_token"],
            )
        except Exception as exc:
            attempt["state"] = "START_ERROR"
            attempt["last_error"] = error_payload(exc)
            stage_checkpoint["state"] = "START_ERROR"
            manifest["updated_at"] = utc_now()
            write_manifest(manifest_path, manifest)
            if error_number == max_api_errors:
                raise RuntimeError(
                    f"Athena start failed after {max_api_errors} attempts: {exc}"
                ) from exc
            time.sleep(retry_delay(poll_seconds, error_number))
            continue

        query_id = str(started["QueryExecutionId"])
        attempt.update(
            {
                "query_execution_id": query_id,
                "state": "QUEUED",
                "submitted_at": utc_now(),
            }
        )
        attempt.pop("last_error", None)
        stage_checkpoint.update(
            {
                "query_execution_id": query_id,
                "state": "QUEUED",
                "attempt": attempt["attempt"],
            }
        )
        manifest["updated_at"] = utc_now()
        write_manifest(manifest_path, manifest)
        return query_id
    raise AssertionError("unreachable")


def poll_query(
    client: Any,
    *,
    query_id: str,
    attempt: dict[str, Any],
    stage_checkpoint: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    poll_seconds: float,
    max_api_errors: int,
) -> dict[str, Any]:
    consecutive_errors = 0
    while True:
        try:
            result = client.get_query_execution(QueryExecutionId=query_id)[
                "QueryExecution"
            ]
        except Exception as exc:
            consecutive_errors += 1
            attempt["state"] = "POLL_ERROR"
            attempt["last_error"] = error_payload(exc)
            stage_checkpoint["state"] = "POLL_ERROR"
            manifest["updated_at"] = utc_now()
            write_manifest(manifest_path, manifest)
            if consecutive_errors >= max_api_errors:
                raise RuntimeError(
                    f"Athena query {query_id} polling failed after "
                    f"{max_api_errors} consecutive errors: {exc}"
                ) from exc
            time.sleep(retry_delay(poll_seconds, consecutive_errors))
            continue

        consecutive_errors = 0
        status = result["Status"]
        state = str(status["State"])
        attempt["state"] = state
        attempt.pop("last_error", None)
        stage_checkpoint["state"] = state
        stage_checkpoint["query_execution_id"] = query_id
        if state in {"FAILED", "CANCELLED"}:
            athena_error = status.get("AthenaError") or {}
            attempt["retryable"] = bool(athena_error.get("Retryable"))
            attempt["state_change_reason"] = (
                status.get("StateChangeReason") or "unknown Athena error"
            )
        if state == "SUCCEEDED":
            attempt["finished_at"] = utc_now()
            stage_checkpoint["finished_at"] = attempt["finished_at"]
            stage_checkpoint["statistics"] = result.get("Statistics") or {}
            stage_checkpoint["output_location"] = str(
                result.get("ResultConfiguration", {}).get("OutputLocation") or ""
            )
        manifest["updated_at"] = utc_now()
        write_manifest(manifest_path, manifest)

        if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return result
        if state not in {"QUEUED", "RUNNING"}:
            raise RuntimeError(f"Athena query {query_id} returned unknown state {state!r}")
        time.sleep(poll_seconds)


def download_result(
    client: Any,
    *,
    output_location: str,
    output: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> None:
    """Download an Athena result through S3 and atomically publish the local CSV."""
    bucket, key = parse_s3_uri(output_location)
    identity = stable_digest(
        {"kind": "athena-result-download", "output_location": output_location}
    )
    checkpoint = manifest.get("download")
    if checkpoint and checkpoint.get("identity") == identity:
        expected_digest = checkpoint.get("sha256")
        if (
            checkpoint.get("state") == "SUCCEEDED"
            and isinstance(expected_digest, str)
            and output.is_file()
            and file_digest(output) == expected_digest
        ):
            return

    checkpoint = {
        "identity": identity,
        "state": "DOWNLOADING",
        "output_location": output_location,
    }
    manifest["download"] = checkpoint
    manifest["updated_at"] = utc_now()
    write_manifest(manifest_path, manifest)

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    digest = hashlib.sha256()
    size = 0
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        with os.fdopen(descriptor, "wb") as handle:
            while chunk := body.read(1024 * 1024):
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output)
        fsync_directory(output.parent)
    except BaseException as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        checkpoint["state"] = "FAILED"
        checkpoint["error"] = error_payload(exc)
        manifest["updated_at"] = utc_now()
        write_manifest(manifest_path, manifest)
        raise

    checkpoint.update(
        {
            "state": "SUCCEEDED",
            "bytes": size,
            "sha256": digest.hexdigest(),
            "finished_at": utc_now(),
        }
    )
    checkpoint.pop("error", None)
    manifest["updated_at"] = utc_now()
    write_manifest(manifest_path, manifest)


def parse_s3_uri(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"Athena returned an invalid S3 output location: {value!r}")
    return parsed.netloc, unquote(parsed.path.lstrip("/"))


def load_or_create_manifest(path: Path, scope: dict[str, str]) -> dict[str, Any]:
    scope_identity = stable_digest(
        {"kind": "commoncrawl-greenhouse-export", "scope": scope}
    )
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Athena checkpoint manifest is unreadable: {path}") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("stages"), dict):
            raise RuntimeError(f"Athena checkpoint manifest is malformed: {path}")
        if (
            manifest.get("version") != MANIFEST_VERSION
            or manifest.get("scope_identity") != scope_identity
        ):
            raise RuntimeError(
                f"Athena checkpoint manifest does not match this query scope: {path}; "
                "use a different output/manifest path"
            )
        return manifest

    manifest = {
        "version": MANIFEST_VERSION,
        "scope": scope,
        "scope_identity": scope_identity,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "stages": {},
    }
    write_manifest(path, manifest)
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def retry_delay(poll_seconds: float, error_number: int) -> float:
    return min(30.0, poll_seconds * (2 ** (error_number - 1)))


def error_payload(error: BaseException) -> dict[str, str]:
    return {"class": type(error).__name__, "message": str(error)}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    main()
