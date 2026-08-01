#!/usr/bin/env python3
"""Union per-crawl Common Crawl Greenhouse candidate exports without losing provenance."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yc_radar.adapters.greenhouse import GreenhouseAdapter
from yc_radar.services.commoncrawl_greenhouse import CRAWL_RE

CRAWL_ID_IN_FILENAME_RE = re.compile(r"CC-MAIN-\d{4}-\d{2}")
REQUIRED_INPUT_FIELDS = frozenset(
    {
        "board_token",
        "canonical_source_url",
        "example_observed_url",
        "observation_count",
        "first_observed_at",
        "last_observed_at",
    }
)
UNION_FIELDS = [
    "board_token",
    "canonical_source_url",
    "example_observed_url",
    "observation_count",
    "first_observed_at",
    "last_observed_at",
    "first_seen_crawl",
    "last_seen_crawl",
    "crawl_count",
    "crawl_ids",
]
EVIDENCE_FIELDS = ["crawl_id", *UNION_FIELDS[:6]]
RESERVED_BOARD_TOKENS = frozenset({"embed", "internal", "v1"})
MANIFEST_VERSION = 1
GREENHOUSE_ADAPTER = GreenhouseAdapter()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Union per-crawl Common Crawl Greenhouse CSVs into a scout input while "
            "retaining exact token-by-crawl evidence."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Per-crawl candidate CSVs; each filename must contain one unique CC-MAIN ID.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/local/debug/greenhouse_board_candidates_union.csv"),
        help="Scout-compatible one-row-per-token CSV.",
    )
    parser.add_argument(
        "--evidence-output",
        type=Path,
        help="One-row-per-token-per-crawl CSV; defaults to <output>.evidence.csv.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Atomic JSON manifest; defaults to <output>.manifest.json.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    evidence_output = args.evidence_output or args.output.with_suffix(".evidence.csv")
    manifest_path = args.manifest or args.output.with_suffix(f"{args.output.suffix}.manifest.json")
    try:
        validate_output_paths(
            inputs=args.inputs,
            output=args.output,
            evidence_output=evidence_output,
            manifest_path=manifest_path,
        )
        union_rows, evidence_rows, input_summaries = union_candidate_files(args.inputs)
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        raise SystemExit(str(exc)) from exc

    write_csv_atomic(args.output, UNION_FIELDS, union_rows)
    write_csv_atomic(evidence_output, EVIDENCE_FIELDS, evidence_rows)
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": input_summaries,
        "union_token_count": len(union_rows),
        "evidence_row_count": len(evidence_rows),
        "total_observation_count": sum(
            int(row["observation_count"]) for row in union_rows
        ),
        "outputs": {
            "candidates": output_manifest(args.output, row_count=len(union_rows)),
            "crawl_evidence": output_manifest(
                evidence_output, row_count=len(evidence_rows)
            ),
        },
    }
    write_json_atomic(manifest_path, manifest)
    print(
        f"inputs={len(args.inputs)} crawls={len(input_summaries)} "
        f"union_tokens={len(union_rows)} evidence_rows={len(evidence_rows)} "
        f"output={args.output} evidence={evidence_output} manifest={manifest_path}"
    )


def validate_output_paths(
    *,
    inputs: list[Path],
    output: Path,
    evidence_output: Path,
    manifest_path: Path,
) -> None:
    input_paths = {path.resolve() for path in inputs}
    output_paths = [output.resolve(), evidence_output.resolve(), manifest_path.resolve()]
    if len(set(output_paths)) != len(output_paths):
        raise ValueError("--output, --evidence-output, and --manifest must be different paths")
    overlap = input_paths.intersection(output_paths)
    if overlap:
        raise ValueError(f"refusing to overwrite an input CSV: {min(map(str, overlap))}")


def union_candidate_files(
    paths: list[Path],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not paths:
        raise ValueError("at least one per-crawl candidate CSV is required")

    crawl_ids: set[str] = set()
    token_crawls: dict[tuple[str, str], dict[str, Any]] = {}
    input_summaries: list[dict[str, Any]] = []
    tokens_seen: set[str] = set()
    crawl_input_order: dict[str, int] = {}

    for input_index, path in enumerate(paths):
        crawl_id = crawl_id_from_filename(path)
        if crawl_id in crawl_ids:
            raise ValueError(f"crawl {crawl_id} is supplied more than once")
        crawl_ids.add(crawl_id)
        crawl_input_order[crawl_id] = input_index
        input_sha256 = file_digest(path)
        rows, raw_row_count = load_crawl_candidates(path, crawl_id=crawl_id)
        if file_digest(path) != input_sha256:
            raise ValueError(f"candidate input changed while it was being read: {path}")
        marginal_tokens = set(rows).difference(tokens_seen)
        tokens_seen.update(rows)
        input_summaries.append(
            {
                "path": str(path.resolve()),
                "sha256": input_sha256,
                "crawl_id": crawl_id,
                "input_row_count": raw_row_count,
                "token_count": len(rows),
                "observation_count": sum(
                    int(row["observation_count"]) for row in rows.values()
                ),
                "marginal_new_tokens": len(marginal_tokens),
            }
        )
        for token, row in rows.items():
            token_crawls[(token, crawl_id)] = row

    evidence_rows = sorted(
        token_crawls.values(),
        key=lambda row: (crawl_input_order[str(row["crawl_id"])], str(row["board_token"])),
    )
    union_rows = aggregate_union_rows(evidence_rows)
    return union_rows, evidence_rows, input_summaries


def crawl_id_from_filename(path: Path) -> str:
    matches = CRAWL_ID_IN_FILENAME_RE.findall(path.name)
    if len(matches) != 1:
        raise ValueError(
            f"input filename must contain exactly one unique CC-MAIN ID: {path}"
        )
    return matches[0]


def load_crawl_candidates(
    path: Path,
    *,
    crawl_id: str,
) -> tuple[dict[str, dict[str, Any]], int]:
    if not path.is_file():
        raise ValueError(f"candidate input does not exist or is not a file: {path}")

    token_rows: dict[str, dict[str, Any]] = {}
    raw_row_count = 0
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        fieldnames = reader.fieldnames or []
        if len(fieldnames) != len(set(fieldnames)):
            raise ValueError(f"candidate CSV contains duplicate headers: {path}")
        missing = REQUIRED_INPUT_FIELDS.difference(fieldnames)
        if missing:
            raise ValueError(
                f"candidate CSV {path} is missing columns: {', '.join(sorted(missing))}"
            )
        for line_number, source_row in enumerate(reader, start=2):
            raw_row_count += 1
            if None in source_row:
                raise ValueError(f"candidate CSV {path}:{line_number} has extra values")
            row = normalized_evidence_row(
                source_row,
                crawl_id=crawl_id,
                path=path,
                line_number=line_number,
            )
            token = str(row["board_token"])
            current = token_rows.get(token)
            token_rows[token] = row if current is None else merge_crawl_rows(current, row)
    return token_rows, raw_row_count


def normalized_evidence_row(
    source_row: dict[str | None, str | None],
    *,
    crawl_id: str,
    path: Path,
    line_number: int,
) -> dict[str, Any]:
    prefix = f"candidate CSV {path}:{line_number}"
    token = str(source_row.get("board_token") or "").strip().lower()
    if token in RESERVED_BOARD_TOKENS:
        raise ValueError(f"{prefix} contains reserved board token {token!r}")
    try:
        canonical_url = GREENHOUSE_ADAPTER.canonical_source_url(token)
    except ValueError as exc:
        raise ValueError(f"{prefix} contains invalid board token {token!r}") from exc

    supplied_canonical = str(source_row.get("canonical_source_url") or "").strip()
    if GREENHOUSE_ADAPTER.extract_board_token(supplied_canonical) != token:
        raise ValueError(f"{prefix} canonical source URL does not match board token")
    example_url = str(source_row.get("example_observed_url") or "").strip()
    if GREENHOUSE_ADAPTER.extract_board_token(example_url) != token:
        raise ValueError(f"{prefix} example observed URL does not match board token")

    observation_count = positive_integer(
        source_row.get("observation_count"),
        field="observation_count",
        prefix=prefix,
    )
    first_observed = parsed_timestamp(
        source_row.get("first_observed_at"), field="first_observed_at", prefix=prefix
    )
    last_observed = parsed_timestamp(
        source_row.get("last_observed_at"), field="last_observed_at", prefix=prefix
    )
    if first_observed > last_observed:
        raise ValueError(f"{prefix} first_observed_at is after last_observed_at")

    return {
        "crawl_id": crawl_id,
        "board_token": token,
        "canonical_source_url": canonical_url,
        "example_observed_url": example_url,
        "observation_count": observation_count,
        "first_observed_at": format_timestamp(first_observed),
        "last_observed_at": format_timestamp(last_observed),
    }


def positive_integer(value: Any, *, field: str, prefix: str) -> int:
    raw = str(value or "").strip()
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{prefix} {field} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{prefix} {field} must be a positive integer")
    return parsed


def parsed_timestamp(value: Any, *, field: str, prefix: str) -> datetime:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{prefix} {field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def merge_crawl_rows(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    if current["crawl_id"] != incoming["crawl_id"]:
        raise ValueError("cannot merge crawl evidence from different crawls")
    return {
        **current,
        "example_observed_url": min(
            str(current["example_observed_url"]), str(incoming["example_observed_url"])
        ),
        "observation_count": int(current["observation_count"])
        + int(incoming["observation_count"]),
        "first_observed_at": min_timestamp(
            str(current["first_observed_at"]), str(incoming["first_observed_at"])
        ),
        "last_observed_at": max_timestamp(
            str(current["last_observed_at"]), str(incoming["last_observed_at"])
        ),
    }


def aggregate_union_rows(evidence_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in evidence_rows:
        grouped.setdefault(str(row["board_token"]), []).append(row)

    union_rows: list[dict[str, Any]] = []
    for token, rows in grouped.items():
        crawl_ids = sorted({str(row["crawl_id"]) for row in rows}, key=crawl_sort_key)
        union_rows.append(
            {
                "board_token": token,
                "canonical_source_url": GREENHOUSE_ADAPTER.canonical_source_url(token),
                "example_observed_url": min(
                    str(row["example_observed_url"]) for row in rows
                ),
                "observation_count": sum(int(row["observation_count"]) for row in rows),
                "first_observed_at": min_timestamp(
                    *(str(row["first_observed_at"]) for row in rows)
                ),
                "last_observed_at": max_timestamp(
                    *(str(row["last_observed_at"]) for row in rows)
                ),
                "first_seen_crawl": crawl_ids[0],
                "last_seen_crawl": crawl_ids[-1],
                "crawl_count": len(crawl_ids),
                "crawl_ids": json.dumps(crawl_ids, separators=(",", ":")),
            }
        )
    return sorted(
        union_rows,
        key=lambda row: (-int(row["observation_count"]), str(row["board_token"])),
    )


def crawl_sort_key(crawl_id: str) -> tuple[int, int]:
    if not CRAWL_RE.fullmatch(crawl_id):
        raise ValueError(f"invalid crawl ID: {crawl_id!r}")
    _, _, year, week = crawl_id.split("-")
    return int(year), int(week)


def min_timestamp(*values: str) -> str:
    return min(values, key=lambda value: parsed_timestamp(value, field="timestamp", prefix="row"))


def max_timestamp(*values: str) -> str:
    return max(values, key=lambda value: parsed_timestamp(value, field="timestamp", prefix="row"))


def write_csv_atomic(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def output_manifest(path: Path, *, row_count: int) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "row_count": row_count,
        "bytes": path.stat().st_size,
        "sha256": file_digest(path),
    }


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


if __name__ == "__main__":
    main()
