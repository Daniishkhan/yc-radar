"""Deterministic metrics for application, verification, and outreach queues."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from yc_radar.services.application_artifacts import canonical_queue_name
from yc_radar.services.application_url_validation import select_application_url


def build_application_pool_metrics(
    queues: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    validations: Sequence[Mapping[str, Any]] = (),
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Summarize queue composition and URL evidence without changing queue decisions."""
    normalized_queues = {
        canonical_queue_name(name): list(rows)
        for name, rows in queues.items()
    }
    normalized_validations = [dict(row) for row in validations]
    validations_by_queue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in normalized_validations:
        raw_queue = str(row.get("queue") or "unknown")
        try:
            queue_name = canonical_queue_name(raw_queue)
        except ValueError:
            queue_name = raw_queue
        validations_by_queue[queue_name].append(row)

    queue_metrics = {
        name: _queue_metrics(rows, validations_by_queue.get(name, ()))
        for name, rows in normalized_queues.items()
    }
    total_rows = sum(len(rows) for rows in normalized_queues.values())
    provider_rows: dict[str, Counter[str]] = defaultdict(Counter)
    for queue_name, rows in normalized_queues.items():
        for row in rows:
            provider_rows[_provider(row)][queue_name] += 1

    provider_validation_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in normalized_validations:
        provider_validation_rows[str(row.get("provider") or "unknown")].append(row)

    provider_names = sorted(set(provider_rows) | set(provider_validation_rows))
    provider_contribution = {}
    for provider in provider_names:
        by_queue = provider_rows.get(provider, Counter())
        row_count = sum(by_queue.values())
        provider_contribution[provider] = {
            "selected_row_count": row_count,
            "share_of_selected_rows": (
                round(row_count / total_rows, 6) if total_rows else None
            ),
            "by_queue": dict(sorted(by_queue.items())),
            "url_validation": _validation_metrics(
                provider_validation_rows.get(provider, ())
            ),
        }

    overall_validation = _validation_metrics(normalized_validations)
    return {
        "schema_version": 1,
        "generated_at": _as_utc(generated_at or datetime.now(UTC)).isoformat(),
        "queue_count": len(normalized_queues),
        "selected_row_count": total_rows,
        "queues": queue_metrics,
        "provider_contribution": provider_contribution,
        "url_validation": overall_validation,
    }


def _queue_metrics(
    rows: Sequence[Mapping[str, Any]],
    validations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    companies = {
        identity
        for row in rows
        if (
            identity := _first_text(
                row,
                "company_slug",
                "company_name",
                "slug",
                "name",
            )
        )
        is not None
    }
    direct_url_count = sum(
        bool(_first_text(row, "application_url", "apply_url"))
        for row in rows
    )
    selected_url_count = sum(select_application_url(row) is not None for row in rows)
    application_statuses = _distribution(rows, "application_status", include_unknown=False)
    return {
        "row_count": len(rows),
        "company_count": len(companies),
        "provider_distribution": _distribution(rows, "provider"),
        "role_match_distribution": _distribution(rows, "role_match_status"),
        "role_family_distribution": _distribution(rows, "role_family"),
        "remote_eligibility_distribution": _first_field_distribution(
            rows,
            "remote_eligibility_status",
            "remote_eligibility",
            "geographic_eligibility",
        ),
        "freshness_distribution": _first_field_distribution(
            rows,
            "freshness_bucket",
            "freshness_band",
        ),
        "direct_application_url_count": direct_url_count,
        "selected_url_count": selected_url_count,
        "missing_url_count": len(rows) - selected_url_count,
        "direct_application_url_coverage": (
            round(direct_url_count / len(rows), 6) if rows else None
        ),
        "selected_url_coverage": (
            round(selected_url_count / len(rows), 6) if rows else None
        ),
        "application_status_distribution": application_statuses,
        "url_validation": _validation_metrics(validations, expected_rows=len(rows)),
    }


def _validation_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_rows: int | None = None,
) -> dict[str, Any]:
    outcomes = Counter(str(row.get("outcome") or "unknown") for row in rows)
    dead_denominator = outcomes["live"] + outcomes["dead"]
    metrics: dict[str, Any] = {
        "validation_row_count": len(rows),
        "outcome_distribution": dict(sorted(outcomes.items())),
        "live_link_count": outcomes["live"],
        "dead_link_count": outcomes["dead"],
        "blocked_link_count": outcomes["blocked"],
        "transient_error_count": outcomes["transient_error"],
        "invalid_link_count": outcomes["invalid"],
        "dead_link_rate_denominator": dead_denominator,
        "dead_link_rate": (
            round(outcomes["dead"] / dead_denominator, 6)
            if dead_denominator
            else None
        ),
    }
    if expected_rows is not None:
        metrics["expected_queue_row_count"] = expected_rows
        metrics["validation_coverage"] = (
            round(len(rows) / expected_rows, 6) if expected_rows else None
        )
        metrics["unvalidated_row_count"] = max(0, expected_rows - len(rows))
    return metrics


def _distribution(
    rows: Sequence[Mapping[str, Any]],
    field: str,
    *,
    include_unknown: bool = True,
) -> dict[str, int]:
    values = Counter()
    for row in rows:
        value = _first_text(row, field)
        if value is not None:
            values[value] += 1
        elif include_unknown:
            values["unknown"] += 1
    return dict(sorted(values.items()))


def _first_field_distribution(
    rows: Sequence[Mapping[str, Any]],
    *fields: str,
) -> dict[str, int]:
    values = Counter(_first_text(row, *fields) or "unknown" for row in rows)
    return dict(sorted(values.items()))


def _provider(row: Mapping[str, Any]) -> str:
    return _first_text(row, "provider") or "unknown"


def _first_text(row: Mapping[str, Any], *fields: str) -> str | None:
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
