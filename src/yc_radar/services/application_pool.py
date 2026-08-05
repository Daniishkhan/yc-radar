from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict


ApplicationLane = Literal["application", "verification", "excluded"]

APPLICATION_REMOTE_STATUSES = frozenset({"pakistan_explicit", "global_explicit"})
VERIFICATION_REMOTE_STATUSES = frozenset(
    {"regional_unconfirmed", "remote_unclear"}
)
TARGET_ROLE_STATUSES = frozenset({"strong", "possible"})


class ApplicationPool(TypedDict):
    application_queue: list[dict[str, Any]]
    verification_queue: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    summary: dict[str, Any]


def build_application_pool(
    rows: Sequence[dict[str, Any]],
    *,
    as_of: datetime | None = None,
    application_max_age_days: int | None = 90,
    limit_per_queue: int | None = None,
) -> ApplicationPool:
    """Split classified job rows into honest, independently useful queues.

    Complete snapshots and observation feeds retain their distinct lifecycle confidence.
    Observation rows can still become application candidates when their positive evidence is
    explicit, fresh, and linked to a public job page; this function never turns observation
    evidence into complete-snapshot absence authority.
    """
    if application_max_age_days is not None and application_max_age_days < 0:
        raise ValueError("application_max_age_days must be non-negative or None")
    if limit_per_queue is not None and limit_per_queue < 0:
        raise ValueError("limit_per_queue must be non-negative or None")

    reference_time = _aware_utc(as_of or datetime.now(UTC))
    queues: dict[ApplicationLane, list[dict[str, Any]]] = {
        "application": [],
        "verification": [],
        "excluded": [],
    }
    for source_row in rows:
        row = dict(source_row)
        age_days = opportunity_age_days(row, as_of=reference_time)
        lane, reason = classify_application_lane(
            row,
            age_days=age_days,
            application_max_age_days=application_max_age_days,
        )
        row["application_lane"] = lane
        row["application_lane_reason"] = reason
        row["application_url"] = preferred_application_url(row)
        row["freshness_age_days"] = age_days
        row["freshness_band"] = freshness_band(age_days)
        row["priority_score"] = opportunity_priority_score(row, age_days=age_days)
        queues[lane].append(row)

    for lane in queues:
        queues[lane].sort(key=_ranking_key)

    application_queue = _bounded(queues["application"], limit_per_queue)
    verification_queue = _bounded(queues["verification"], limit_per_queue)
    excluded = queues["excluded"]
    summary = application_pool_summary(
        rows=rows,
        application_candidates=queues["application"],
        verification_candidates=queues["verification"],
        application_queue=application_queue,
        verification_queue=verification_queue,
        excluded=excluded,
        as_of=reference_time,
        application_max_age_days=application_max_age_days,
    )
    return {
        "application_queue": application_queue,
        "verification_queue": verification_queue,
        "excluded": excluded,
        "summary": summary,
    }


def classify_application_lane(
    row: dict[str, Any],
    *,
    age_days: int | None,
    application_max_age_days: int | None,
) -> tuple[ApplicationLane, str]:
    if str(row.get("status") or "active") != "active":
        return "excluded", "job is not active"
    role_status = str(row.get("role_match_status") or "")
    if role_status not in TARGET_ROLE_STATUSES:
        return "excluded", "role is outside the strong or possible target lanes"
    if not preferred_application_url(row):
        return "excluded", "no public posting or application URL is available"
    if (
        application_max_age_days is not None
        and age_days is not None
        and age_days > application_max_age_days
    ):
        return "excluded", "posting is older than the application freshness window"

    remote_status = str(row.get("remote_eligibility_status") or "")
    if remote_status in APPLICATION_REMOTE_STATUSES:
        return "application", "role has explicit Pakistan or worldwide remote evidence"
    if remote_status in VERIFICATION_REMOTE_STATUSES:
        return "verification", "role is remote but geographic eligibility needs verification"
    if remote_status == "restricted_remote":
        return "excluded", "remote eligibility is explicitly restricted"
    if remote_status == "onsite_explicit":
        return "excluded", "role explicitly requires onsite or hybrid work"
    return "excluded", "role lacks usable remote eligibility evidence"


def opportunity_priority_score(row: dict[str, Any], *, age_days: int | None) -> int:
    score = {
        "strong": 60,
        "possible": 35,
        "weak": 5,
        "exclude": -60,
    }.get(str(row.get("role_match_status") or ""), 0)
    score += {
        "pakistan_explicit": 52,
        "global_explicit": 48,
        "regional_unconfirmed": 16,
        "remote_unclear": 10,
        "restricted_remote": -55,
        "onsite_explicit": -65,
        "no_remote_evidence": -35,
    }.get(str(row.get("remote_eligibility_status") or ""), 0)
    score += 10 if row.get("lifecycle_managed") is True else 0
    score += 10 if row.get("apply_url") else 5 if row.get("posting_url") else -20
    if age_days is None:
        score -= 5
    elif age_days <= 7:
        score += 25
    elif age_days <= 30:
        score += 15
    elif age_days <= 90:
        score += 5
    elif age_days > 180:
        score -= 15
    return score


def opportunity_age_days(
    row: dict[str, Any],
    *,
    as_of: datetime,
) -> int | None:
    timestamp = next(
        (
            parsed
            for field in (
                "source_published_at",
                "source_updated_at",
                "observed_at",
                "last_changed_at",
            )
            if (parsed := _parse_timestamp(row.get(field))) is not None
        ),
        None,
    )
    if timestamp is None:
        return None
    seconds = max((_aware_utc(as_of) - timestamp).total_seconds(), 0)
    return int(seconds // 86_400)


def freshness_band(age_days: int | None) -> str:
    if age_days is None:
        return "unknown"
    if age_days <= 7:
        return "0_7_days"
    if age_days <= 30:
        return "8_30_days"
    if age_days <= 90:
        return "31_90_days"
    if age_days <= 180:
        return "91_180_days"
    return "over_180_days"


def preferred_application_url(row: dict[str, Any]) -> str | None:
    for field in ("apply_url", "posting_url"):
        value = str(row.get(field) or "").strip()
        if value.startswith(("https://", "http://")):
            return value
    return None


def application_pool_summary(
    *,
    rows: Sequence[dict[str, Any]],
    application_candidates: Sequence[dict[str, Any]],
    verification_candidates: Sequence[dict[str, Any]],
    application_queue: Sequence[dict[str, Any]],
    verification_queue: Sequence[dict[str, Any]],
    excluded: Sequence[dict[str, Any]],
    as_of: datetime,
    application_max_age_days: int | None,
) -> dict[str, Any]:
    selected = [*application_queue, *verification_queue]
    return {
        "generated_at": as_of.isoformat(),
        "application_max_age_days": application_max_age_days,
        "inventory_count": len(rows),
        "application_candidate_count": len(application_candidates),
        "verification_candidate_count": len(verification_candidates),
        "application_queue_count": len(application_queue),
        "verification_queue_count": len(verification_queue),
        "excluded_count": len(excluded),
        "provider_contribution": _counts(selected, "provider"),
        "application_provider_contribution": _counts(application_queue, "provider"),
        "verification_provider_contribution": _counts(verification_queue, "provider"),
        "remote_status_distribution": _counts(rows, "remote_eligibility_status"),
        "role_status_distribution": _counts(rows, "role_match_status"),
        "freshness_distribution": _counts(selected, "freshness_band"),
        "status_confidence_distribution": _counts(selected, "status_confidence"),
        "exclusion_reason_distribution": _counts(excluded, "application_lane_reason"),
    }


def _counts(rows: Iterable[dict[str, Any]], field: str) -> dict[str, int]:
    values = Counter(str(row.get(field) or "unknown") for row in rows)
    return dict(sorted(values.items()))


def _ranking_key(row: dict[str, Any]) -> tuple[int, int, str, str]:
    age = row.get("freshness_age_days")
    normalized_age = int(age) if isinstance(age, int) else 1_000_000
    return (
        -int(row.get("priority_score") or 0),
        normalized_age,
        str(row.get("company_name") or "").casefold(),
        str(row.get("title") or "").casefold(),
    )


def _bounded(
    rows: list[dict[str, Any]],
    limit: int | None,
) -> list[dict[str, Any]]:
    return list(rows if limit is None else rows[:limit])


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware_utc(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _aware_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
