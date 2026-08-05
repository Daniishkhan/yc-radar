from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot
from yc_radar.services import company_registry as company_registry_service
from yc_radar.services.company_registry import (
    CompanyIdentityConflict,
    CompanyRegistry,
    CompanySourceDependentCounts,
)
from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    engine_from_url,
    jobs_table,
    sync_runs_table,
)
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_sync_service import JobSyncService


CREATED_AT = datetime(2026, 8, 5, 9, tzinfo=UTC)
REPAIRED_AT = datetime(2026, 8, 5, 10, tzinfo=UTC)


def load_repair_script():
    script_path = Path(__file__).parents[1] / "scripts" / "repair_company_source.py"
    spec = spec_from_file_location("repair_company_source_script", script_path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_company(registry: CompanyRegistry, name: str, slug: str) -> int:
    return registry.register_company(
        name=name,
        website=f"https://{slug}.example",
        requested_slug=slug,
        now=CREATED_AT,
    ).company_id


def add_source(
    engine,
    company_id: int,
    *,
    external_id: str = "acme",
    sync_mode: str = "complete_snapshot",
) -> dict:
    source, allowed, created = JobRepository(engine).register_source(
        company_id=company_id,
        provider="greenhouse",
        source_kind="ats_board",
        external_id=external_id,
        source_url=f"https://job-boards.greenhouse.io/{external_id}",
        sync_mode=sync_mode,
        metadata={"preserved": True},
        now=CREATED_AT,
    )
    assert allowed is True
    assert created is True
    return source


def seed_completed_job(engine, source_id: int, *, external_id: str = "acme") -> None:
    JobSyncService(engine, clock=lambda: CREATED_AT).sync_snapshot(
        company_source_id=source_id,
        run_key="seed-complete",
        snapshot=SourceSnapshot(
            provider="greenhouse",
            external_source_id=external_id,
            adapter_version="test",
            is_complete=True,
            http_status=200,
            jobs=[
                NormalizedJob(
                    external_job_id="job-1",
                    title="Backend Engineer",
                    content_hash="job-1-hash",
                )
            ],
        ),
    )


def source_row(engine, source_id: int) -> dict:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(company_sources_table).where(company_sources_table.c.id == source_id)
            )
            .mappings()
            .one()
        )


def test_cli_is_dry_run_by_default_and_actions_are_mutually_exclusive() -> None:
    repair_company_source = load_repair_script()
    args = repair_company_source.parse_args(
        [
            "--provider",
            "greenhouse",
            "--external-id",
            "acme",
            "--expected-company-id",
            "1",
            "--target-company-id",
            "2",
            "--reason",
            "incorrect identity",
        ]
    )

    assert args.yes is False
    with pytest.raises(SystemExit):
        repair_company_source.parse_args(
            [
                "--provider",
                "greenhouse",
                "--external-id",
                "acme",
                "--expected-company-id",
                "1",
                "--target-company-id",
                "2",
                "--disable-source",
                "--reason",
                "incorrect identity",
            ]
        )


def test_dry_run_with_new_provisional_company_does_not_write(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    old_company_id = add_company(registry, "Old Employer", "old-employer")
    source = add_source(engine, old_company_id)

    def unexpected_schema_change(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dry-run must not invoke schema migration")

    monkeypatch.setattr(company_registry_service, "create_schema", unexpected_schema_change)

    result = registry.reassign_source_identity(
        provider="greenhouse",
        external_id="acme",
        expected_company_id=old_company_id,
        new_company_name="Correct Employer",
        new_company_slug="correct-employer",
        reason="provider source belongs to a different employer",
    )

    with engine.connect() as connection:
        company_count = connection.scalar(select(func.count()).select_from(companies_table))
    assert result.applied is False
    assert result.after.company_id is None
    assert result.new_company_slug == "correct-employer"
    assert company_count == 1
    assert source_row(engine, int(source["id"]))["company_id"] == old_company_id
    assert source_row(engine, int(source["id"]))["metadata"] == {"preserved": True}


def test_guarded_reassignment_preserves_source_jobs_and_runs(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    old_company_id = add_company(registry, "Old Employer", "old-employer")
    target_company_id = add_company(registry, "Correct Employer", "correct-employer")
    source = add_source(engine, old_company_id)
    source_id = int(source["id"])
    seed_completed_job(engine, source_id)
    with engine.connect() as connection:
        job_ids_before = list(
            connection.scalars(
                select(jobs_table.c.id).where(jobs_table.c.company_source_id == source_id)
            )
        )
        run_ids_before = list(
            connection.scalars(
                select(sync_runs_table.c.id).where(
                    sync_runs_table.c.company_source_id == source_id
                )
            )
        )

    result = registry.reassign_source_identity(
        provider="greenhouse",
        external_id="acme",
        expected_company_id=old_company_id,
        target_company_id=target_company_id,
        reason="independent identity evidence disproved the original match",
        apply=True,
        now=REPAIRED_AT,
    )

    repaired = source_row(engine, source_id)
    with engine.connect() as connection:
        job_ids_after = list(
            connection.scalars(
                select(jobs_table.c.id).where(jobs_table.c.company_source_id == source_id)
            )
        )
        run_ids_after = list(
            connection.scalars(
                select(sync_runs_table.c.id).where(
                    sync_runs_table.c.company_source_id == source_id
                )
            )
        )
    assert result.applied is True
    assert result.company_source_id == source_id
    assert result.before.dependent_counts == CompanySourceDependentCounts(jobs=1, sync_runs=1)
    assert result.after.dependent_counts == result.before.dependent_counts
    assert repaired["company_id"] == target_company_id
    assert repaired["metadata"]["preserved"] is True
    assert repaired["metadata"]["identity_repair"][-1] == {
        "action": "reassign",
        "reason": "independent identity evidence disproved the original match",
        "repaired_at": REPAIRED_AT.isoformat(),
        "old_company_id": old_company_id,
        "new_company_id": target_company_id,
        "old_status": "active",
        "new_status": "active",
    }
    assert job_ids_after == job_ids_before
    assert run_ids_after == run_ids_before


def test_reassignment_rejects_expected_owner_mismatch(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    old_company_id = add_company(registry, "Old Employer", "old-employer")
    target_company_id = add_company(registry, "Correct Employer", "correct-employer")
    source = add_source(engine, old_company_id)

    with pytest.raises(CompanyIdentityConflict, match="owner mismatch"):
        registry.reassign_source_identity(
            provider="greenhouse",
            external_id="acme",
            expected_company_id=target_company_id,
            target_company_id=old_company_id,
            reason="incorrect identity",
            apply=True,
        )

    assert source_row(engine, int(source["id"]))["company_id"] == old_company_id


def test_reassignment_rejects_running_sync(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    old_company_id = add_company(registry, "Old Employer", "old-employer")
    target_company_id = add_company(registry, "Correct Employer", "correct-employer")
    source = add_source(engine, old_company_id)
    JobSyncService(engine, clock=lambda: CREATED_AT).start_run(
        company_source_id=int(source["id"]),
        run_key="still-running",
        provider="greenhouse",
        adapter_version="test",
    )

    with pytest.raises(CompanyIdentityConflict, match="running sync"):
        registry.reassign_source_identity(
            provider="greenhouse",
            external_id="acme",
            expected_company_id=old_company_id,
            target_company_id=target_company_id,
            reason="incorrect identity",
            apply=True,
        )

    assert source_row(engine, int(source["id"]))["company_id"] == old_company_id


def test_reassignment_can_create_provisional_target_in_same_repair(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    old_company_id = add_company(registry, "Old Employer", "old-employer")
    source = add_source(engine, old_company_id)

    result = registry.reassign_source_identity(
        provider="greenhouse",
        external_id="acme",
        expected_company_id=old_company_id,
        new_company_name="Provider Confirmed Employer",
        new_company_slug="provider-confirmed",
        reason="provider confirms a distinct employer but no domain is verified",
        apply=True,
        now=REPAIRED_AT,
    )

    assert result.new_company_created is True
    assert result.after.company_id is not None
    with engine.connect() as connection:
        target = (
            connection.execute(
                select(companies_table).where(companies_table.c.id == result.after.company_id)
            )
            .mappings()
            .one()
        )
    assert target["name"] == "Provider Confirmed Employer"
    assert target["slug"] == "provider-confirmed"
    assert target["identity_state"] == "provisional"
    assert target["website"] is None
    assert source_row(engine, int(source["id"]))["company_id"] == target["id"]


def test_disable_source_is_dry_run_by_default_then_preserves_children_on_apply(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    company_id = add_company(registry, "Portfolio", "portfolio")
    source = add_source(engine, company_id, external_id="portfolio-board")
    source_id = int(source["id"])
    seed_completed_job(engine, source_id, external_id="portfolio-board")

    def unexpected_schema_change(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dry-run must not invoke schema migration")

    with monkeypatch.context() as context:
        context.setattr(company_registry_service, "create_schema", unexpected_schema_change)
        preview = registry.disable_source_identity(
            provider="greenhouse",
            external_id="portfolio-board",
            expected_company_id=company_id,
            reason="source is a multi-employer portfolio board",
        )
    assert preview.applied is False
    assert preview.before.status == "active"
    assert preview.after.status == "disabled"
    assert source_row(engine, source_id)["status"] == "active"

    applied = registry.disable_source_identity(
        provider="greenhouse",
        external_id="portfolio-board",
        expected_company_id=company_id,
        reason="source is a multi-employer portfolio board",
        apply=True,
        now=REPAIRED_AT,
    )
    disabled = source_row(engine, source_id)
    assert applied.before.dependent_counts == CompanySourceDependentCounts(jobs=1, sync_runs=1)
    assert applied.after.dependent_counts == applied.before.dependent_counts
    assert disabled["status"] == "disabled"
    assert disabled["metadata"]["identity_repair"][-1]["action"] == "disable"
    assert disabled["metadata"]["identity_repair"][-1]["old_company_id"] == company_id
    assert disabled["metadata"]["identity_repair"][-1]["new_company_id"] == company_id


def test_disable_source_rejects_running_sync(postgres_database_url: str) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    company_id = add_company(registry, "Portfolio", "portfolio")
    source = add_source(engine, company_id, external_id="portfolio-board")
    JobSyncService(engine, clock=lambda: CREATED_AT).start_run(
        company_source_id=int(source["id"]),
        run_key="still-running",
        provider="greenhouse",
        adapter_version="test",
    )

    with pytest.raises(CompanyIdentityConflict, match="running sync"):
        registry.disable_source_identity(
            provider="greenhouse",
            external_id="portfolio-board",
            expected_company_id=company_id,
            reason="source is a multi-employer portfolio board",
            apply=True,
        )
    assert source_row(engine, int(source["id"]))["status"] == "active"


@pytest.mark.parametrize("sync_mode", ["complete_snapshot", "observation"])
def test_sync_start_waits_for_source_repair_and_rejects_committed_disable(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    sync_mode: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    registry = CompanyRegistry(engine)
    company_id = add_company(registry, "Portfolio", "portfolio")
    source = add_source(
        engine,
        company_id,
        external_id="portfolio-board",
        sync_mode=sync_mode,
    )
    source_id = int(source["id"])
    repair_holds_source_lock = Event()
    allow_repair_to_finish = Event()
    sync_lock_attempted = Event()
    original_reject_running = company_registry_service._reject_running_source_syncs

    def pause_repair_after_source_lock(connection, company_source_id: int) -> None:
        original_reject_running(connection, company_source_id)
        repair_holds_source_lock.set()
        if not allow_repair_to_finish.wait(timeout=5):
            raise AssertionError("test did not release source repair")

    monkeypatch.setattr(
        company_registry_service,
        "_reject_running_source_syncs",
        pause_repair_after_source_lock,
    )

    def observe_sync_lock(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.upper().split())
        if "FROM COMPANY_SOURCES" in normalized and "FOR UPDATE" in normalized:
            sync_lock_attempted.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        repair_future = pool.submit(
            registry.disable_source_identity,
            provider="greenhouse",
            external_id="portfolio-board",
            expected_company_id=company_id,
            reason="source is a multi-employer portfolio board",
            apply=True,
            now=REPAIRED_AT,
        )
        assert repair_holds_source_lock.wait(timeout=5)
        sqlalchemy_event.listen(engine, "before_cursor_execute", observe_sync_lock)
        try:
            sync_service = JobSyncService(engine, clock=lambda: REPAIRED_AT)
            if sync_mode == "complete_snapshot":
                sync_future = pool.submit(
                    sync_service.start_run,
                    company_source_id=source_id,
                    run_key="racing-start",
                    provider="greenhouse",
                    adapter_version="test",
                )
            else:
                sync_future = pool.submit(
                    sync_service.sync_observations,
                    company_source_id=source_id,
                    run_key="racing-start",
                    jobs=[],
                    adapter_version="test",
                )
            assert sync_lock_attempted.wait(timeout=5)
            assert sync_future.done() is False
            allow_repair_to_finish.set()
            assert repair_future.result(timeout=5).applied is True
            with pytest.raises(ValueError, match="company source is disabled"):
                sync_future.result(timeout=5)
        finally:
            allow_repair_to_finish.set()
            sqlalchemy_event.remove(engine, "before_cursor_execute", observe_sync_lock)

    with engine.connect() as connection:
        run_count = connection.scalar(
            select(func.count())
            .select_from(sync_runs_table)
            .where(sync_runs_table.c.company_source_id == source_id)
        )
    assert run_count == 0
    assert source_row(engine, source_id)["status"] == "disabled"
