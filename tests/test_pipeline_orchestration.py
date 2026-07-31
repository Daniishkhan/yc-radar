import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from yc_radar.services.run_status import (
    process_outcome,
    stage_checkpoint,
    stage_finished,
    stage_started,
    write_status,
)

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_pipeline.py"
SPEC = importlib.util.spec_from_file_location("run_pipeline", SCRIPT_PATH)
assert SPEC and SPEC.loader
run_pipeline = importlib.util.module_from_spec(SPEC)
sys.modules["run_pipeline"] = run_pipeline
SPEC.loader.exec_module(run_pipeline)


def test_process_outcome_preserves_signal_exit_codes() -> None:
    assert process_outcome(-9) == {
        "raw_return_code": -9,
        "shell_exit_code": 137,
        "signal": {"number": 9, "name": "SIGKILL"},
    }
    assert process_outcome(-15)["shell_exit_code"] == 143
    assert process_outcome(-15)["signal"]["name"] == "SIGTERM"


def test_ats_branch_runs_when_classification_is_killed(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    commands: dict[str, list[str]] = {}

    async def fake_run_child(stage: str, command: list[str], status_dir: Path):
        del status_dir
        calls.append(stage)
        commands[stage] = command
        return {"raw_return_code": -9 if stage == "classification" else 0, "shell_exit_code": 137 if stage == "classification" else 0}

    monkeypatch.setattr(run_pipeline, "run_child", fake_run_child)
    args = SimpleNamespace(
        status_dir=tmp_path,
        discovery_limit=1,
        classification_limit=1,
        sync_limit=1,
        run_key=None,
    )

    result = asyncio.run(run_pipeline.run_pipeline(args))

    assert result == 137
    assert calls[0] == "discovery"
    assert {"ats-registration", "ats-sync", "classification"}.issubset(calls)
    assert "discover" in commands["ats-registration"]
    assert "discover-greenhouse" not in commands["ats-registration"]
    assert "--provider" not in commands["ats-sync"]


def test_pipeline_aggregates_zero_exit_partial_stage(monkeypatch, tmp_path: Path) -> None:
    async def fake_run_child(stage: str, command: list[str], status_dir: Path):
        del command, status_dir
        return {
            "state": "partial" if stage == "classification" else "completed",
            "raw_return_code": 0,
            "shell_exit_code": 0,
        }

    monkeypatch.setattr(run_pipeline, "run_child", fake_run_child)
    args = SimpleNamespace(
        status_dir=tmp_path,
        discovery_limit=1,
        classification_limit=1,
        sync_limit=1,
        run_key=None,
    )

    assert asyncio.run(run_pipeline.run_pipeline(args)) == 0
    pipeline = __import__("json").loads(
        (tmp_path / "pipeline.json").read_text(encoding="utf-8")
    )
    assert pipeline["state"] == "partial"
    assert pipeline["stages"]["classification"]["state"] == "partial"


def test_pipeline_prioritizes_signal_over_simultaneous_ats_failure(monkeypatch, tmp_path: Path) -> None:
    async def fake_run_child(stage: str, command: list[str], status_dir: Path):
        del command, status_dir
        if stage == "classification":
            return {"raw_return_code": -15, "shell_exit_code": 143}
        if stage == "ats-sync":
            return {"raw_return_code": 1, "shell_exit_code": 1}
        return {"raw_return_code": 0, "shell_exit_code": 0}

    monkeypatch.setattr(run_pipeline, "run_child", fake_run_child)
    args = SimpleNamespace(
        status_dir=tmp_path,
        discovery_limit=1,
        classification_limit=1,
        sync_limit=1,
        run_key=None,
    )

    assert asyncio.run(run_pipeline.run_pipeline(args)) == 143
    pipeline = __import__("json").loads((tmp_path / "pipeline.json").read_text(encoding="utf-8"))
    assert pipeline["raw_return_code"] == -15
    assert pipeline["signal"] == {"number": 15, "name": "SIGTERM"}
    assert pipeline["stages"]["ats_sync"]["raw_return_code"] == 1


def test_run_child_preserves_partial_state_from_successful_child(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeProcess:
        async def wait(self) -> int:
            write_status(
                tmp_path / "classification.json",
                stage_finished(
                    stage_started("classification"),
                    state="partial",
                    selected=3,
                    processed=3,
                    succeeded=2,
                    failed=1,
                ),
            )
            return 0

    async def fake_create_subprocess_exec(*args):
        del args
        return FakeProcess()

    monkeypatch.setattr(
        run_pipeline.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    outcome = asyncio.run(
        run_pipeline.run_child("classification", ["fake-child"], tmp_path)
    )

    assert outcome["state"] == "partial"
    assert outcome["raw_return_code"] == 0
    assert outcome["failed"] == 1


def test_run_child_merges_last_checkpoint_into_signal_process_artifact(monkeypatch, tmp_path: Path) -> None:
    class FakeProcess:
        async def wait(self) -> int:
            write_status(
                tmp_path / "classification.json",
                stage_checkpoint(
                    stage_started("classification"),
                    selected=8,
                    processed=4,
                    succeeded=3,
                    failed=1,
                    cache={"hits": 2},
                ),
            )
            return -9

    async def fake_create_subprocess_exec(*args):
        del args
        return FakeProcess()

    monkeypatch.setattr(run_pipeline.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    outcome = asyncio.run(run_pipeline.run_child("classification", ["fake-child"], tmp_path))

    assert outcome["shell_exit_code"] == 137
    assert outcome["processed"] == 4
    assert outcome["cache"] == {"hits": 2}
    persisted = __import__("json").loads((tmp_path / "classification.process.json").read_text(encoding="utf-8"))
    assert persisted["signal"]["name"] == "SIGKILL"
    assert persisted["selected"] == 8
