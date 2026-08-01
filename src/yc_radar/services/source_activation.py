"""Frozen, restart-safe registration batches for discovered ATS sources.

The ordinary discovery path is intentionally lightweight and idempotent.  This
module is for detached backfills where the exact candidate set must survive a
process restart.  It snapshots provider-recognized career-page evidence before
performing any registrations, rejects ambiguous company ownership up front,
and records every candidate outcome in an atomic local checkpoint.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from yc_radar.services.database import (
    fetch_company_career_page_rows,
    url_inventory_writer_lock,
)
from yc_radar.services.job_source_registry import (
    JobSourceProviderRegistry,
    JobSourceRegistry,
    default_job_source_providers,
)
from yc_radar.services.run_status import read_status, write_status

CHECKPOINT_SCHEMA_VERSION = 1
TERMINAL_STATES = frozenset({"registered", "existing", "conflict", "exhausted"})
RETRYABLE_STATES = frozenset({"pending", "running", "failed"})

ProgressCallback = Callable[[dict[str, Any]], None]


def activate_discovered_sources(
    engine: Engine,
    *,
    provider: str,
    checkpoint_file: Path,
    checkpoint_every: int = 10,
    max_attempts: int = 3,
    providers: JobSourceProviderRegistry | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Register one frozen provider inventory, resuming safely after interruption.

    A database commit can occur immediately before process death and therefore
    before its local candidate state is published.  Retrying that candidate is
    safe because ``JobSourceRegistry.register_url`` is provider-identity
    idempotent; it will report the already-created source as existing.
    """
    normalized_provider = provider.strip().lower()
    if not normalized_provider:
        raise ValueError("provider is required")
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be positive")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    provider_registry = providers or default_job_source_providers()
    adapter = provider_registry.adapter_for(normalized_provider)
    registry = JobSourceRegistry(engine, providers=provider_registry)
    scope = {
        "provider": normalized_provider,
        "adapter_version": adapter.adapter_version,
        "source_kind": adapter.source_kind,
    }

    with url_inventory_writer_lock(engine):
        checkpoint = _load_checkpoint(checkpoint_file)
        if checkpoint is None:
            checkpoint = _build_checkpoint(
                engine,
                registry=registry,
                provider=normalized_provider,
                scope=scope,
            )
            _publish_checkpoint(checkpoint_file, checkpoint, progress)
        else:
            _validate_checkpoint(checkpoint, scope=scope, registry=registry)

        since_publish = 0
        for key in checkpoint["candidate_keys"]:
            candidate = checkpoint["candidates"][key]
            state = str(candidate.get("state") or "")
            attempts = int(candidate.get("attempts") or 0)
            if state in TERMINAL_STATES:
                continue
            if state not in RETRYABLE_STATES:
                raise ValueError(f"unsupported candidate state {state!r} for {key}")
            if attempts >= max_attempts:
                candidate["state"] = "exhausted"
                candidate["error"] = {
                    "class": "AttemptBudgetExhausted",
                    "message": f"registration did not succeed after {attempts} attempts",
                }
                since_publish += 1
                if since_publish >= checkpoint_every:
                    _publish_checkpoint(checkpoint_file, checkpoint, progress)
                    since_publish = 0
                continue

            candidate["attempts"] = attempts + 1
            candidate["state"] = "running"
            candidate.pop("error", None)
            # Persist intent before the database transaction so a killed process
            # cannot make the remaining work appear untouched.
            _publish_checkpoint(checkpoint_file, checkpoint, progress)
            try:
                result = registry.register_url(
                    company_id=int(candidate["company_id"]),
                    source_url=str(candidate["observed_urls"][0]),
                    provider=normalized_provider,
                    discovered_from_url=str(candidate["observed_urls"][0]),
                    evidence={
                        "registration": "checkpointed_career_page_discovery",
                        "candidate_key": key,
                        "observed_urls": candidate["observed_urls"],
                    },
                )
            except ValueError as exc:
                # Identity conflicts and invalid evidence are terminal for this
                # frozen batch.  Nothing is guessed or reassigned.
                candidate["state"] = "conflict"
                candidate["error"] = _error_payload(exc)
            except Exception as exc:
                candidate["state"] = (
                    "exhausted"
                    if int(candidate["attempts"]) >= max_attempts
                    else "failed"
                )
                candidate["error"] = _error_payload(exc)
                _publish_checkpoint(checkpoint_file, checkpoint, progress)
                if candidate["state"] != "exhausted":
                    raise
            else:
                if (
                    result.provider != normalized_provider
                    or result.external_source_id != candidate["external_source_id"]
                    or result.company_id != int(candidate["company_id"])
                ):
                    raise RuntimeError(f"registration result changed candidate identity: {key}")
                candidate["state"] = "registered" if result.created else "existing"
                candidate["career_source_id"] = result.career_source_id
                candidate.pop("error", None)
            since_publish += 1
            if since_publish >= checkpoint_every:
                _publish_checkpoint(checkpoint_file, checkpoint, progress)
                since_publish = 0

        _publish_checkpoint(checkpoint_file, checkpoint, progress)
        return summarize_checkpoint(checkpoint)


def summarize_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    candidates = list(checkpoint.get("candidates", {}).values())
    states = [str(candidate.get("state") or "") for candidate in candidates]
    conflicts = [
        {
            "candidate_key": candidate["candidate_key"],
            "company_id": candidate["company_id"],
            "provider": candidate["provider"],
            "external_source_id": candidate["external_source_id"],
            "error": candidate.get("error") or {},
        }
        for candidate in candidates
        if candidate.get("state") in {"conflict", "exhausted"}
    ]
    return {
        "provider": checkpoint["scope"]["provider"],
        "selected": len(candidates),
        "processed": sum(state in TERMINAL_STATES for state in states),
        "registered": states.count("registered"),
        "existing": states.count("existing"),
        "pending": sum(state in RETRYABLE_STATES for state in states),
        "skipped": int(checkpoint.get("skipped_rows") or 0),
        "conflicts": conflicts,
        "observed_rows": int(checkpoint.get("observed_rows") or 0),
        "inventory_sha256": checkpoint["inventory_sha256"],
    }


def _build_checkpoint(
    engine: Engine,
    *,
    registry: JobSourceRegistry,
    provider: str,
    scope: dict[str, str],
) -> dict[str, Any]:
    candidates: dict[str, dict[str, Any]] = {}
    owners: dict[tuple[str, str], set[int]] = {}
    skipped = 0
    rows = fetch_company_career_page_rows(engine)
    for page in rows:
        company_id = page.get("company_id")
        observed_url = str(page.get("career_page_url") or "")
        if company_id is None:
            skipped += 1
            continue
        detected = registry.providers.detect(observed_url, provider=provider)
        if detected is None:
            skipped += 1
            continue
        numeric_company_id = int(company_id)
        key = _candidate_key(
            company_id=numeric_company_id,
            provider=detected.provider,
            external_source_id=detected.external_source_id,
        )
        candidate = candidates.setdefault(
            key,
            {
                "candidate_key": key,
                "company_id": numeric_company_id,
                "provider": detected.provider,
                "external_source_id": detected.external_source_id,
                "canonical_source_url": detected.canonical_url,
                "observed_urls": [],
                "ownership_company_ids": [],
                "attempts": 0,
                "state": "pending",
            },
        )
        candidate["observed_urls"].append(observed_url)
        owners.setdefault(
            (detected.provider, detected.external_source_id), set()
        ).add(numeric_company_id)

    for candidate in candidates.values():
        candidate["observed_urls"] = sorted(set(candidate["observed_urls"]))
        company_ids = sorted(
            owners[(candidate["provider"], candidate["external_source_id"])]
        )
        candidate["ownership_company_ids"] = company_ids
        if len(company_ids) > 1:
            candidate["state"] = "conflict"
            candidate["error"] = {
                "class": "AmbiguousCompanyOwnership",
                "message": (
                    f"{candidate['provider']} source {candidate['external_source_id']} "
                    f"was observed for company_ids={company_ids}"
                ),
            }

    candidate_keys = sorted(
        candidates,
        key=lambda key: (
            str(candidates[key]["provider"]),
            str(candidates[key]["external_source_id"]).casefold(),
            str(candidates[key]["external_source_id"]),
            int(candidates[key]["company_id"]),
        ),
    )
    checkpoint: dict[str, Any] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "scope": scope,
        "observed_rows": len(rows),
        "skipped_rows": skipped,
        "candidate_keys": candidate_keys,
        "candidates": candidates,
    }
    checkpoint["inventory_sha256"] = _inventory_digest(checkpoint)
    return checkpoint


def _load_checkpoint(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    checkpoint = read_status(path)
    if checkpoint is None:
        raise ValueError(f"source-discovery checkpoint is unreadable: {path}")
    return checkpoint


def _validate_checkpoint(
    checkpoint: dict[str, Any],
    *,
    scope: dict[str, str],
    registry: JobSourceRegistry,
) -> None:
    if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("source-discovery checkpoint schema is unsupported")
    if checkpoint.get("scope") != scope:
        raise ValueError("source-discovery checkpoint scope does not match this command")
    candidate_keys = checkpoint.get("candidate_keys")
    candidates = checkpoint.get("candidates")
    if (
        not isinstance(candidate_keys, list)
        or not isinstance(candidates, dict)
        or len(candidate_keys) != len(set(candidate_keys))
        or set(candidate_keys) != set(candidates)
    ):
        raise ValueError("source-discovery checkpoint candidate inventory is malformed")
    if checkpoint.get("inventory_sha256") != _inventory_digest(checkpoint):
        raise ValueError("source-discovery checkpoint candidate inventory was modified")

    ownership: dict[tuple[str, str], set[int]] = {}
    for key in candidate_keys:
        candidate = candidates[key]
        if not isinstance(candidate, dict) or candidate.get("candidate_key") != key:
            raise ValueError(f"source-discovery checkpoint candidate is malformed: {key}")
        if candidate.get("provider") != scope["provider"]:
            raise ValueError(f"source-discovery checkpoint provider changed: {key}")
        expected_key = _candidate_key(
            company_id=int(candidate["company_id"]),
            provider=str(candidate["provider"]),
            external_source_id=str(candidate["external_source_id"]),
        )
        if expected_key != key:
            raise ValueError(f"source-discovery checkpoint candidate identity changed: {key}")
        observed_urls = candidate.get("observed_urls")
        if not isinstance(observed_urls, list) or not observed_urls:
            raise ValueError(f"source-discovery checkpoint has no observed URL: {key}")
        for observed_url in observed_urls:
            detected = registry.providers.detect(str(observed_url), provider=scope["provider"])
            if (
                detected is None
                or detected.provider != candidate["provider"]
                or detected.external_source_id != candidate["external_source_id"]
                or detected.canonical_url != candidate["canonical_source_url"]
            ):
                raise ValueError(f"provider detection no longer matches checkpoint: {key}")
        ownership.setdefault(
            (str(candidate["provider"]), str(candidate["external_source_id"])), set()
        ).add(int(candidate["company_id"]))
        state = str(candidate.get("state") or "")
        if state not in TERMINAL_STATES | RETRYABLE_STATES:
            raise ValueError(f"source-discovery checkpoint state is invalid: {key}")
        attempts = int(candidate.get("attempts") or 0)
        if attempts < 0:
            raise ValueError(f"source-discovery checkpoint attempts are invalid: {key}")

    for identity, company_ids in ownership.items():
        expected = sorted(company_ids)
        for candidate in candidates.values():
            if (candidate["provider"], candidate["external_source_id"]) != identity:
                continue
            if candidate.get("ownership_company_ids") != expected:
                raise ValueError(
                    "source-discovery checkpoint ownership evidence is inconsistent"
                )


def _publish_checkpoint(
    path: Path,
    checkpoint: dict[str, Any],
    progress: ProgressCallback | None,
) -> None:
    write_status(path, checkpoint)
    if progress is not None:
        progress(checkpoint)


def _candidate_key(*, company_id: int, provider: str, external_source_id: str) -> str:
    value = json.dumps(
        [company_id, provider, external_source_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _inventory_digest(checkpoint: dict[str, Any]) -> str:
    candidates = checkpoint.get("candidates") or {}
    immutable = {
        "scope": checkpoint.get("scope"),
        "observed_rows": checkpoint.get("observed_rows"),
        "skipped_rows": checkpoint.get("skipped_rows"),
        "candidate_keys": checkpoint.get("candidate_keys"),
        "candidates": [
            {
                "candidate_key": candidates[key].get("candidate_key"),
                "company_id": candidates[key].get("company_id"),
                "provider": candidates[key].get("provider"),
                "external_source_id": candidates[key].get("external_source_id"),
                "canonical_source_url": candidates[key].get("canonical_source_url"),
                "observed_urls": candidates[key].get("observed_urls"),
                "ownership_company_ids": candidates[key].get("ownership_company_ids"),
            }
            for key in checkpoint.get("candidate_keys") or []
        ],
    }
    encoded = json.dumps(
        immutable,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error_payload(exc: BaseException) -> dict[str, str]:
    return {"class": type(exc).__name__, "message": str(exc)}
