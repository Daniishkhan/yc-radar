#!/usr/bin/env python3
"""Audit and conservatively clean duplicate/low-value URL inventory rows.

Dry run is the default.  `--apply` requires the reviewed dry-run manifest in the
same audit directory and writes before-images before one transactional mutation.
Raw career-page discovery events are never selected for mutation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Connection, Engine

from yc_radar.core.config import get_settings
from yc_radar.services.database import (
    URL_INVENTORY_ADVISORY_LOCK,
    career_page_discovery_events_table,
    career_sources_table,
    company_career_pages_table,
    discovered_urls_table,
    engine_from_url,
    external_job_postings_table,
    job_posting_observations_table,
    job_posting_versions_table,
    job_postings_table,
    page_classifications_table,
    source_documents_table,
    source_sync_runs_table,
)
from yc_radar.services.url_quality import (
    POLICY_VERSION,
    canonical_url_key,
    inventory_rejection_reason,
    normalize_url,
    quality_rejection_reason,
)

TABLES = {
    "career_page_discovery_events": career_page_discovery_events_table,
    "company_career_pages": company_career_pages_table,
    "discovered_urls": discovered_urls_table,
    "source_documents": source_documents_table,
    "page_classifications": page_classifications_table,
    "external_job_postings": external_job_postings_table,
    "career_sources": career_sources_table,
    "source_sync_runs": source_sync_runs_table,
    "job_postings": job_postings_table,
    "job_posting_versions": job_posting_versions_table,
    "job_posting_observations": job_posting_observations_table,
}


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    parser = argparse.ArgumentParser(description="Audit or conservatively clean local URL inventory.")
    parser.add_argument("--audit-dir", type=Path, default=settings.local_debug_dir / "url-cleanup" / stamp)
    parser.add_argument("--apply", action="store_true", help="Apply only the reviewed matching dry run.")
    return parser.parse_args()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode())


def canonical_jsonl_bytes(values: list[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str) + "\n"
        for value in values
    ).encode()


def action_digest(actions: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_jsonl_bytes(actions)).hexdigest()


def atomic_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    _atomic_bytes(path, canonical_jsonl_bytes(values))


def load_reviewed_actions(path: Path) -> list[dict[str, Any]]:
    """Load the exact dry-run action list that the operator reviewed."""
    try:
        actions = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("reviewed actions.jsonl is missing or malformed") from exc
    if not all(isinstance(action, dict) for action in actions):
        raise RuntimeError("reviewed actions.jsonl must contain JSON objects")
    return actions


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        raise


def table_counts(connection: Connection) -> dict[str, int]:
    counts = {name: int(connection.scalar(select(func.count()).select_from(table)) or 0) for name, table in TABLES.items()}
    counts["discovered_urls_active"] = int(
        connection.scalar(select(func.count()).select_from(discovered_urls_table).where(discovered_urls_table.c.is_active.is_(True))) or 0
    )
    counts["discovered_urls_inactive"] = int(
        connection.scalar(select(func.count()).select_from(discovered_urls_table).where(discovered_urls_table.c.is_active.is_(False))) or 0
    )
    return counts


def load_inventory(
    connection: Connection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, dict[str, Any]]]:
    pages = [
        dict(row)
        for row in connection.execute(select(company_career_pages_table)).mappings()
    ]
    urls = [dict(row) for row in connection.execute(select(discovered_urls_table)).mappings()]
    classifications = {
        int(row["discovered_url_id"]): dict(row)
        for row in connection.execute(
            select(page_classifications_table).where(page_classifications_table.c.discovered_url_id.is_not(None))
        ).mappings()
    }
    return pages, urls, classifications


def load_career_source_urls(connection: Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(select(career_sources_table)).mappings()]


def inventory_fingerprint(
    pages: list[dict[str, Any]],
    urls: list[dict[str, Any]],
    classifications: dict[int, dict[str, Any]],
    career_sources: list[dict[str, Any]] | None = None,
) -> str:
    # Include every field that can alter an action, quality reason, or duplicate
    # survivor. An apply must reject even a same-count plan whose winner changed.
    rows = {
        "pages": [
            [
                row["id"],
                row["company_slug"],
                row["normalized_url"],
                row.get("career_page_url"),
                row.get("is_primary"),
                row.get("confidence"),
                row.get("http_status"),
                row.get("observed_source_count"),
                row.get("checked_at"),
            ]
            for row in sorted(pages, key=lambda item: int(item["id"]))
        ],
        "urls": [
            [
                row["id"],
                row["company_slug"],
                row["normalized_url"],
                row.get("url"),
                row.get("url_key"),
                row.get("is_active"),
                row.get("is_primary"),
                row.get("confidence"),
                row.get("http_status"),
                row.get("source_event_count"),
                row.get("first_seen_at"),
            ]
            for row in sorted(urls, key=lambda item: int(item["id"]))
        ],
        "classifications": [
            [identifier, row.get("page_kind"), row.get("http_status")]
            for identifier, row in sorted(classifications.items())
        ],
        "career_sources": [
            [
                row["id"],
                row.get("source_url"),
                row.get("discovered_from_url"),
            ]
            for row in sorted(career_sources or [], key=lambda item: int(item["id"]))
        ],
    }
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _successful(row: dict[str, Any], classifications: dict[int, dict[str, Any]]) -> bool:
    # Classification IDs belong to discovered_urls, never company_career_pages.
    if "url_key" not in row:
        return False
    classification = classifications.get(int(row["id"]))
    if not classification:
        return False
    status = classification.get("http_status")
    return status is not None and 200 <= int(status) < 400


def _survivor_key(row: dict[str, Any], classifications: dict[int, dict[str, Any]]) -> tuple[Any, ...]:
    # Lower tuple wins: verified provider board, fetched success, active/primary, HTTP, evidence, age, ID.
    normalized = str(row.get("normalized_url") or row.get("career_page_url") or "")
    return (
        0 if _company_ats(normalized) else 1,
        0 if normalize_url(normalized, normalized) == normalized else 1,
        0 if _successful(row, classifications) else 1,
        0 if row.get("is_active", True) else 1,
        0 if row.get("is_primary") else 1,
        0 if (row.get("http_status") or 0) and (row.get("http_status") or 0) < 400 else 1,
        -float(row.get("confidence") or 0),
        -int(row.get("source_event_count") or row.get("observed_source_count") or 0),
        str(row.get("first_seen_at") or row.get("checked_at") or ""),
        int(row["id"]),
    )


def _company_ats(url: str) -> bool:
    from yc_radar.services.source_providers import is_company_ats_url

    return is_company_ats_url(url)


def _action(
    category: str,
    *,
    winner_id: int | None = None,
    loser_id: int | None = None,
    reason: str | None = None,
    company_slug: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    core = {
        "category": category,
        "winner_id": winner_id,
        "loser_id": loser_id,
        "reason": reason,
        "company_slug": company_slug,
        **details,
    }
    core["action_id"] = hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]
    return core


def build_cleanup_plan(
    pages: list[dict[str, Any]],
    urls: list[dict[str, Any]],
    classifications: dict[int, dict[str, Any]],
    career_sources: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Produce deterministic, conservative actions without mutating input or storage."""
    actions: list[dict[str, Any]] = []
    for category, rows in (("company_career_page", pages), ("discovered_url", urls)):
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if category == "discovered_url" and not row.get("is_active"):
                continue
            normalized = str(row.get("normalized_url") or row.get("career_page_url") or "")
            key = canonical_url_key(normalized)
            if key:
                groups[(str(row["company_slug"]), key)].append(row)
        for (company_slug, _), members in sorted(groups.items()):
            if len(members) < 2:
                continue
            winner = min(members, key=lambda row: _survivor_key(row, classifications))
            for loser in sorted((row for row in members if row["id"] != winner["id"]), key=lambda row: int(row["id"])):
                actions.append(
                    _action(
                        f"{category}_duplicate",
                        winner_id=int(winner["id"]),
                        loser_id=int(loser["id"]),
                        company_slug=company_slug,
                    )
                )

    duplicate_page_losers = {
        action["loser_id"]
        for action in actions
        if action["category"] == "company_career_page_duplicate"
    }
    for row in sorted(pages, key=lambda item: int(item["id"])):
        if int(row["id"]) in duplicate_page_losers:
            continue
        url = str(row.get("normalized_url") or row.get("career_page_url") or "")
        reason = inventory_rejection_reason(str(row["company_slug"]), url)
        category = "company_career_page_invalid_delete"
        if reason is None:
            reason = quality_rejection_reason(url)
            category = "company_career_page_quality_delete"
        if reason:
            actions.append(
                _action(
                    category,
                    loser_id=int(row["id"]),
                    reason=reason,
                    company_slug=str(row["company_slug"]),
                )
            )
            continue
        canonical = normalize_url(url, url)
        if canonical and canonical != url:
            actions.append(
                _action(
                    "company_career_page_canonicalize",
                    winner_id=int(row["id"]),
                    reason="canonicalize_scheme_host_or_query",
                    company_slug=str(row["company_slug"]),
                    before_url=url,
                    after_url=canonical,
                )
            )

    duplicate_url_losers = {
        action["loser_id"]
        for action in actions
        if action["category"] == "discovered_url_duplicate"
    }
    successful_by_company = {
        str(row["company_slug"])
        for row in urls
        if row.get("is_active") and _successful(row, classifications)
    }
    for row in sorted(urls, key=lambda item: int(item["id"])):
        if not row.get("is_active") or int(row["id"]) in duplicate_url_losers:
            continue
        url = str(row.get("normalized_url") or row.get("url") or "")
        inventory_reason = inventory_rejection_reason(str(row["company_slug"]), url)
        if inventory_reason:
            actions.append(
                _action(
                    "discovered_url_inventory_deactivate",
                    loser_id=int(row["id"]),
                    reason=inventory_reason,
                    company_slug=str(row["company_slug"]),
                )
            )
            continue
        reason = quality_rejection_reason(url)
        if reason:
            actions.append(
                _action(
                    "discovered_url_quality_deactivate",
                    loser_id=int(row["id"]),
                    reason=reason,
                    company_slug=str(row["company_slug"]),
                )
            )
            continue
        canonical = normalize_url(url, url)
        if canonical and canonical != url:
            actions.append(
                _action(
                    "discovered_url_canonicalize",
                    winner_id=int(row["id"]),
                    reason="canonicalize_scheme_host_or_query",
                    company_slug=str(row["company_slug"]),
                    before_url=url,
                    after_url=canonical,
                )
            )
        classification = classifications.get(int(row["id"]))
        if (
            classification
            and classification.get("page_kind") == "fetch_error"
            and classification.get("http_status") in {404, 410}
            and str(row["company_slug"]) in successful_by_company
        ):
            actions.append(
                _action(
                    "discovered_url_terminal_error_deactivate",
                    loser_id=int(row["id"]),
                    reason=f"http_{classification['http_status']}_with_stronger_survivor",
                    company_slug=str(row["company_slug"]),
                )
            )

    for row in sorted(career_sources or [], key=lambda item: int(item["id"])):
        source_url = str(row.get("source_url") or "")
        raw_discovered_from_url = row.get("discovered_from_url")
        discovered_from_url = (
            str(raw_discovered_from_url) if raw_discovered_from_url is not None else None
        )
        canonical_source_url = normalize_url(source_url, source_url) or source_url
        canonical_discovered_url = (
            normalize_url(discovered_from_url, discovered_from_url)
            if discovered_from_url
            else None
        )
        if (
            canonical_source_url != source_url
            or canonical_discovered_url != discovered_from_url
        ):
            actions.append(
                _action(
                    "career_source_url_canonicalize",
                    winner_id=int(row["id"]),
                    reason="canonicalize_scheme_host_or_query",
                    before_url=source_url,
                    after_url=canonical_source_url,
                    before_discovered_from_url=discovered_from_url,
                    after_discovered_from_url=canonical_discovered_url,
                )
            )

    page_loser_ids = {
        int(action["loser_id"])
        for action in actions
        if action["category"]
        in {
            "company_career_page_duplicate",
            "company_career_page_invalid_delete",
            "company_career_page_quality_delete",
        }
    }
    surviving_pages: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pages:
        if int(row["id"]) not in page_loser_ids:
            surviving_pages[str(row["company_slug"])].append(row)
    for company_slug, members in sorted(surviving_pages.items()):
        primary_count = sum(bool(row.get("is_primary")) for row in members)
        if primary_count != 1:
            winner = min(members, key=lambda row: _survivor_key(row, classifications))
            actions.append(
                _action(
                    "company_career_page_primary_reselect",
                    winner_id=int(winner["id"]),
                    reason=f"surviving_primary_count_{primary_count}",
                    company_slug=company_slug,
                )
            )

    url_loser_ids = {
        int(action["loser_id"])
        for action in actions
        if action["category"].startswith("discovered_url_")
        and action.get("loser_id") is not None
    }
    surviving_urls: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in urls:
        if row.get("is_active") and int(row["id"]) not in url_loser_ids:
            surviving_urls[str(row["company_slug"])].append(row)
    for company_slug, members in sorted(surviving_urls.items()):
        primary_count = sum(bool(row.get("is_primary")) for row in members)
        if primary_count != 1:
            winner = min(members, key=lambda row: _survivor_key(row, classifications))
            actions.append(
                _action(
                    "discovered_url_primary_reselect",
                    winner_id=int(winner["id"]),
                    reason=f"surviving_primary_count_{primary_count}",
                    company_slug=company_slug,
                )
            )
    return sorted(
        actions,
        key=lambda action: (
            action["category"],
            action.get("company_slug") or "",
            action.get("loser_id") or 0,
        ),
    )


def action_counts(actions: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(action["category"] for action in actions).items()))


def verify_reviewed_action_plan(
    manifest: dict[str, Any],
    reviewed_actions: list[dict[str, Any]],
    recomputed_actions: list[dict[str, Any]],
) -> None:
    """Require the current plan to be byte-for-byte the reviewed dry-run plan."""
    expected_digest = manifest.get("actions_sha256")
    expected_ids = manifest.get("action_ids")
    if not isinstance(expected_digest, str) or not isinstance(expected_ids, list):
        raise RuntimeError("dry-run manifest lacks an action digest; run a fresh audit before --apply")
    reviewed_ids = [str(action.get("action_id") or "") for action in reviewed_actions]
    if action_digest(reviewed_actions) != expected_digest or reviewed_ids != expected_ids:
        raise RuntimeError("reviewed actions.jsonl does not match its manifest")
    if action_counts(recomputed_actions) != manifest.get("action_counts"):
        raise RuntimeError("cleanup action counts changed after dry run; run a fresh audit before --apply")
    recomputed_ids = [str(action.get("action_id") or "") for action in recomputed_actions]
    if action_digest(recomputed_actions) != expected_digest or recomputed_ids != expected_ids:
        raise RuntimeError("cleanup action plan changed after dry run; run a fresh audit before --apply")


def write_dry_run_artifacts(
    audit_dir: Path,
    *,
    database: str,
    counts: dict[str, int],
    fingerprint: str,
    actions: list[dict[str, Any]],
) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    # Publish the reviewed action content before its manifest so --apply never
    # accepts a manifest that points at a partial or different plan.
    atomic_json(audit_dir / "before-counts.json", counts)
    atomic_jsonl(audit_dir / "actions.jsonl", actions)
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", delete=False, dir=audit_dir) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "action_id",
                "category",
                "company_slug",
                "winner_id",
                "loser_id",
                "reason",
                "before_url",
                "after_url",
                "before_discovered_from_url",
                "after_discovered_from_url",
            ],
        )
        writer.writeheader()
        writer.writerows(actions)
        temporary = Path(handle.name)
    os.replace(temporary, audit_dir / "actions.csv")
    manifest = {
        "policy_version": POLICY_VERSION,
        "database": database,
        "input_fingerprint": fingerprint,
        "generated_at": datetime.now(UTC).isoformat(),
        "action_counts": action_counts(actions),
        "action_ids": [str(action["action_id"]) for action in actions],
        "actions_sha256": action_digest(actions),
        "dry_run": True,
    }
    atomic_json(audit_dir / "manifest.json", manifest)


def _merge_list(*values: Any) -> list[Any]:
    merged: list[Any] = []
    for value in values:
        for item in value if isinstance(value, list) else []:
            if item not in merged:
                merged.append(item)
    return merged


def _cleanup_raw_json(row: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    raw = dict(row.get("raw_json") or {})
    history = list(raw.get("url_cleanup_actions") or [])
    if action["action_id"] not in history:
        history.append(action["action_id"])
    raw["url_cleanup_actions"] = history
    return raw


def apply_cleanup_plan(engine: Engine, audit_dir: Path, manifest: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    """Apply one verified plan under a session advisory lock and return before/after counts."""
    with engine.connect() as connection:
        locked = connection.scalar(
            select(func.pg_try_advisory_lock(func.hashtext(URL_INVENTORY_ADVISORY_LOCK)))
        )
        if not locked:
            raise RuntimeError("URL cleanup or URL-inventory pipeline work is already running")
        try:
            reviewed_actions = load_reviewed_actions(audit_dir / "actions.jsonl")
            connection.commit()
            pages, urls, classifications = load_inventory(connection)
            career_sources = load_career_source_urls(connection)
            fresh_fingerprint = inventory_fingerprint(
                pages, urls, classifications, career_sources
            )
            if fresh_fingerprint != manifest["input_fingerprint"]:
                raise RuntimeError("inventory changed after dry run; run a fresh audit before --apply")
            actions = build_cleanup_plan(pages, urls, classifications, career_sources)
            verify_reviewed_action_plan(manifest, reviewed_actions, actions)
            # End read preflight, then repeat it under the mutation transaction.
            connection.commit()
            with connection.begin():
                pages, urls, classifications = load_inventory(connection)
                career_sources = load_career_source_urls(connection)
                if (
                    inventory_fingerprint(pages, urls, classifications, career_sources)
                    != manifest["input_fingerprint"]
                ):
                    raise RuntimeError("inventory changed during cleanup preflight")
                transactional_actions = build_cleanup_plan(
                    pages, urls, classifications, career_sources
                )
                verify_reviewed_action_plan(manifest, reviewed_actions, transactional_actions)
                actions = transactional_actions
                page_by_id = {int(row["id"]): row for row in pages}
                url_by_id = {int(row["id"]): row for row in urls}
                career_source_by_id = {
                    int(row["id"]): row for row in career_sources
                }
                page_action_companies = {
                    str(action["company_slug"])
                    for action in actions
                    if action["category"].startswith("company_career_page_")
                }
                url_action_companies = {
                    str(action["company_slug"])
                    for action in actions
                    if action["category"].startswith("discovered_url_")
                }
                deleted_page_ids = {
                    int(action["loser_id"])
                    for action in actions
                    if action["category"]
                    in {
                        "company_career_page_duplicate",
                        "company_career_page_invalid_delete",
                        "company_career_page_quality_delete",
                    }
                }
                touched_page_ids = set(deleted_page_ids)
                touched_page_ids.update(
                    int(action["winner_id"])
                    for action in actions
                    if action["category"]
                    in {
                        "company_career_page_canonicalize",
                        "company_career_page_duplicate",
                    }
                )
                touched_page_ids.update(
                    int(row["id"])
                    for row in pages
                    if str(row["company_slug"]) in page_action_companies
                )
                touched_url_ids = {
                    int(action["loser_id"])
                    for action in actions
                    if action["category"].startswith("discovered_url_")
                    and action.get("loser_id") is not None
                }
                touched_url_ids.update(
                    int(action["winner_id"])
                    for action in actions
                    if action["category"]
                    in {"discovered_url_canonicalize", "discovered_url_duplicate"}
                )
                touched_url_ids.update(
                    int(row["id"])
                    for row in urls
                    if str(row["company_slug"]) in url_action_companies
                )
                touched_career_source_ids = {
                    int(action["winner_id"])
                    for action in actions
                    if action["category"] == "career_source_url_canonicalize"
                }
                if touched_page_ids:
                    connection.execute(
                        select(company_career_pages_table.c.id)
                        .where(company_career_pages_table.c.id.in_(touched_page_ids))
                        .with_for_update()
                    )
                affected_page_urls = [
                    str(page_by_id[row_id]["normalized_url"])
                    for row_id in deleted_page_ids
                ]
                if affected_page_urls:
                    source_references = connection.scalar(
                        select(func.count())
                        .select_from(career_sources_table)
                        .where(career_sources_table.c.discovered_from_url.in_(affected_page_urls))
                    )
                    if source_references:
                        raise RuntimeError("refusing to delete pages referenced by career sources")
                if touched_url_ids:
                    connection.execute(
                        select(discovered_urls_table.c.id)
                        .where(discovered_urls_table.c.id.in_(touched_url_ids))
                        .with_for_update()
                    )
                if touched_career_source_ids:
                    locked_sources = connection.execute(
                        select(career_sources_table)
                        .where(career_sources_table.c.id.in_(touched_career_source_ids))
                        .with_for_update()
                    ).mappings()
                    career_source_by_id.update(
                        {int(row["id"]): dict(row) for row in locked_sources}
                    )
                backup = [
                    {"table": "company_career_pages", "row": page_by_id[row_id]}
                    for row_id in sorted(touched_page_ids)
                ] + [
                    {"table": "discovered_urls", "row": url_by_id[row_id]}
                    for row_id in sorted(touched_url_ids)
                ] + [
                    {"table": "career_sources", "row": career_source_by_id[row_id]}
                    for row_id in sorted(touched_career_source_ids)
                ]
                backup_bytes = canonical_jsonl_bytes(backup)
                backup_sha256 = hashlib.sha256(backup_bytes).hexdigest()
                atomic_jsonl(audit_dir / "backup.jsonl", backup)
                if hashlib.sha256((audit_dir / "backup.jsonl").read_bytes()).hexdigest() != backup_sha256:
                    raise RuntimeError("cleanup backup verification failed")
                atomic_json(
                    audit_dir / "backup-manifest.json",
                    {
                        "actions_sha256": action_digest(actions),
                        "backup_sha256": backup_sha256,
                        "row_count": len(backup),
                    },
                )
                before = table_counts(connection)
                raw_event_count = before["career_page_discovery_events"]
                canonical_before = {
                    name: before[name]
                    for name in (
                        "career_sources",
                        "job_postings",
                        "job_posting_versions",
                        "job_posting_observations",
                    )
                }
                for action in actions:
                    category = action["category"]
                    if category in {
                        "company_career_page_primary_reselect",
                        "discovered_url_primary_reselect",
                    }:
                        continue
                    if category == "career_source_url_canonicalize":
                        source = career_source_by_id[int(action["winner_id"])]
                        connection.execute(
                            update(career_sources_table)
                            .where(career_sources_table.c.id == source["id"])
                            .values(
                                source_url=action["after_url"],
                                discovered_from_url=action[
                                    "after_discovered_from_url"
                                ],
                                raw_json=_cleanup_raw_json(source, action),
                            )
                        )
                    elif category in {
                        "company_career_page_invalid_delete",
                        "company_career_page_quality_delete",
                    }:
                        loser = page_by_id[int(action["loser_id"])]
                        connection.execute(
                            delete(company_career_pages_table).where(
                                company_career_pages_table.c.id == loser["id"]
                            )
                        )
                    elif category == "company_career_page_canonicalize":
                        winner = page_by_id[int(action["winner_id"])]
                        connection.execute(
                            update(company_career_pages_table)
                            .where(company_career_pages_table.c.id == winner["id"])
                            .values(
                                career_page_url=action["after_url"],
                                normalized_url=action["after_url"],
                                raw_json=_cleanup_raw_json(winner, action),
                            )
                        )
                    elif category == "company_career_page_duplicate":
                        winner = page_by_id[int(action["winner_id"])]
                        loser = page_by_id[int(action["loser_id"])]
                        merged_raw = _cleanup_raw_json(winner, action)
                        merged_raw["merged_page_ids"] = sorted(set(merged_raw.get("merged_page_ids", []) + [int(loser["id"])]))
                        connection.execute(
                            update(company_career_pages_table)
                            .where(company_career_pages_table.c.id == winner["id"])
                            .values(
                                observed_source_count=int(winner.get("observed_source_count") or 0)
                                + int(loser.get("observed_source_count") or 0),
                                confidence=max(float(winner.get("confidence") or 0), float(loser.get("confidence") or 0)),
                                raw_json=merged_raw,
                            )
                        )
                        connection.execute(delete(company_career_pages_table).where(company_career_pages_table.c.id == loser["id"]))
                    elif category == "discovered_url_canonicalize":
                        winner = url_by_id[int(action["winner_id"])]
                        connection.execute(
                            update(discovered_urls_table)
                            .where(discovered_urls_table.c.id == winner["id"])
                            .values(
                                url=action["after_url"],
                                normalized_url=action["after_url"],
                                url_key=canonical_url_key(action["after_url"]),
                                raw_json=_cleanup_raw_json(winner, action),
                            )
                        )
                    elif category == "discovered_url_duplicate":
                        winner = url_by_id[int(action["winner_id"])]
                        loser = url_by_id[int(action["loser_id"])]
                        merged_raw = _cleanup_raw_json(winner, action)
                        connection.execute(
                            update(discovered_urls_table)
                            .where(discovered_urls_table.c.id == winner["id"])
                            .values(
                                discovery_sources=_merge_list(winner.get("discovery_sources"), loser.get("discovery_sources")),
                                evidence_samples=_merge_list(winner.get("evidence_samples"), loser.get("evidence_samples")),
                                source_event_count=int(winner.get("source_event_count") or 0)
                                + int(loser.get("source_event_count") or 0),
                                confidence=max(float(winner.get("confidence") or 0), float(loser.get("confidence") or 0)),
                                fetch_priority=max(float(winner.get("fetch_priority") or 0), float(loser.get("fetch_priority") or 0)),
                                raw_json=merged_raw,
                            )
                        )
                        connection.execute(
                            update(discovered_urls_table)
                            .where(discovered_urls_table.c.id == loser["id"])
                            .values(is_active=False, is_primary=False, raw_json=_cleanup_raw_json(loser, action))
                        )
                    else:
                        loser = url_by_id[int(action["loser_id"])]
                        connection.execute(
                            update(discovered_urls_table)
                            .where(discovered_urls_table.c.id == loser["id"])
                            .values(is_active=False, is_primary=False, raw_json=_cleanup_raw_json(loser, action))
                        )
                for company_slug in sorted(page_action_companies):
                    current_pages = [
                        dict(row)
                        for row in connection.execute(
                            select(company_career_pages_table).where(company_career_pages_table.c.company_slug == company_slug)
                        ).mappings()
                    ]
                    if current_pages:
                        winner = min(current_pages, key=lambda row: _survivor_key(row, classifications))
                        connection.execute(
                            update(company_career_pages_table)
                            .where(company_career_pages_table.c.company_slug == company_slug)
                            .values(is_primary=False)
                        )
                        connection.execute(
                            update(company_career_pages_table)
                            .where(company_career_pages_table.c.id == winner["id"])
                            .values(is_primary=True)
                        )
                for company_slug in sorted(url_action_companies):
                    current_urls = [
                        dict(row)
                        for row in connection.execute(
                            select(discovered_urls_table).where(
                                discovered_urls_table.c.company_slug == company_slug,
                                discovered_urls_table.c.is_active.is_(True),
                            )
                        ).mappings()
                    ]
                    if current_urls:
                        winner = min(current_urls, key=lambda row: _survivor_key(row, classifications))
                        connection.execute(
                            update(discovered_urls_table)
                            .where(discovered_urls_table.c.company_slug == company_slug)
                            .values(is_primary=False)
                        )
                        connection.execute(
                            update(discovered_urls_table)
                            .where(discovered_urls_table.c.id == winner["id"])
                            .values(is_primary=True)
                        )
                after = table_counts(connection)
                if after["career_page_discovery_events"] != raw_event_count:
                    raise RuntimeError("raw discovery event invariant failed")
                if {name: after[name] for name in canonical_before} != canonical_before:
                    raise RuntimeError("canonical provider lifecycle invariant failed")
                active = [row for row in connection.execute(select(discovered_urls_table).where(discovered_urls_table.c.is_active.is_(True))).mappings()]
                seen: set[tuple[str, str]] = set()
                for row in active:
                    key = canonical_url_key(str(row["normalized_url"]))
                    pair = (str(row["company_slug"]), key or str(row["id"]))
                    if pair in seen:
                        raise RuntimeError("active canonical duplicate invariant failed")
                    seen.add(pair)
                for company_slug in url_action_companies:
                    active_primary_count = connection.scalar(
                        select(func.count())
                        .select_from(discovered_urls_table)
                        .where(
                            discovered_urls_table.c.company_slug == company_slug,
                            discovered_urls_table.c.is_active.is_(True),
                            discovered_urls_table.c.is_primary.is_(True),
                        )
                    )
                    active_count = connection.scalar(
                        select(func.count())
                        .select_from(discovered_urls_table)
                        .where(
                            discovered_urls_table.c.company_slug == company_slug,
                            discovered_urls_table.c.is_active.is_(True),
                        )
                    )
                    if active_count and active_primary_count != 1:
                        raise RuntimeError("active discovered URL primary invariant failed")
            return before, after
        finally:
            connection.execute(
                select(func.pg_advisory_unlock(func.hashtext(URL_INVENTORY_ADVISORY_LOCK)))
            )


def run(args: argparse.Namespace) -> None:
    engine = engine_from_url()
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN READ ONLY")
        database = str(connection.exec_driver_sql("SELECT current_database()").scalar_one())
        pages, urls, classifications = load_inventory(connection)
        career_sources = load_career_source_urls(connection)
        counts = table_counts(connection)
        fingerprint = inventory_fingerprint(
            pages, urls, classifications, career_sources
        )
        actions = build_cleanup_plan(
            pages, urls, classifications, career_sources
        )
        connection.exec_driver_sql("ROLLBACK")
    if not args.apply:
        write_dry_run_artifacts(
            args.audit_dir,
            database=database,
            counts=counts,
            fingerprint=fingerprint,
            actions=actions,
        )
        print(json.dumps({"audit_dir": str(args.audit_dir), "counts": counts, "actions": action_counts(actions)}, sort_keys=True))
        return
    manifest_path = args.audit_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit("--apply requires a prior dry run in the same --audit-dir")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("policy_version") != POLICY_VERSION or manifest.get("database") != database:
        raise SystemExit("dry-run manifest policy or database does not match this apply")
    before, after = apply_cleanup_plan(engine, args.audit_dir, manifest)
    atomic_json(args.audit_dir / "after-counts.json", after)
    atomic_json(args.audit_dir / "apply-summary.json", {"before": before, "after": after, "actions": action_counts(actions)})
    print(json.dumps({"audit_dir": str(args.audit_dir), "before": before, "after": after, "actions": action_counts(actions)}, sort_keys=True))


if __name__ == "__main__":
    run(parse_args())
