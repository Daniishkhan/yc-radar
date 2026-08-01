#!/usr/bin/env python3
"""Run the resumable Greenhouse history backfill as one detached worker job.

The child scripts remain responsible for their own durable checkpoints. This wrapper only
coordinates their order and records enough content fingerprints to safely skip a completed
stage after the detached job is restarted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = 1
SCRIPTS_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPTS_DIR.parent
PERSISTENT_SCOUT_CACHE = (
    REPOSITORY_ROOT / "data" / "local" / "cache" / "greenhouse_source_scout"
)
PERSISTENT_RESOLVER_CACHE = (
    REPOSITORY_ROOT / "data" / "local" / "cache" / "greenhouse_domain_resolver.json"
)

TOP_LEVEL_STATUS_NAME = "history-backfill.status.json"
UNION_MANIFEST_NAME = "greenhouse-candidate-union.manifest.json"
SCOUT_OUTPUT_NAME = "greenhouse-board-verification.csv"
SCOUT_STATUS_NAME = "greenhouse-scout.status.json"
RESOLVER_OUTPUT_NAME = "greenhouse-domain-resolution.csv"
RESOLVER_STATUS_NAME = "greenhouse-domain-resolver.status.json"
SYNC_CHECKPOINT_NAME = "greenhouse-sync.checkpoint.json"
SYNC_STATUS_NAME = "greenhouse-sync.status.json"


@dataclass(frozen=True)
class Stage:
    name: str
    command: tuple[str, ...]
    inputs: tuple[Path, ...]
    outputs: tuple[Path, ...]
    resumable_terminal_outputs: tuple[Path, ...] = ()


@dataclass(frozen=True)
class BackfillConfig:
    candidate_inputs: tuple[Path, ...]
    run_dir: Path
    union_output: Path
    evidence_output: Path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sequentially union, verify, resolve, and sync historical Greenhouse "
            "Common Crawl candidates with restart-safe top-level status."
        )
    )
    parser.add_argument(
        "candidate_inputs",
        nargs="+",
        type=Path,
        help="Two or more per-crawl Greenhouse candidate CSVs.",
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="Unique directory for this run's manifests, outputs, and status files.",
    )
    parser.add_argument(
        "--union-output",
        required=True,
        type=Path,
        help="Explicit one-row-per-board union CSV output.",
    )
    parser.add_argument(
        "--evidence-output",
        required=True,
        type=Path,
        help="Explicit one-row-per-board-per-crawl evidence CSV output.",
    )
    args = parser.parse_args(argv)
    if len(args.candidate_inputs) < 2:
        parser.error("at least two per-crawl candidate CSVs are required")
    return args


def normalized_config(args: argparse.Namespace) -> BackfillConfig:
    candidate_inputs = tuple(path.resolve() for path in args.candidate_inputs)
    if len(candidate_inputs) < 2:
        raise ValueError("at least two per-crawl candidate CSVs are required")
    if len(set(candidate_inputs)) != len(candidate_inputs):
        raise ValueError("candidate input paths must be unique")

    run_dir = args.run_dir.resolve()
    union_output = args.union_output.resolve()
    evidence_output = args.evidence_output.resolve()
    if union_output == evidence_output:
        raise ValueError("--union-output and --evidence-output must be different paths")
    fixed_outputs = run_artifact_paths(run_dir)
    all_outputs = [union_output, evidence_output, *fixed_outputs]
    if len(set(all_outputs)) != len(all_outputs):
        raise ValueError("union/evidence outputs must not overlap run-directory artifacts")
    if set(candidate_inputs).intersection(all_outputs):
        raise ValueError("refusing to overwrite a candidate input")
    return BackfillConfig(
        candidate_inputs=candidate_inputs,
        run_dir=run_dir,
        union_output=union_output,
        evidence_output=evidence_output,
    )


def run_artifact_paths(run_dir: Path) -> tuple[Path, ...]:
    scout_output = run_dir / SCOUT_OUTPUT_NAME
    resolver_output = run_dir / RESOLVER_OUTPUT_NAME
    return (
        run_dir / TOP_LEVEL_STATUS_NAME,
        run_dir / UNION_MANIFEST_NAME,
        scout_output,
        scout_output.with_suffix(".partial.csv"),
        scout_output.with_suffix(".checkpoint.json"),
        run_dir / SCOUT_STATUS_NAME,
        resolver_output,
        resolver_output.with_suffix(".partial.csv"),
        resolver_output.with_suffix(".checkpoint.json"),
        run_dir / RESOLVER_STATUS_NAME,
        run_dir / SYNC_CHECKPOINT_NAME,
        run_dir / SYNC_STATUS_NAME,
    )


def build_stages(config: BackfillConfig) -> list[Stage]:
    run_dir = config.run_dir
    union_manifest = run_dir / UNION_MANIFEST_NAME
    scout_output = run_dir / SCOUT_OUTPUT_NAME
    scout_status = run_dir / SCOUT_STATUS_NAME
    resolver_output = run_dir / RESOLVER_OUTPUT_NAME
    resolver_status = run_dir / RESOLVER_STATUS_NAME
    sync_checkpoint = run_dir / SYNC_CHECKPOINT_NAME
    sync_status = run_dir / SYNC_STATUS_NAME

    union_command = (
        sys.executable,
        str(SCRIPTS_DIR / "union_commoncrawl_greenhouse.py"),
        *(str(path) for path in config.candidate_inputs),
        "--output",
        str(config.union_output),
        "--evidence-output",
        str(config.evidence_output),
        "--manifest",
        str(union_manifest),
    )
    scout_command = (
        sys.executable,
        str(SCRIPTS_DIR / "scout_greenhouse_sources.py"),
        "--input",
        str(config.union_output),
        "--output",
        str(scout_output),
        "--cache-dir",
        str(PERSISTENT_SCOUT_CACHE),
        "--status-file",
        str(scout_status),
        "--delay-seconds",
        "1",
        "--checkpoint-every",
        "25",
        "--apply",
    )
    resolver_command = (
        sys.executable,
        str(SCRIPTS_DIR / "resolve_greenhouse_domains.py"),
        "--input",
        str(scout_output),
        "--output",
        str(resolver_output),
        "--status-file",
        str(resolver_status),
        "--cache-file",
        str(PERSISTENT_RESOLVER_CACHE),
        "--delay-seconds",
        "1",
        "--retry-delay-seconds",
        "2",
        "--max-attempts",
        "3",
        "--company-timeout-seconds",
        "120",
        "--checkpoint-every",
        "10",
        "--apply",
    )
    sync_command = (
        sys.executable,
        str(SCRIPTS_DIR / "sync_job_sources.py"),
        "sync",
        "--provider",
        "greenhouse",
        "--checkpoint-file",
        str(sync_checkpoint),
        "--status-file",
        str(sync_status),
        "--delay-seconds",
        "1",
        "--max-attempts",
        "4",
    )
    return [
        Stage(
            name="union",
            command=union_command,
            inputs=config.candidate_inputs,
            outputs=(config.union_output, config.evidence_output, union_manifest),
        ),
        Stage(
            name="scout",
            command=scout_command,
            inputs=(config.union_output,),
            outputs=(scout_output, scout_status),
        ),
        Stage(
            name="resolver",
            command=resolver_command,
            inputs=(scout_output,),
            outputs=(resolver_output, resolver_status),
            resumable_terminal_outputs=(
                resolver_output.with_suffix(".partial.csv"),
                resolver_status,
            ),
        ),
        Stage(
            name="sync",
            command=sync_command,
            inputs=(scout_output, resolver_output),
            outputs=(sync_checkpoint, sync_status),
        ),
    ]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def file_fingerprint(path: Path, *, required: bool = True) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        before = resolved.stat()
    except FileNotFoundError:
        if required:
            raise
        return {"path": str(resolved), "exists": False}
    if not resolved.is_file():
        raise ValueError(f"expected a regular file: {resolved}")

    digest = hashlib.sha256()
    with resolved.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    after = resolved.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError(f"file changed while it was fingerprinted: {resolved}")
    return {
        "path": str(resolved),
        "exists": True,
        "size": after.st_size,
        "sha256": digest.hexdigest(),
    }


def fingerprint_files(
    paths: Sequence[Path],
    *,
    required: bool = True,
) -> list[dict[str, Any]]:
    return [file_fingerprint(path, required=required) for path in paths]


def observed_fingerprints(paths: Sequence[Path]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for path in paths:
        try:
            observations.append(file_fingerprint(path, required=False))
        except (OSError, RuntimeError, ValueError) as exc:
            observations.append(
                {
                    "path": str(path.resolve()),
                    "exists": path.exists(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return observations


def load_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "pending",
            "stages": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"top-level status is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"top-level status has an unsupported schema: {path}")
    if not isinstance(payload.get("stages"), dict):
        raise ValueError(f"top-level status has a malformed stage map: {path}")
    return payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(payload, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def completed_stage_is_reusable(stage: Stage, prior: Any) -> bool:
    if not isinstance(prior, dict):
        return False
    if prior.get("state") != "completed" or prior.get("return_code") != 0:
        return False
    if prior.get("command") != list(stage.command):
        return False
    try:
        current_inputs = fingerprint_files(stage.inputs)
        current_outputs = fingerprint_files(stage.outputs)
    except (OSError, RuntimeError, ValueError):
        return False
    return prior.get("inputs") == current_inputs and prior.get("outputs") == current_outputs


def resolver_quota_checkpoint(stage: Stage, return_code: int) -> list[dict[str, Any]] | None:
    if stage.name != "resolver" or return_code != 0 or not stage.resumable_terminal_outputs:
        return None
    status_path = stage.resumable_terminal_outputs[-1]
    try:
        child_status = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(child_status, dict) or child_status.get("state") != "quota_exhausted":
            return None
        return fingerprint_files(stage.resumable_terminal_outputs)
    except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError):
        return None


def run_stage(stage: Stage, status: dict[str, Any], status_path: Path) -> int | None:
    prior = status["stages"].get(stage.name)
    if completed_stage_is_reusable(stage, prior):
        prior["last_verified_at"] = utc_now()
        status["updated_at"] = prior["last_verified_at"]
        write_json_atomic(status_path, status)
        print(f"Skipping completed {stage.name} stage; fingerprints still match.", flush=True)
        return 0

    started_at = utc_now()
    try:
        input_fingerprints = fingerprint_files(stage.inputs)
    except (OSError, RuntimeError, ValueError) as exc:
        status["stages"][stage.name] = {
            "state": "failed",
            "command": list(stage.command),
            "started_at": started_at,
            "finished_at": utc_now(),
            "return_code": None,
            "error": f"{type(exc).__name__}: {exc}",
            "inputs": observed_fingerprints(stage.inputs),
            "outputs": observed_fingerprints(stage.outputs),
        }
        status["updated_at"] = status["stages"][stage.name]["finished_at"]
        write_json_atomic(status_path, status)
        return 1

    status["stages"][stage.name] = {
        "state": "running",
        "command": list(stage.command),
        "started_at": started_at,
        "return_code": None,
        "inputs": input_fingerprints,
        "outputs": observed_fingerprints(stage.outputs),
    }
    status["updated_at"] = started_at
    write_json_atomic(status_path, status)

    try:
        process = subprocess.run(
            list(stage.command),
            cwd=REPOSITORY_ROOT,
            check=False,
        )
    except OSError as exc:
        finished_at = utc_now()
        status["stages"][stage.name] = {
            "state": "failed",
            "command": list(stage.command),
            "started_at": started_at,
            "finished_at": finished_at,
            "return_code": None,
            "error": f"{type(exc).__name__}: {exc}",
            "inputs": input_fingerprints,
            "outputs": observed_fingerprints(stage.outputs),
        }
        status["updated_at"] = finished_at
        write_json_atomic(status_path, status)
        return 1
    finished_at = utc_now()
    output_fingerprints = observed_fingerprints(stage.outputs)
    missing_outputs = [
        item["path"]
        for item in output_fingerprints
        if not item.get("exists") or not item.get("sha256")
    ]
    quota_checkpoint = (
        resolver_quota_checkpoint(stage, process.returncode) if missing_outputs else None
    )
    if quota_checkpoint is not None:
        status["stages"][stage.name] = {
            "state": "quota_exhausted",
            "command": list(stage.command),
            "started_at": started_at,
            "finished_at": finished_at,
            "return_code": process.returncode,
            "inputs": input_fingerprints,
            "outputs": output_fingerprints,
            "resume_outputs": quota_checkpoint,
        }
        status["updated_at"] = finished_at
        write_json_atomic(status_path, status)
        return None
    state = "completed" if process.returncode == 0 and not missing_outputs else "failed"
    record: dict[str, Any] = {
        "state": state,
        "command": list(stage.command),
        "started_at": started_at,
        "finished_at": finished_at,
        "return_code": process.returncode,
        "inputs": input_fingerprints,
        "outputs": output_fingerprints,
    }
    if missing_outputs and process.returncode == 0:
        record["error"] = "child exited successfully without outputs: " + ", ".join(
            missing_outputs
        )
    status["stages"][stage.name] = record
    status["updated_at"] = finished_at
    write_json_atomic(status_path, status)
    if state == "completed":
        return 0
    return process.returncode if process.returncode != 0 else 1


def run(args: argparse.Namespace) -> int:
    try:
        config = normalized_config(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    config.run_dir.mkdir(parents=True, exist_ok=True)
    status_path = config.run_dir / TOP_LEVEL_STATUS_NAME
    try:
        status = load_status(status_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    started_at = utc_now()
    status.update(
        {
            "schema_version": SCHEMA_VERSION,
            "state": "running",
            "run_dir": str(config.run_dir),
            "started_at": status.get("started_at") or started_at,
            "attempt_started_at": started_at,
            "attempt_count": int(status.get("attempt_count") or 0) + 1,
            "updated_at": started_at,
        }
    )
    status.pop("finished_at", None)
    status.pop("return_code", None)
    status.pop("failed_stage", None)
    status.pop("paused_stage", None)
    write_json_atomic(status_path, status)

    for stage in build_stages(config):
        return_code = run_stage(stage, status, status_path)
        if return_code is None:
            finished_at = utc_now()
            status.update(
                {
                    "state": "quota_exhausted",
                    "paused_stage": stage.name,
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                    "return_code": 0,
                }
            )
            write_json_atomic(status_path, status)
            return 0
        if return_code:
            finished_at = utc_now()
            status.update(
                {
                    "state": "failed",
                    "failed_stage": stage.name,
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                    "return_code": return_code,
                }
            )
            write_json_atomic(status_path, status)
            return return_code

    finished_at = utc_now()
    status.update(
        {
            "state": "completed",
            "finished_at": finished_at,
            "updated_at": finished_at,
            "return_code": 0,
        }
    )
    status.pop("failed_stage", None)
    write_json_atomic(status_path, status)
    return 0


def main() -> None:
    return_code = run(parse_args())
    if return_code < 0:
        return_code = 128 + abs(return_code)
    raise SystemExit(return_code)


if __name__ == "__main__":
    main()
