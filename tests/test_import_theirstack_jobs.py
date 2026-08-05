from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yc_radar.services.run_status import read_status, write_status
from yc_radar.services.theirstack_client import (
    CreditBalance,
    SearchResult,
    TheirStackRequestCache,
)


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "import_theirstack_jobs.py"
SPEC = importlib.util.spec_from_file_location("import_theirstack_jobs", SCRIPT_PATH)
assert SPEC and SPEC.loader
import_theirstack_jobs_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(import_theirstack_jobs_script)


def _manifest(
    *,
    selected: tuple[int, ...] = (101,),
    reserve: tuple[int, ...] = (),
    batches: list[dict[str, Any]] | None = None,
    state: str = "previewed",
) -> dict[str, Any]:
    if batches is None:
        batches = [
            {
                "index": index,
                "job_ids": list(job_ids),
                "body": import_theirstack_jobs_script.paid_search_body(job_ids),
                "state": "pending",
            }
            for index, job_ids in enumerate(
                import_theirstack_jobs_script.chunks(selected, 25),
                start=1,
            )
        ]
    payload: dict[str, Any] = {
        "schema_version": import_theirstack_jobs_script.MANIFEST_SCHEMA_VERSION,
        "importer_version": import_theirstack_jobs_script.IMPORTER_VERSION,
        "state": state,
        "created_at": "2026-08-03T00:00:00+00:00",
        "updated_at": "2026-08-03T00:00:00+00:00",
        "observation_time": "2026-08-03T00:00:00+00:00",
        "credit_budget": len(selected),
        "excluded_job_ids": [],
        "selected_job_ids": list(selected),
        "reserve_job_ids": list(reserve),
        "strata": [],
        "batches": batches,
        "top_up_batches": [],
    }
    payload["plan_id"] = import_theirstack_jobs_script.plan_digest(payload)
    return payload


def test_preview_replays_valid_manifest_without_vendor_or_database_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest = _manifest()
    write_status(manifest_path, manifest)

    def unexpected_access(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("manifest replay must remain local")

    monkeypatch.setattr(
        import_theirstack_jobs_script,
        "existing_theirstack_job_ids",
        unexpected_access,
    )
    args = SimpleNamespace(manifest=manifest_path, refresh=False)

    summary = import_theirstack_jobs_script.preview_command(args, client=unexpected_access)

    assert summary["plan_id"] == manifest["plan_id"]
    assert summary["replayed"] is True


def test_preview_replay_validates_frozen_plan_before_returning(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest = _manifest(selected=(101, 102))
    manifest["selected_job_ids"] = [101, 999]
    write_status(manifest_path, manifest)
    args = SimpleNamespace(manifest=manifest_path, refresh=False)

    with pytest.raises(SystemExit, match="plan scope has changed"):
        import_theirstack_jobs_script.preview_command(args)


def test_refresh_paces_network_requests_even_when_preview_cache_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = TheirStackRequestCache(tmp_path / "cache")

    class RefreshClient:
        def __init__(self) -> None:
            self.cache = cache
            self.search_calls = 0

        def credit_balance(self) -> CreditBalance:
            return CreditBalance(api_credits=1, used_api_credits=0)

        def search(self, body: dict[str, Any], **kwargs: Any) -> SearchResult:
            assert kwargs["force_refresh"] is True
            self.search_calls += 1
            return SearchResult(
                payload={"data": [], "metadata": {}},
                request_hash=self.cache.request_hash(
                    "POST", import_theirstack_jobs_script.SEARCH_URL, body
                ),
                cache_source="network",
            )

    for stratum in import_theirstack_jobs_script.DEFAULT_SEARCH_STRATA:
        body = import_theirstack_jobs_script.preview_search_body(stratum, page=0)
        cache.store(
            "POST",
            import_theirstack_jobs_script.SEARCH_URL,
            body,
            {"data": [], "metadata": {}},
        )
    monkeypatch.setattr(
        import_theirstack_jobs_script,
        "existing_theirstack_job_ids",
        lambda: set(),
    )
    sleeps: list[float] = []
    client = RefreshClient()
    args = SimpleNamespace(
        manifest=tmp_path / "manifest.json",
        refresh=True,
        cache_dir=tmp_path / "cache",
        credit_budget=1,
        pages_per_stratum=1,
        reserve_size=0,
        exclude_job_id=[],
        exclude_job_ids_file=None,
        request_delay_seconds=0.25,
        preview_cache_max_age_hours=6.0,
    )

    import_theirstack_jobs_script.preview_command(
        args,
        client=client,
        sleeper=sleeps.append,
    )

    assert client.search_calls == len(import_theirstack_jobs_script.DEFAULT_SEARCH_STRATA)
    assert sleeps == [0.25] * (client.search_calls - 1)


@pytest.mark.parametrize("corruption", ["job_ids", "body"])
def test_validate_manifest_binds_paid_batches_to_canonical_selected_chunks(
    corruption: str,
) -> None:
    manifest = _manifest(selected=(101, 102))
    batch = manifest["batches"][0]
    if corruption == "job_ids":
        batch["job_ids"] = [101, 999]
        batch["body"] = import_theirstack_jobs_script.paid_search_body([101, 999])
    else:
        batch["body"] = import_theirstack_jobs_script.paid_search_body([102, 101])

    with pytest.raises(SystemExit):
        import_theirstack_jobs_script.validate_manifest(manifest)


def test_fetch_requires_explicit_paid_consent_before_loading_manifest() -> None:
    args = SimpleNamespace(yes_spend_credits=False)

    with pytest.raises(SystemExit, match="--yes-spend-credits"):
        import_theirstack_jobs_script.fetch_command(args)


def test_fetch_preflights_cumulative_pending_ids_against_current_balance(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    selected = tuple(range(101, 127))
    batches = [
        {
            "index": index,
            "job_ids": list(job_ids),
            "body": import_theirstack_jobs_script.paid_search_body(job_ids),
            "state": "pending",
        }
        for index, job_ids in enumerate(
            import_theirstack_jobs_script.chunks(selected, 25),
            start=1,
        )
    ]
    write_status(
        manifest_path,
        _manifest(selected=selected, batches=batches),
    )

    class CreditBoundClient:
        def __init__(self) -> None:
            self.cache = TheirStackRequestCache(tmp_path / "cache")
            self.search_calls: list[dict[str, Any]] = []

        def credit_balance(self) -> CreditBalance:
            return CreditBalance(api_credits=25, used_api_credits=175)

        def search(
            self,
            body: dict[str, Any],
            *,
            allow_paid: bool = False,
        ) -> SearchResult:
            assert allow_paid is True
            self.search_calls.append(dict(body))
            payload = {
                "data": [
                    {"id": job_id, "has_blurred_data": False}
                    for job_id in body["job_id_or"]
                ],
                "metadata": {},
            }
            return SearchResult(
                payload=payload,
                request_hash=self.cache.request_hash(
                    "POST",
                    import_theirstack_jobs_script.SEARCH_URL,
                    body,
                ),
                cache_source="network",
            )

    client = CreditBoundClient()
    args = SimpleNamespace(
        yes_spend_credits=True,
        manifest=manifest_path,
        cache_dir=tmp_path / "cache",
        max_credits=26,
        request_delay_seconds=0,
        retry_uncertain_spend=False,
    )

    with pytest.raises(SystemExit, match="(?:balance|credits)"):
        import_theirstack_jobs_script.fetch_command(args, client=client)

    assert client.search_calls == []


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"id": 999, "has_blurred_data": False}, "unplanned job ID 999"),
        ({"id": 101, "has_blurred_data": True}, "job 101 is still blurred"),
    ],
)
def test_cached_jobs_rejects_unplanned_or_blurred_records(
    tmp_path: Path,
    row: dict[str, Any],
    message: str,
) -> None:
    body = import_theirstack_jobs_script.paid_search_body([101])
    batch = {
        "index": 1,
        "job_ids": [101],
        "body": body,
        "state": "fetched",
    }
    manifest = _manifest(batches=[batch], state="fetched")
    cache = TheirStackRequestCache(tmp_path / "cache")
    cache.store(
        "POST",
        import_theirstack_jobs_script.SEARCH_URL,
        body,
        {"data": [row], "metadata": {}},
    )

    with pytest.raises(SystemExit, match=message):
        import_theirstack_jobs_script.cached_jobs(manifest, cache)


@pytest.mark.parametrize("top_up_state", ["pending", "requesting"])
def test_apply_rejects_incomplete_top_up_batches_before_database_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    top_up_state: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    cache_dir = tmp_path / "cache"
    primary_body = import_theirstack_jobs_script.paid_search_body([101])
    primary_batch = {
        "index": 1,
        "job_ids": [101],
        "body": primary_body,
        "state": "fetched",
        "returned_job_ids": [101],
    }
    manifest = _manifest(
        selected=(101,),
        reserve=(201,),
        batches=[primary_batch],
        state="fetched",
    )
    manifest["top_up_batches"] = [
        {
            "index": "top-up-1",
            "job_ids": [201],
            "body": import_theirstack_jobs_script.paid_search_body([201]),
            "state": top_up_state,
        }
    ]
    write_status(manifest_path, manifest)
    TheirStackRequestCache(cache_dir).store(
        "POST",
        import_theirstack_jobs_script.SEARCH_URL,
        primary_body,
        {
            "data": [
                {
                    "id": 101,
                    "job_title": "Senior Backend Engineer",
                    "has_blurred_data": False,
                }
            ],
            "metadata": {},
        },
    )

    def unexpected_database_access() -> None:
        raise AssertionError("incomplete top-up must be rejected before database access")

    monkeypatch.setattr(
        import_theirstack_jobs_script,
        "engine_from_url",
        unexpected_database_access,
    )
    args = SimpleNamespace(
        manifest=manifest_path,
        cache_dir=cache_dir,
        no_stage_urls=False,
    )

    with pytest.raises(SystemExit, match="fully fetched"):
        import_theirstack_jobs_script.apply_command(args)


def test_apply_uses_only_cached_jobs_and_never_constructs_vendor_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    cache_dir = tmp_path / "cache"
    body = import_theirstack_jobs_script.paid_search_body([101])
    batch = {
        "index": 1,
        "job_ids": [101],
        "body": body,
        "state": "fetched",
        "returned_job_ids": [101],
    }
    manifest = _manifest(batches=[batch], state="fetched")
    write_status(manifest_path, manifest)
    TheirStackRequestCache(cache_dir).store(
        "POST",
        import_theirstack_jobs_script.SEARCH_URL,
        body,
        {
            "data": [
                {
                    "id": 101,
                    "job_title": "Senior Backend Engineer",
                    "has_blurred_data": False,
                }
            ],
            "metadata": {},
        },
    )

    class DisposableEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = DisposableEngine()
    captured: dict[str, Any] = {}

    def unexpected_vendor_client(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("apply must not construct a TheirStack client")

    def fake_import(
        received_engine: object,
        jobs: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        captured.update(engine=received_engine, jobs=jobs, kwargs=kwargs)
        return "applied"

    monkeypatch.setattr(
        import_theirstack_jobs_script,
        "_client_from_settings",
        unexpected_vendor_client,
    )
    monkeypatch.setattr(
        import_theirstack_jobs_script,
        "engine_from_url",
        lambda: engine,
    )
    monkeypatch.setattr(
        import_theirstack_jobs_script,
        "import_theirstack_jobs",
        fake_import,
    )
    monkeypatch.setattr(
        import_theirstack_jobs_script,
        "import_result_dict",
        lambda result: {"result": result},
    )
    args = SimpleNamespace(
        manifest=manifest_path,
        cache_dir=cache_dir,
        no_stage_urls=True,
    )

    summary = import_theirstack_jobs_script.apply_command(args)

    assert summary["manifest_state"] == "applied"
    assert captured["engine"] is engine
    assert [job["id"] for job in captured["jobs"]] == [101]
    assert captured["kwargs"]["stage_urls"] is False
    assert captured["kwargs"]["plan_id"] == manifest["plan_id"]
    assert engine.disposed is True
    persisted = read_status(manifest_path)
    assert persisted is not None
    assert persisted["state"] == "applied"
