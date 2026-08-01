from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_ashby_backfill.py"
SPEC = importlib.util.spec_from_file_location("run_ashby_backfill", SCRIPT_PATH)
assert SPEC and SPEC.loader
ashby_backfill = importlib.util.module_from_spec(SPEC)
sys.modules["run_ashby_backfill"] = ashby_backfill
SPEC.loader.exec_module(ashby_backfill)


def args_for(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        run_dir=tmp_path / "ashby-run",
        delay_seconds=1.0,
        registration_max_attempts=3,
        sync_max_attempts=4,
        run_key=None,
    )


def option_path(command: list[str], option: str) -> Path:
    return Path(command[command.index(option) + 1])


def publish_child_status(command: list[str], *, state: str = "completed") -> None:
    status_path = option_path(command, "--status-file")
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "state": state,
                "started_at": "2026-08-01T00:00:00+00:00",
                "finished_at": "2026-08-01T00:01:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def test_runs_provider_filtered_registration_then_checkpointed_sync(
    monkeypatch, tmp_path: Path
) -> None:
    args = args_for(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, *, cwd, check):
        assert cwd == ashby_backfill.SCRIPTS_DIR.parent
        assert check is False
        copied = list(command)
        calls.append(copied)
        publish_child_status(copied)
        return subprocess.CompletedProcess(copied, 0)

    monkeypatch.setattr(ashby_backfill.subprocess, "run", fake_run)

    assert ashby_backfill.run(args) == 0
    assert [Path(command[1]).name for command in calls] == [
        "discover_job_sources_checkpointed.py",
        "sync_job_sources.py",
    ]
    registration, sync = calls
    assert registration[registration.index("--provider") + 1] == "ashby"
    assert option_path(registration, "--checkpoint-file").name == (
        ashby_backfill.REGISTRATION_CHECKPOINT_NAME
    )
    assert sync[2] == "sync"
    assert sync[sync.index("--provider") + 1] == "ashby"
    assert sync[sync.index("--delay-seconds") + 1] == "1.0"
    assert sync[sync.index("--max-attempts") + 1] == "4"
    assert sync[sync.index("--run-key") + 1] == "ashby-backfill:ashby-run"
    assert option_path(sync, "--checkpoint-file").name == ashby_backfill.SYNC_CHECKPOINT_NAME

    status = json.loads(
        (args.run_dir / ashby_backfill.TOP_LEVEL_STATUS_NAME).read_text(encoding="utf-8")
    )
    assert status["state"] == "completed"
    assert status["raw_return_code"] == 0
    assert list(status["stages"]) == ["registration", "sync"]


def test_partial_safe_registration_still_syncs_unambiguous_sources(
    monkeypatch, tmp_path: Path
) -> None:
    args = args_for(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, *, cwd, check):
        del cwd, check
        copied = list(command)
        calls.append(copied)
        state = (
            "partial"
            if Path(copied[1]).name == "discover_job_sources_checkpointed.py"
            else "completed"
        )
        publish_child_status(copied, state=state)
        return subprocess.CompletedProcess(copied, 0)

    monkeypatch.setattr(ashby_backfill.subprocess, "run", fake_run)

    assert ashby_backfill.run(args) == 0
    assert len(calls) == 2
    status = json.loads(
        (args.run_dir / ashby_backfill.TOP_LEVEL_STATUS_NAME).read_text(encoding="utf-8")
    )
    assert status["state"] == "partial"
    assert status["stages"]["registration"]["state"] == "partial"
    assert status["stages"]["sync"]["state"] == "completed"


def test_registration_failure_stops_before_sync(monkeypatch, tmp_path: Path) -> None:
    args = args_for(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, *, cwd, check):
        del cwd, check
        copied = list(command)
        calls.append(copied)
        publish_child_status(copied, state="failed")
        return subprocess.CompletedProcess(copied, 9)

    monkeypatch.setattr(ashby_backfill.subprocess, "run", fake_run)

    assert ashby_backfill.run(args) == 9
    assert len(calls) == 1
    status = json.loads(
        (args.run_dir / ashby_backfill.TOP_LEVEL_STATUS_NAME).read_text(encoding="utf-8")
    )
    assert status["state"] == "failed"
    assert status["failed_stage"] == "registration"
    assert "sync" not in status["stages"]


def test_success_without_terminal_child_status_fails_closed(
    monkeypatch, tmp_path: Path
) -> None:
    args = args_for(tmp_path)

    def fake_run(command, *, cwd, check):
        del cwd, check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(ashby_backfill.subprocess, "run", fake_run)

    assert ashby_backfill.run(args) == 1
    status = json.loads(
        (args.run_dir / ashby_backfill.TOP_LEVEL_STATUS_NAME).read_text(encoding="utf-8")
    )
    assert status["state"] == "failed"
    assert status["stages"]["registration"]["error"]["class"] == "InvalidChildStatus"
