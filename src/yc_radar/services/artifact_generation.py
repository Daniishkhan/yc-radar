"""Coordinate and atomically publish local queue-generation artifacts."""

from __future__ import annotations

import csv
import errno
import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO


ARTIFACT_GENERATION_LOCK_NAME = ".queue-artifact-generation.lock"


class ArtifactGenerationLocked(RuntimeError):
    """Raised when another local queue generator owns the shared advisory lock."""


def artifact_generation_lock_path(*, output_dir: Path, local_dir: Path) -> Path:
    """Return one shared lock beneath ``data/local`` or an isolated custom output.

    Normal production outputs are descendants of the configured local-data directory, so every
    dated/current run shares one lock. Tests and intentional outputs outside that tree keep their
    lock in the custom output directory rather than mutating the repository's real ``data/local``.
    """
    resolved_output = output_dir.expanduser().resolve()
    resolved_local = local_dir.expanduser().resolve()
    lock_root = (
        resolved_local
        if resolved_output == resolved_local or resolved_output.is_relative_to(resolved_local)
        else resolved_output
    )
    return lock_root / ARTIFACT_GENERATION_LOCK_NAME


@contextmanager
def artifact_generation_lock(*, output_dir: Path, local_dir: Path) -> Iterator[Path]:
    """Acquire the shared queue-generator lock without waiting."""
    lock_path = artifact_generation_lock_path(output_dir=output_dir, local_dir=local_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise ArtifactGenerationLocked(
                f"another queue artifact generator is already running (lock: {lock_path})"
            ) from exc
        try:
            yield lock_path
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def atomic_text_writer(path: Path, *, newline: str | None = None) -> Iterator[TextIO]:
    """Write a text artifact completely before atomically replacing its destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", newline=newline, encoding="utf-8") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    default: Callable[[Any], Any] | None = None,
    trailing_newline: bool = False,
) -> None:
    """Serialize JSON directly into a same-directory temporary file and publish it."""
    with atomic_text_writer(path) as handle:
        json.dump(
            payload,
            handle,
            indent=indent,
            sort_keys=sort_keys,
            default=default,
        )
        if trailing_newline:
            handle.write("\n")


def atomic_write_csv(
    path: Path,
    *,
    fieldnames: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """Write CSV rows into a same-directory temporary file and publish it."""
    with atomic_text_writer(path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
