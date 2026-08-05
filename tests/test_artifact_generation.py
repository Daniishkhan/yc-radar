from __future__ import annotations

import csv
from pathlib import Path

import pytest

from yc_radar.services import artifact_generation
from yc_radar.services.artifact_generation import (
    ARTIFACT_GENERATION_LOCK_NAME,
    ArtifactGenerationLocked,
    artifact_generation_lock,
    artifact_generation_lock_path,
    atomic_text_writer,
    atomic_write_csv,
    atomic_write_json,
)


def test_lock_path_is_shared_at_resolved_local_root(tmp_path: Path) -> None:
    local_dir = tmp_path / "data" / "local"
    current_run = local_dir / "runs" / "current"
    dated_run = local_dir / "runs" / "2026-08-05"

    assert artifact_generation_lock_path(
        output_dir=current_run,
        local_dir=local_dir,
    ) == (local_dir / ARTIFACT_GENERATION_LOCK_NAME).resolve()
    assert artifact_generation_lock_path(
        output_dir=dated_run,
        local_dir=local_dir,
    ) == (local_dir / ARTIFACT_GENERATION_LOCK_NAME).resolve()


def test_custom_output_uses_an_isolated_resolved_lock(tmp_path: Path) -> None:
    custom_output = tmp_path / "test-output" / ".." / "test-output"

    assert artifact_generation_lock_path(
        output_dir=custom_output,
        local_dir=tmp_path / "configured" / "local",
    ) == custom_output.resolve() / ARTIFACT_GENERATION_LOCK_NAME


def test_lock_contention_fails_immediately_without_a_second_process(tmp_path: Path) -> None:
    output_dir = tmp_path / "data" / "local" / "runs" / "current"
    local_dir = tmp_path / "data" / "local"

    with artifact_generation_lock(output_dir=output_dir, local_dir=local_dir):
        with pytest.raises(ArtifactGenerationLocked, match="already running"):
            with artifact_generation_lock(output_dir=output_dir, local_dir=local_dir):
                raise AssertionError("contended lock unexpectedly acquired")

    with artifact_generation_lock(output_dir=output_dir, local_dir=local_dir):
        pass


def test_atomic_text_writer_publishes_only_after_success(tmp_path: Path) -> None:
    destination = tmp_path / "queue.csv"
    destination.write_text("old\n", encoding="utf-8")

    with atomic_text_writer(destination, newline="") as handle:
        handle.write("new\n")
        assert destination.read_text(encoding="utf-8") == "old\n"
        assert len(list(tmp_path.glob(f".{destination.name}.*.tmp"))) == 1

    assert destination.read_text(encoding="utf-8") == "new\n"
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_atomic_json_replace_failure_preserves_destination_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "queue.json"
    destination.write_text('{"state":"old"}', encoding="utf-8")

    def fail_replace(source: str, target: Path) -> None:
        raise OSError(f"cannot replace {source} with {target}")

    monkeypatch.setattr(artifact_generation.os, "replace", fail_replace)

    with pytest.raises(OSError, match="cannot replace"):
        atomic_write_json(destination, {"state": "new"}, indent=2)

    assert destination.read_text(encoding="utf-8") == '{"state":"old"}'
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_atomic_csv_serialization_failure_preserves_destination(tmp_path: Path) -> None:
    destination = tmp_path / "queue.csv"
    destination.write_text("state\nold\n", encoding="utf-8")

    class InvalidCsvValue:
        def __str__(self) -> str:
            raise ValueError("invalid CSV value")

    with pytest.raises(ValueError, match="invalid CSV value"):
        atomic_write_csv(
            destination,
            fieldnames=["state"],
            rows=[{"state": InvalidCsvValue()}],
        )

    assert destination.read_text(encoding="utf-8") == "state\nold\n"
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_atomic_csv_success_preserves_csv_dialect(tmp_path: Path) -> None:
    destination = tmp_path / "queue.csv"

    atomic_write_csv(
        destination,
        fieldnames=["name", "reason"],
        rows=[{"name": "Example", "reason": "one, two"}],
    )

    with destination.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == [{"name": "Example", "reason": "one, two"}]
