import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from yc_radar.services.database import (
    career_sources_table,
    company_career_pages_table,
    create_schema,
    discovered_urls_table,
    URL_INVENTORY_ADVISORY_LOCK,
    engine_from_url,
    replace_career_page_data,
    upsert_companies,
    url_inventory_writer_lock,
)
from yc_radar.services.job_repository import JobRepository

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_url_inventory.py"
SPEC = importlib.util.spec_from_file_location("cleanup_url_inventory", SCRIPT_PATH)
assert SPEC and SPEC.loader
cleanup_url_inventory = importlib.util.module_from_spec(SPEC)
sys.modules["cleanup_url_inventory"] = cleanup_url_inventory
SPEC.loader.exec_module(cleanup_url_inventory)


def test_cleanup_plan_merges_only_canonical_duplicates_and_deactivates_deterministic_low_value_rows() -> None:
    pages = [
        {
            "id": 1,
            "company_slug": "example",
            "normalized_url": "https://example.com/careers",
            "is_primary": True,
            "confidence": 0.84,
            "observed_source_count": 2,
        },
        {
            "id": 2,
            "company_slug": "example",
            "normalized_url": "http://www.example.com/careers?utm_source=footer",
            "is_primary": False,
            "confidence": 0.78,
            "observed_source_count": 1,
        },
    ]
    urls = [
        {
            "id": 1,
            "company_slug": "example",
            "normalized_url": "https://example.com/careers",
            "is_active": True,
            "is_primary": True,
            "confidence": 0.84,
            "source_event_count": 2,
        },
        {
            "id": 2,
            "company_slug": "example",
            "normalized_url": "http://www.example.com/careers?utm_source=footer",
            "is_active": True,
            "is_primary": False,
            "confidence": 0.78,
            "source_event_count": 1,
        },
        {
            "id": 3,
            "company_slug": "example",
            "normalized_url": "https://example.com/privacy",
            "is_active": True,
            "is_primary": False,
            "confidence": 0.5,
            "source_event_count": 1,
        },
    ]

    actions = cleanup_url_inventory.build_cleanup_plan(pages, urls, {})

    assert cleanup_url_inventory.action_counts(actions) == {
        "company_career_page_duplicate": 1,
        "discovered_url_duplicate": 1,
        "discovered_url_quality_deactivate": 1,
    }
    assert {action["loser_id"] for action in actions} == {2, 3}
    assert not any(action["loser_id"] == 1 for action in actions)

    urls[1]["is_active"] = False
    post_apply_actions = cleanup_url_inventory.build_cleanup_plan([pages[0]], urls, {})
    assert not any(action["category"] == "discovered_url_duplicate" for action in post_apply_actions)


def test_cleanup_plan_does_not_cross_wire_page_ids_or_none_http_statuses() -> None:
    pages = [
        {
            "id": 1,
            "company_slug": "example",
            "normalized_url": "https://example.com/careers",
            "is_primary": False,
            "confidence": 0.5,
        },
        {
            "id": 2,
            "company_slug": "example",
            "normalized_url": "https://example.com/careers",
            "is_primary": True,
            "confidence": 0.9,
        },
    ]
    urls = [
        {
            "id": 1,
            "company_slug": "other",
            "normalized_url": "https://other.example/careers",
            "url_key": "https://other.example/careers",
            "is_active": True,
            "is_primary": True,
        },
        {
            "id": 2,
            "company_slug": "other",
            "normalized_url": "https://other.example/jobs",
            "url_key": "https://other.example/jobs",
            "is_active": True,
            "is_primary": False,
        },
    ]
    classifications = {
        1: {"page_kind": "fetch_error", "http_status": None},
        2: {"page_kind": "fetch_error", "http_status": 404},
    }

    actions = cleanup_url_inventory.build_cleanup_plan(pages, urls, classifications)

    page_duplicate = next(
        action
        for action in actions
        if action["category"] == "company_career_page_duplicate"
    )
    assert page_duplicate["winner_id"] == 2
    assert not any(
        action["category"] == "discovered_url_terminal_error_deactivate"
        for action in actions
    )


def test_cleanup_plan_canonicalizes_filters_and_rejects_bad_primary_urls() -> None:
    tracked = "https://job-boards.greenhouse.io/koko?gh_src=campaign"
    canonical = "https://job-boards.greenhouse.io/koko"
    pages = [
        {
            "id": 1,
            "company_slug": "koko",
            "normalized_url": tracked,
            "is_primary": True,
            "confidence": 0.9,
        },
        {
            "id": 2,
            "company_slug": "dover",
            "normalized_url": "https://app.dover.com/",
            "is_primary": True,
            "confidence": 0.9,
        },
    ]
    urls = [
        {
            "id": 1,
            "company_slug": "koko",
            "normalized_url": tracked,
            "url_key": tracked,
            "is_active": True,
            "is_primary": True,
            "confidence": 0.9,
        },
        {
            "id": 2,
            "company_slug": "dover",
            "normalized_url": "https://app.dover.com/",
            "url_key": "https://app.dover.com/",
            "is_active": True,
            "is_primary": True,
            "confidence": 0.9,
        },
    ]
    sources = [
        {
            "id": 7,
            "source_url": tracked,
            "discovered_from_url": tracked,
        }
    ]

    actions = cleanup_url_inventory.build_cleanup_plan(pages, urls, {}, sources)

    assert cleanup_url_inventory.action_counts(actions) == {
        "career_source_url_canonicalize": 1,
        "company_career_page_canonicalize": 1,
        "company_career_page_quality_delete": 1,
        "discovered_url_canonicalize": 1,
        "discovered_url_quality_deactivate": 1,
    }
    canonicalizations = [
        action for action in actions if action["category"].endswith("canonicalize")
    ]
    assert {action["after_url"] for action in canonicalizations} == {canonical}


def test_cleanup_plan_quarantines_audited_fanout_and_vendor_navigation_only() -> None:
    pages = [
        {
            "id": 1,
            "company_slug": "kalibrr",
            "normalized_url": "https://www.kalibrr.com/c/other-company/jobs/123",
            "is_primary": True,
            "confidence": 0.9,
        },
        {
            "id": 2,
            "company_slug": "kalibrr",
            "normalized_url": "https://www.kalibrr.com/c/kalibrr-ph/jobs",
            "is_primary": False,
            "confidence": 0.8,
        },
    ]
    urls = [
        {
            "id": 1,
            "company_slug": "kalibrr",
            "normalized_url": pages[0]["normalized_url"],
            "is_active": True,
            "is_primary": True,
            "confidence": 0.9,
        },
        {
            "id": 2,
            "company_slug": "kalibrr",
            "normalized_url": pages[1]["normalized_url"],
            "is_active": True,
            "is_primary": False,
            "confidence": 0.8,
        },
        {
            "id": 3,
            "company_slug": "ashby",
            "normalized_url": "https://www.ashbyhq.com/pricing",
            "is_active": True,
            "is_primary": True,
            "confidence": 0.9,
        },
        {
            "id": 4,
            "company_slug": "ashby",
            "normalized_url": "https://www.ashbyhq.com/careers",
            "is_active": True,
            "is_primary": False,
            "confidence": 0.8,
        },
    ]

    actions = cleanup_url_inventory.build_cleanup_plan(pages, urls, {})

    assert cleanup_url_inventory.action_counts(actions) == {
        "company_career_page_invalid_delete": 1,
        "company_career_page_primary_reselect": 1,
        "discovered_url_inventory_deactivate": 2,
        "discovered_url_primary_reselect": 2,
    }
    assert {
        action["loser_id"]
        for action in actions
        if action.get("loser_id") is not None
    } == {1, 3}


def test_url_inventory_writer_lock_conflicts_with_cleanup_exclusive_lock(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    with url_inventory_writer_lock(engine):
        with engine.connect() as observer:
            assert observer.scalar(
                select(func.pg_try_advisory_lock(func.hashtext(URL_INVENTORY_ADVISORY_LOCK)))
            ) is False

    with engine.connect() as exclusive:
        assert exclusive.scalar(
            select(func.pg_try_advisory_lock(func.hashtext(URL_INVENTORY_ADVISORY_LOCK)))
        ) is True
        with pytest.raises(RuntimeError, match="cleanup apply is active"):
            with url_inventory_writer_lock(engine):
                pass
        exclusive.execute(select(func.pg_advisory_unlock(func.hashtext(URL_INVENTORY_ADVISORY_LOCK))))


def test_cleanup_apply_canonicalizes_page_queue_and_registered_source_urls(
    postgres_database_url: str, tmp_path: Path
) -> None:
    engine = engine_from_url(postgres_database_url)
    tracked = "https://job-boards.greenhouse.io/example?gh_src=campaign"
    canonical = "https://job-boards.greenhouse.io/example"
    now = datetime.now(UTC)
    upsert_companies(
        engine,
        [
            {
                "id": 1,
                "slug": "example",
                "name": "Example",
                "website": "https://example.com",
                "regions": [],
                "industries": [],
                "tags": [],
            }
        ],
    )
    replace_career_page_data(
        engine,
        discovery_events=[],
        career_pages=[
            {
                "company_id": 1,
                "company_slug": "example",
                "company_name": "Example",
                "career_page_url": tracked,
                "normalized_url": tracked,
                "page_type": "ats",
                "discovery_source": "test",
                "confidence": 0.9,
                "is_primary": True,
                "checked_at": now,
            }
        ],
        company_slugs=["example"],
    )
    JobRepository(engine).register_career_source(
        company_id=1,
        provider="greenhouse",
        source_kind="ats_board",
        external_source_id="example",
        source_url=tracked,
        discovered_from_url=tracked,
        now=now,
    )

    with engine.connect() as connection:
        pages, urls, classifications = cleanup_url_inventory.load_inventory(connection)
        sources = cleanup_url_inventory.load_career_source_urls(connection)
        counts = cleanup_url_inventory.table_counts(connection)
    actions = cleanup_url_inventory.build_cleanup_plan(
        pages, urls, classifications, sources
    )
    cleanup_url_inventory.write_dry_run_artifacts(
        tmp_path,
        database=str(engine.url.database),
        counts=counts,
        fingerprint=cleanup_url_inventory.inventory_fingerprint(
            pages, urls, classifications, sources
        ),
        actions=actions,
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    cleanup_url_inventory.apply_cleanup_plan(engine, tmp_path, manifest)

    with engine.connect() as connection:
        assert connection.scalar(select(company_career_pages_table.c.normalized_url)) == canonical
        assert connection.scalar(select(discovered_urls_table.c.normalized_url)) == canonical
        source = connection.execute(select(career_sources_table)).mappings().one()
    assert source["source_url"] == canonical
    assert source["discovered_from_url"] == canonical
    backup = [
        json.loads(line)
        for line in (tmp_path / "backup.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {entry["table"] for entry in backup} == {
        "career_sources",
        "company_career_pages",
        "discovered_urls",
    }


def test_cleanup_apply_requires_reviewed_digest_and_backs_up_all_mutated_rows(
    postgres_database_url: str, tmp_path: Path
) -> None:
    engine = engine_from_url(postgres_database_url)
    create_schema(engine)
    now = datetime.now(UTC)

    def page(url: str, *, primary: bool, confidence: float) -> dict:
        return {
            "company_slug": "example",
            "company_name": "Example",
            "website": "https://example.com",
            "yc_is_hiring": False,
            "yc_job_count": 0,
            "career_page_url": url,
            "normalized_url": url,
            "page_type": "careers_page",
            "discovery_source": "test",
            "confidence": confidence,
            "http_status": 200,
            "evidence": "test",
            "is_primary": primary,
            "observed_source_count": 1,
            "checked_at": now,
            "raw_json": {},
            "created_at": now,
            "updated_at": now,
        }

    def url(url_value: str, *, primary: bool) -> dict:
        return {
            "company_slug": "ashby",
            "company_name": "Ashby",
            "website": "https://ashbyhq.com",
            "url": url_value,
            "normalized_url": url_value,
            "url_key": f"legacy-{url_value}",
            "url_kind": "careers_page",
            "discovery_sources": ["test"],
            "evidence_samples": ["test"],
            "source_event_count": 1,
            "confidence": 0.8,
            "fetch_priority": 1.0 if primary else 0.8,
            "http_status": 200,
            "is_primary": primary,
            "is_active": True,
            "first_seen_at": now,
            "last_seen_at": now,
            "raw_json": {},
            "created_at": now,
            "updated_at": now,
        }

    with engine.begin() as connection:
        connection.execute(
            company_career_pages_table.insert(),
            [
                page("https://example.com/careers", primary=True, confidence=0.9),
                page("http://www.example.com/careers?utm_source=footer", primary=False, confidence=0.8),
                page("https://example.com/jobs", primary=False, confidence=0.7),
            ],
        )
        connection.execute(
            discovered_urls_table.insert(),
            [
                url("https://www.ashbyhq.com/pricing", primary=True),
                url("https://www.ashbyhq.com/careers", primary=False),
            ],
        )
    with engine.connect() as connection:
        pages, urls, classifications = cleanup_url_inventory.load_inventory(connection)
        counts = cleanup_url_inventory.table_counts(connection)
    actions = cleanup_url_inventory.build_cleanup_plan(pages, urls, classifications)
    fingerprint = cleanup_url_inventory.inventory_fingerprint(pages, urls, classifications)
    cleanup_url_inventory.write_dry_run_artifacts(
        tmp_path,
        database=str(engine.url.database),
        counts=counts,
        fingerprint=fingerprint,
        actions=actions,
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    reviewed_actions = cleanup_url_inventory.load_reviewed_actions(tmp_path / "actions.jsonl")

    changed_survivor_input = [dict(row) for row in urls]
    changed_survivor_input[0]["source_event_count"] += 1
    assert cleanup_url_inventory.inventory_fingerprint(pages, changed_survivor_input, classifications) != fingerprint
    tampered_actions = [dict(action) for action in reviewed_actions]
    tampered_actions[0]["winner_id"] = 999
    with pytest.raises(RuntimeError, match="reviewed actions"):
        cleanup_url_inventory.verify_reviewed_action_plan(manifest, tampered_actions, actions)

    before, after = cleanup_url_inventory.apply_cleanup_plan(engine, tmp_path, manifest)

    backup = [json.loads(line) for line in (tmp_path / "backup.jsonl").read_text(encoding="utf-8").splitlines()]
    backed_up_page_ids = {entry["row"]["id"] for entry in backup if entry["table"] == "company_career_pages"}
    backed_up_url_ids = {entry["row"]["id"] for entry in backup if entry["table"] == "discovered_urls"}
    assert backed_up_page_ids == {row["id"] for row in pages}
    assert backed_up_url_ids == {row["id"] for row in urls}
    backup_manifest = json.loads((tmp_path / "backup-manifest.json").read_text(encoding="utf-8"))
    assert backup_manifest["backup_sha256"] == hashlib.sha256((tmp_path / "backup.jsonl").read_bytes()).hexdigest()
    assert before["career_page_discovery_events"] == after["career_page_discovery_events"] == 0

    with engine.connect() as connection:
        active_urls = [
            dict(row)
            for row in connection.execute(
                select(discovered_urls_table).where(discovered_urls_table.c.is_active.is_(True))
            ).mappings()
        ]
    assert len(active_urls) == 1
    assert active_urls[0]["normalized_url"] == "https://www.ashbyhq.com/careers"
    assert active_urls[0]["is_primary"] is True
