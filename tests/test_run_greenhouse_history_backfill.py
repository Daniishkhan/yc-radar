import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_greenhouse_history_backfill.py"
)
SPEC = importlib.util.spec_from_file_location("run_greenhouse_history_backfill", SCRIPT_PATH)
assert SPEC and SPEC.loader
history_backfill = importlib.util.module_from_spec(SPEC)
sys.modules["run_greenhouse_history_backfill"] = history_backfill
SPEC.loader.exec_module(history_backfill)


def args_for(tmp_path: Path) -> SimpleNamespace:
    inputs = [
        tmp_path / "greenhouse_board_candidates_CC-MAIN-2026-30.csv",
        tmp_path / "greenhouse_board_candidates_CC-MAIN-2026-25.csv",
    ]
    for index, path in enumerate(inputs):
        path.write_text(f"candidate-{index}\n", encoding="utf-8")
    return SimpleNamespace(
        candidate_inputs=inputs,
        run_dir=tmp_path / "run",
        union_output=tmp_path / "outputs" / "union.csv",
        evidence_output=tmp_path / "outputs" / "evidence.csv",
    )


def option_path(command: list[str], option: str) -> Path:
    return Path(command[command.index(option) + 1])


def publish_success_outputs(command: list[str]) -> None:
    script = Path(command[1]).name
    if script == "union_commoncrawl_greenhouse.py":
        paths = [
            option_path(command, "--output"),
            option_path(command, "--evidence-output"),
            option_path(command, "--manifest"),
        ]
    elif script in {"scout_greenhouse_sources.py", "resolve_greenhouse_domains.py"}:
        paths = [option_path(command, "--output"), option_path(command, "--status-file")]
    elif script == "sync_job_sources.py":
        paths = [
            option_path(command, "--checkpoint-file"),
            option_path(command, "--status-file"),
        ]
    else:  # pragma: no cover - protects the test helper from silently accepting a new stage
        raise AssertionError(f"unexpected child script: {script}")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{script}:{path.name}\n", encoding="utf-8")


def successful_runner(calls: list[list[str]]):
    def fake_run(command, *, cwd, check):
        assert cwd == history_backfill.REPOSITORY_ROOT
        assert check is False
        command = list(command)
        calls.append(command)
        publish_success_outputs(command)
        return subprocess.CompletedProcess(command, 0)

    return fake_run


def test_runs_all_children_in_order_with_explicit_safe_options(
    monkeypatch, tmp_path: Path
) -> None:
    args = args_for(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(history_backfill.subprocess, "run", successful_runner(calls))

    assert history_backfill.run(args) == 0

    assert [Path(command[1]).name for command in calls] == [
        "union_commoncrawl_greenhouse.py",
        "scout_greenhouse_sources.py",
        "resolve_greenhouse_domains.py",
        "sync_job_sources.py",
    ]
    assert all(command[0] == sys.executable for command in calls)

    union, scout, resolver, sync = calls
    assert option_path(union, "--manifest").parent == args.run_dir.resolve()
    assert option_path(union, "--output") == args.union_output.resolve()
    assert option_path(union, "--evidence-output") == args.evidence_output.resolve()

    assert option_path(scout, "--cache-dir") == history_backfill.PERSISTENT_SCOUT_CACHE
    assert scout[-1] == "--apply"
    assert scout[scout.index("--delay-seconds") + 1] == "1"
    assert scout[scout.index("--checkpoint-every") + 1] == "25"

    assert option_path(resolver, "--cache-file") == history_backfill.PERSISTENT_RESOLVER_CACHE
    assert resolver[-1] == "--apply"
    assert resolver[resolver.index("--max-attempts") + 1] == "3"
    assert resolver[resolver.index("--company-timeout-seconds") + 1] == "120"
    assert resolver[resolver.index("--checkpoint-every") + 1] == "10"

    assert sync[2] == "sync"
    assert sync[sync.index("--provider") + 1] == "greenhouse"
    assert sync[sync.index("--delay-seconds") + 1] == "1"
    assert sync[sync.index("--max-attempts") + 1] == "4"
    assert option_path(sync, "--checkpoint-file").parent == args.run_dir.resolve()
    assert option_path(sync, "--status-file").parent == args.run_dir.resolve()

    status = json.loads(
        (args.run_dir / history_backfill.TOP_LEVEL_STATUS_NAME).read_text(encoding="utf-8")
    )
    assert status["state"] == "completed"
    assert status["return_code"] == 0
    assert set(status["stages"]) == {"union", "scout", "resolver", "sync"}
    for stage in status["stages"].values():
        assert stage["state"] == "completed"
        assert stage["return_code"] == 0
        assert stage["started_at"]
        assert stage["finished_at"]
        assert stage["command"][0] == sys.executable
        assert all(item["sha256"] for item in stage["inputs"])
        assert all(item["sha256"] for item in stage["outputs"])


def test_restart_skips_completed_stages_only_while_fingerprints_match(
    monkeypatch, tmp_path: Path
) -> None:
    args = args_for(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(history_backfill.subprocess, "run", successful_runner(calls))
    assert history_backfill.run(args) == 0

    calls.clear()
    assert history_backfill.run(args) == 0
    assert calls == []

    scout_output = args.run_dir / history_backfill.SCOUT_OUTPUT_NAME
    scout_output.write_text("tampered\n", encoding="utf-8")
    assert history_backfill.run(args) == 0
    assert [Path(command[1]).name for command in calls] == [
        "scout_greenhouse_sources.py"
    ]

    status = json.loads(
        (args.run_dir / history_backfill.TOP_LEVEL_STATUS_NAME).read_text(encoding="utf-8")
    )
    assert status["attempt_count"] == 3
    assert status["state"] == "completed"


def test_changed_candidate_fingerprint_reruns_union_but_keeps_valid_downstream_stages(
    monkeypatch, tmp_path: Path
) -> None:
    args = args_for(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(history_backfill.subprocess, "run", successful_runner(calls))
    assert history_backfill.run(args) == 0

    calls.clear()
    args.candidate_inputs[0].write_text("candidate-changed\n", encoding="utf-8")
    assert history_backfill.run(args) == 0

    assert [Path(command[1]).name for command in calls] == [
        "union_commoncrawl_greenhouse.py"
    ]


def test_failure_stops_before_later_stages_and_is_recorded(
    monkeypatch, tmp_path: Path
) -> None:
    args = args_for(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, *, cwd, check):
        assert cwd == history_backfill.REPOSITORY_ROOT
        assert check is False
        command = list(command)
        calls.append(command)
        if Path(command[1]).name == "union_commoncrawl_greenhouse.py":
            publish_success_outputs(command)
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, 9)

    monkeypatch.setattr(history_backfill.subprocess, "run", fake_run)

    assert history_backfill.run(args) == 9
    assert [Path(command[1]).name for command in calls] == [
        "union_commoncrawl_greenhouse.py",
        "scout_greenhouse_sources.py",
    ]
    status = json.loads(
        (args.run_dir / history_backfill.TOP_LEVEL_STATUS_NAME).read_text(encoding="utf-8")
    )
    assert status["state"] == "failed"
    assert status["failed_stage"] == "scout"
    assert status["return_code"] == 9
    assert status["stages"]["scout"]["state"] == "failed"
    assert status["stages"]["scout"]["return_code"] == 9
    assert "resolver" not in status["stages"]
    assert "sync" not in status["stages"]


def test_resolver_quota_checkpoint_stops_without_triggering_systemd_retry(
    monkeypatch, tmp_path: Path
) -> None:
    args = args_for(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, *, cwd, check):
        assert cwd == history_backfill.REPOSITORY_ROOT
        assert check is False
        command = list(command)
        calls.append(command)
        script = Path(command[1]).name
        if script != "resolve_greenhouse_domains.py":
            publish_success_outputs(command)
            return subprocess.CompletedProcess(command, 0)

        output = option_path(command, "--output")
        partial = output.with_suffix(".partial.csv")
        status_path = option_path(command, "--status-file")
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text("durable resolver checkpoint\n", encoding="utf-8")
        status_path.write_text(
            json.dumps({"state": "quota_exhausted", "processed": 10}) + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(history_backfill.subprocess, "run", fake_run)

    assert history_backfill.run(args) == 0
    assert [Path(command[1]).name for command in calls] == [
        "union_commoncrawl_greenhouse.py",
        "scout_greenhouse_sources.py",
        "resolve_greenhouse_domains.py",
    ]
    status = json.loads(
        (args.run_dir / history_backfill.TOP_LEVEL_STATUS_NAME).read_text(encoding="utf-8")
    )
    assert status["state"] == "quota_exhausted"
    assert status["paused_stage"] == "resolver"
    assert status["return_code"] == 0
    assert status["stages"]["resolver"]["state"] == "quota_exhausted"
    assert status["stages"]["resolver"]["return_code"] == 0
    assert status["stages"]["resolver"]["resume_outputs"][0]["sha256"]
    assert "sync" not in status["stages"]


def test_cli_requires_at_least_two_candidate_inputs(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        history_backfill.parse_args(
            [
                str(tmp_path / "only-one.csv"),
                "--run-dir",
                str(tmp_path / "run"),
                "--union-output",
                str(tmp_path / "union.csv"),
                "--evidence-output",
                str(tmp_path / "evidence.csv"),
            ]
        )
