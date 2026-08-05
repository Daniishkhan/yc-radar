"""Read queue artifacts without coupling operational tools to one generator."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


QUEUE_ALIASES = {
    "application": "application_queue",
    "applications": "application_queue",
    "application_queue": "application_queue",
    "jobs_to_apply": "application_queue",
    "apply": "application_queue",
    "verification": "verification_queue",
    "verification_queue": "verification_queue",
    "jobs_to_verify": "verification_queue",
    "verify": "verification_queue",
    "company_outreach": "company_outreach_queue",
    "company_outreach_queue": "company_outreach_queue",
    "outreach": "company_outreach_queue",
    "targets": "company_outreach_queue",
    "weekly_targets": "company_outreach_queue",
}

RUN_DIRECTORY_CANDIDATES = {
    "application_queue": ("application_queue", "jobs_to_apply"),
    "verification_queue": ("verification_queue", "jobs_to_verify"),
    "company_outreach_queue": ("company_outreach_queue", "weekly_targets"),
}


def canonical_queue_name(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in QUEUE_ALIASES:
        return QUEUE_ALIASES[normalized]
    if not normalized or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in normalized):
        raise ValueError(f"invalid queue name: {value!r}")
    return normalized


def parse_queue_spec(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or not raw_path.strip():
        raise ValueError("queue must use NAME=PATH")
    return canonical_queue_name(name), Path(raw_path).expanduser()


def discover_queue_artifacts(run_dir: Path) -> list[tuple[str, Path]]:
    """Choose at most one JSON/CSV artifact per known queue in a run directory."""
    discovered: list[tuple[str, Path]] = []
    for queue_name, stems in RUN_DIRECTORY_CANDIDATES.items():
        selected: Path | None = None
        for stem in stems:
            for suffix in (".json", ".csv"):
                candidate = run_dir / f"{stem}{suffix}"
                if candidate.is_file():
                    selected = candidate
                    break
            if selected is not None:
                break
        if selected is not None:
            discovered.append((queue_name, selected))
    return discovered


def load_queues(
    queue_artifacts: Iterable[tuple[str | None, Path]],
) -> dict[str, list[dict[str, Any]]]:
    queues: dict[str, list[dict[str, Any]]] = {}
    for requested_name, artifact_path in queue_artifacts:
        loaded = read_queue_artifact(artifact_path, requested_queue=requested_name)
        for queue_name, rows in loaded.items():
            queues.setdefault(queue_name, []).extend(rows)
    return queues


def read_queue_artifact(
    artifact_path: Path,
    *,
    requested_queue: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Read CSV, a JSON row list, or a JSON object containing named row lists."""
    requested = canonical_queue_name(requested_queue) if requested_queue else None
    suffix = artifact_path.suffix.lower()
    if suffix == ".csv":
        with artifact_path.open(newline="", encoding="utf-8-sig") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
        queue_name = requested or canonical_queue_name(artifact_path.stem)
        return {queue_name: rows}
    if suffix != ".json":
        raise ValueError(f"unsupported queue artifact type: {artifact_path}")

    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON queue artifact {artifact_path}: {exc}") from exc

    if isinstance(payload, list):
        queue_name = requested or canonical_queue_name(artifact_path.stem)
        return {queue_name: _mapping_rows(payload, artifact_path)}
    if not isinstance(payload, dict):
        raise ValueError(f"queue artifact must contain an object or row list: {artifact_path}")

    named_lists = _named_row_lists(payload)
    if requested is not None:
        matching = [
            (name, rows)
            for name, rows in named_lists
            if canonical_queue_name(name) == requested
        ]
        if matching:
            return {requested: _mapping_rows(matching[0][1], artifact_path)}
        if len(named_lists) == 1:
            return {requested: _mapping_rows(named_lists[0][1], artifact_path)}
        raise ValueError(
            f"JSON artifact {artifact_path} has no row list for queue {requested!r}"
        )

    queues: dict[str, list[dict[str, Any]]] = {}
    for name, rows in named_lists:
        queue_name = (
            canonical_queue_name(artifact_path.stem)
            if name == "jobs"
            else canonical_queue_name(name)
        )
        if queue_name in queues:
            raise ValueError(f"JSON artifact repeats queue {queue_name!r}: {artifact_path}")
        queues[queue_name] = _mapping_rows(rows, artifact_path)
    if not queues:
        raise ValueError(f"JSON artifact contains no recognized row lists: {artifact_path}")
    return queues


def _named_row_lists(payload: Mapping[str, Any]) -> list[tuple[str, Sequence[Any]]]:
    recognized = {
        "jobs",
        "targets",
        *QUEUE_ALIASES,
    }
    return [
        (str(name), value)
        for name, value in payload.items()
        if name in recognized and isinstance(value, list)
    ]


def _mapping_rows(values: Sequence[Any], artifact_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise ValueError(
                f"queue artifact row {index} is not an object: {artifact_path}"
            )
        rows.append(dict(value))
    return rows
