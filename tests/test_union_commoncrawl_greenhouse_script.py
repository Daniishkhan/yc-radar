from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "union_commoncrawl_greenhouse.py"
)
SPEC = importlib.util.spec_from_file_location("union_commoncrawl_greenhouse", SCRIPT_PATH)
assert SPEC and SPEC.loader
union_script = importlib.util.module_from_spec(SPEC)
sys.modules["union_commoncrawl_greenhouse"] = union_script
SPEC.loader.exec_module(union_script)


INPUT_FIELDS = [
    "board_token",
    "canonical_source_url",
    "example_observed_url",
    "observation_count",
    "first_observed_at",
    "last_observed_at",
]


def candidate(
    token: str,
    *,
    count: str = "1",
    first: str = "2026-07-10 00:00:00.000",
    last: str = "2026-07-11 00:00:00.000",
    canonical_token: str | None = None,
    example_token: str | None = None,
) -> dict[str, str]:
    canonical_token = canonical_token or token
    example_token = example_token or token
    return {
        "board_token": token,
        "canonical_source_url": (
            f"https://job-boards.greenhouse.io/{canonical_token}"
        ),
        "example_observed_url": (
            f"https://boards.greenhouse.io/{example_token}/jobs/1?gh_src=test"
        ),
        "observation_count": count,
        "first_observed_at": first,
        "last_observed_at": last,
    }


def write_candidates(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=INPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_union_preserves_per_crawl_evidence_and_manifest_marginals(
    tmp_path: Path, monkeypatch
) -> None:
    latest = tmp_path / "greenhouse_board_candidates_CC-MAIN-2026-30.csv"
    earlier = tmp_path / "greenhouse_board_candidates_CC-MAIN-2026-21.csv"
    write_candidates(
        latest,
        [
            candidate("AcMe", count="2", canonical_token="ACME", example_token="ACME"),
            candidate("beta", count="4"),
            candidate(
                "ACME",
                count="1",
                canonical_token="acme",
                example_token="acme",
                first="2026-07-09 00:00:00.000",
                last="2026-07-12 00:00:00.000",
            ),
        ],
    )
    write_candidates(
        earlier,
        [
            candidate(
                "acme",
                count="3",
                first="2026-05-08 00:00:00.000",
                last="2026-05-09 00:00:00.000",
            ),
            candidate(
                "gamma",
                count="5",
                first="2026-05-10 00:00:00.000",
                last="2026-05-11 00:00:00.000",
            ),
        ],
    )
    output = tmp_path / "union.csv"
    evidence_output = tmp_path / "evidence.csv"
    manifest_path = tmp_path / "manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            str(latest),
            str(earlier),
            "--output",
            str(output),
            "--evidence-output",
            str(evidence_output),
            "--manifest",
            str(manifest_path),
        ],
    )

    union_script.main()

    union_rows = {row["board_token"]: row for row in read_csv(output)}
    assert set(union_rows) == {"acme", "beta", "gamma"}
    assert union_rows["acme"] == {
        "board_token": "acme",
        "canonical_source_url": "https://job-boards.greenhouse.io/acme",
        "example_observed_url": "https://boards.greenhouse.io/ACME/jobs/1?gh_src=test",
        "observation_count": "6",
        "first_observed_at": "2026-05-08T00:00:00Z",
        "last_observed_at": "2026-07-12T00:00:00Z",
        "first_seen_crawl": "CC-MAIN-2026-21",
        "last_seen_crawl": "CC-MAIN-2026-30",
        "crawl_count": "2",
        "crawl_ids": '["CC-MAIN-2026-21","CC-MAIN-2026-30"]',
    }

    evidence_rows = read_csv(evidence_output)
    assert len(evidence_rows) == 4
    latest_acme = next(
        row
        for row in evidence_rows
        if row["crawl_id"] == "CC-MAIN-2026-30" and row["board_token"] == "acme"
    )
    assert latest_acme["observation_count"] == "3"
    assert latest_acme["first_observed_at"] == "2026-07-09T00:00:00Z"
    assert latest_acme["last_observed_at"] == "2026-07-12T00:00:00Z"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["union_token_count"] == 3
    assert manifest["evidence_row_count"] == 4
    assert manifest["total_observation_count"] == 15
    assert [row["crawl_id"] for row in manifest["inputs"]] == [
        "CC-MAIN-2026-30",
        "CC-MAIN-2026-21",
    ]
    assert [row["input_row_count"] for row in manifest["inputs"]] == [3, 2]
    assert [row["token_count"] for row in manifest["inputs"]] == [2, 2]
    assert [row["marginal_new_tokens"] for row in manifest["inputs"]] == [2, 1]
    assert manifest["outputs"]["candidates"]["sha256"] == sha256(output)
    assert manifest["outputs"]["crawl_evidence"]["sha256"] == sha256(
        evidence_output
    )


def test_union_rejects_duplicate_crawl_inputs(tmp_path: Path) -> None:
    first = tmp_path / "one_CC-MAIN-2026-30.csv"
    second = tmp_path / "two_CC-MAIN-2026-30.csv"
    write_candidates(first, [candidate("acme")])
    write_candidates(second, [candidate("other")])

    with pytest.raises(ValueError, match="supplied more than once"):
        union_script.union_candidate_files([first, second])


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (candidate("bad/token"), "invalid board token"),
        (candidate("acme", count="0"), "must be a positive integer"),
        (
            candidate("acme", canonical_token="other"),
            "canonical source URL does not match",
        ),
        (
            candidate("acme", example_token="other"),
            "example observed URL does not match",
        ),
    ],
)
def test_union_rejects_invalid_candidate_rows(
    tmp_path: Path, row: dict[str, str], message: str
) -> None:
    path = tmp_path / "candidates_CC-MAIN-2026-30.csv"
    write_candidates(path, [row])

    with pytest.raises(ValueError, match=message):
        union_script.union_candidate_files([path])


def test_union_requires_exactly_one_crawl_id_in_each_filename(tmp_path: Path) -> None:
    path = tmp_path / "CC-MAIN-2026-30_to_CC-MAIN-2026-21.csv"
    write_candidates(path, [candidate("acme")])

    with pytest.raises(ValueError, match="exactly one unique CC-MAIN ID"):
        union_script.union_candidate_files([path])


def test_union_rejects_missing_required_headers(tmp_path: Path) -> None:
    path = tmp_path / "candidates_CC-MAIN-2026-30.csv"
    path.write_text("board_token,observation_count\nacme,1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing columns"):
        union_script.union_candidate_files([path])
