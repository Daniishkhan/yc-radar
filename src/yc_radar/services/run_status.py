"""Atomic local stage-status artifacts for script-first pipeline diagnostics."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def process_outcome(return_code: int | None) -> dict[str, Any]:
    """Preserve subprocess signals instead of flattening them to a generic failure."""
    if return_code is None:
        return {"raw_return_code": None, "shell_exit_code": None, "signal": None}
    if return_code < 0:
        signal_number = -return_code
        try:
            signal_name = __import__("signal").Signals(signal_number).name
        except ValueError:
            signal_name = f"SIG{signal_number}"
        return {
            "raw_return_code": return_code,
            "shell_exit_code": 128 + signal_number,
            "signal": {"number": signal_number, "name": signal_name},
        }
    return {"raw_return_code": return_code, "shell_exit_code": return_code, "signal": None}


def read_status(path: Path | None) -> dict[str, Any] | None:
    """Read a prior atomic checkpoint; malformed artifacts are not fatal."""
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_status(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        try:
            directory = os.open(path.parent, os.O_DIRECTORY)
        except (AttributeError, OSError):
            pass
        else:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def stage_started(stage: str, *, command: list[str] | None = None) -> dict[str, Any]:
    return {
        "stage": stage,
        "state": "running",
        "started_at": utc_now(),
        "command": command or [],
        "selected": 0,
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "cache": {},
        "error_classes": {},
    }


def stage_checkpoint(
    status: dict[str, Any],
    *,
    cache: dict[str, int] | None = None,
    **counts: Any,
) -> dict[str, Any]:
    """Record durable in-progress counters without marking the stage finished."""
    payload = {**status, **counts, "state": "running", "updated_at": utc_now()}
    if cache is not None:
        payload["cache"] = cache
    return payload


def stage_finished(
    status: dict[str, Any],
    *,
    state: str,
    cache: dict[str, int] | None = None,
    error: BaseException | str | None = None,
    **counts: Any,
) -> dict[str, Any]:
    payload = {**status, **counts, "state": state, "finished_at": utc_now()}
    if cache is not None:
        payload["cache"] = cache
    if error is not None:
        payload["error"] = {
            "class": type(error).__name__ if isinstance(error, BaseException) else "Message",
            "message": str(error),
        }
    return payload
