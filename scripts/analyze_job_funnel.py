#!/usr/bin/env python3
"""Produce a read-only canonical-job funnel report and tiered remote-role queues.

The command deliberately separates remote work arrangement from applicant geography.  The strict
actionable CSV only contains matching role clusters whose public evidence explicitly names
Pakistan or an unrestricted global scope.  A second CSV broadens the role lane to full-stack and
software-engineering titles while keeping unclear country or regional eligibility in verification
tiers.  Those labels are evidence summaries, not work-authorization or visa conclusions.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection

from yc_radar.core.config import get_settings
from yc_radar.services.candidate_fit import (
    REMOTE_ELIGIBILITY_ORDER,
    classify_remote_eligibility,
    classify_role_text,
)
from yc_radar.services.database import (
    career_sources_table,
    companies_table,
    engine_from_url,
    job_posting_versions_table,
    job_postings_table,
    source_sync_runs_table,
)


SCHEMA_VERSION = 1
ACTIONABLE_REMOTE_STATUSES = frozenset({"pakistan_explicit", "global_explicit"})
REMOTE_LEAD_STATUSES = frozenset(
    {
        "pakistan_explicit",
        "global_explicit",
        "remote_unclear",
        "regional_unconfirmed",
    }
)
LEAD_TIER_BY_REMOTE_STATUS = {
    "pakistan_explicit": "confirmed",
    "global_explicit": "confirmed",
    "remote_unclear": "verify_country",
    "regional_unconfirmed": "verify_region",
}
GEOGRAPHIC_ELIGIBILITY_BY_REMOTE_STATUS = {
    "pakistan_explicit": "pakistan",
    "global_explicit": "global",
    "remote_unclear": "unknown",
    "regional_unconfirmed": "regional_unconfirmed",
}
LEAD_TIER_ORDER = {"confirmed": 0, "verify_country": 1, "verify_region": 2}
ROLE_SCOPE_ORDER = {"primary_target": 0, "expanded_fullstack_software": 1}
ROLE_STATUS_ORDER = {"strong": 0, "possible": 1, "weak": 2, "exclude": 3}
ROLE_TITLE_PREFILTER_PATTERN = (
    r"engineer|developer|architect|site[[:space:]-]+reliability|"
    r"(^|[^a-z])sre([^a-z]|$)|(^|[^a-z])sde([^a-z]|$)|devops|"
    r"member[[:space:]]+of[[:space:]]+technical[[:space:]]+staff"
)
REMOTE_ELIGIBILITY_CAVEAT = (
    "Public posting evidence only; verify employer-of-record availability, work authorization, "
    "visa, tax, timezone, and current country restrictions before applying."
)
LEAD_REVIEW_NOTES = {
    "confirmed": (
        "Explicit Pakistan or worldwide remote evidence; verify current legal engagement "
        "eligibility before applying."
    ),
    "verify_country": (
        "Remote role with no explicit country list; verify Pakistan eligibility before applying."
    ),
    "verify_region": (
        "Regional or timezone evidence only; verify Pakistan eligibility before applying."
    ),
}
EXPANDED_TITLE_PATTERN = re.compile(
    r"(?:\bfull ?stack\b.*\b(?:engineer|developer)\b|"
    r"\b(?:engineer|developer)\b.*\bfull ?stack\b|"
    r"\bsoftware (?:(?:development|systems?|security|devops|platform|cloud|"
    r"infrastructure) )?(?:engineer|developer)\b|\bsde\b)",
    flags=re.IGNORECASE,
)
ACTIVE_CLEARANCE_LEVEL_PATTERN = (
    r"(?:secret|top\s+secret|ts\s*[/\-]\s*sci|top\s+secret\s*[/\-]\s*sci|"
    r"dod(?:\s+security)?)"
)
ACTIVE_CLEARANCE_PHRASE_PATTERN = re.compile(
    rf"\bactive\b[^.!?;\r\n]{{0,80}}\b{ACTIVE_CLEARANCE_LEVEL_PATTERN}\b"
    rf"[^.!?;\r\n]{{0,60}}\bclearance\b|"
    rf"\bactive\b[^.!?;\r\n]{{0,80}}\bclearance\b"
    rf"[^.!?;\r\n]{{0,60}}\b{ACTIVE_CLEARANCE_LEVEL_PATTERN}\b|"
    r"\bactive\b[^.!?;\r\n]{0,30}\b(?:ts\s*[/\-]\s*sci|"
    r"top\s+secret\s*[/\-]\s*sci)\b|"
    rf"\b{ACTIVE_CLEARANCE_LEVEL_PATTERN}\b[^.!?;\r\n]{{0,60}}\bclearance\b|"
    rf"\bclearance\b[^.!?;\r\n]{{0,60}}\b{ACTIVE_CLEARANCE_LEVEL_PATTERN}\b",
    flags=re.IGNORECASE,
)
LABELED_REQUIRED_CLEARANCE_PATTERN = re.compile(
    rf"\binvestigative\s+requirements?\b[^.!?;\r\n]{{0,100}}"
    rf"\b{ACTIVE_CLEARANCE_LEVEL_PATTERN}\b"
    rf"(?:[^.!?;\r\n]{{0,30}}\bclearance\b)?",
    flags=re.IGNORECASE,
)
CLEARANCE_CLAUSE_BOUNDARY_PATTERN = re.compile(
    r"(?:[.!?;\r\n]+|</?(?:br|div|li|p)\b[^>]*>|[•●▪])",
    flags=re.IGNORECASE,
)
CLEARANCE_OPTIONAL_BEFORE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:ability|able|eligible|willing)\s+to\s+(?:obtain|acquire)"
    r"(?:\s+and\s+maintain)?\b[^.!?;\r\n]{0,60}|"
    r"\b(?:must|need(?:s|ed)?\s+to|required\s+to)\s+"
    r"(?:be\s+able\s+to\s+)?(?:obtain|acquire)\b[^.!?;\r\n]{0,60}|"
    r"\b(?:do|does)\s+not\s+require\b[^.!?;\r\n]{0,60}|"
    r"\b(?:preferred|optional|desirable|nice[- ]to[- ]have|prefers?|"
    r"preference\s+for)\b[^.!?;\r\n]{0,60}|"
    r"\bno\s+(?:(?:current|currently)\s+)?|"
    r"\bwithout\s+(?:requiring\s+)?(?:an?\s+)?"
    r")$",
    flags=re.IGNORECASE,
)
CLEARANCE_OPTIONAL_AFTER_PATTERN = re.compile(
    r"\b(?:not\s+(?:required|mandatory)|preferred|optional|desirable|"
    r"nice[- ]to[- ]have|a\s+plus)\b",
    flags=re.IGNORECASE,
)
CLEARANCE_OBTAIN_ALTERNATIVE_PATTERN = re.compile(
    r"\bor\s+(?:(?:have|with)\s+)?(?:the\s+)?(?:ability\s+to|(?:be\s+)?able\s+to|"
    r"(?:be\s+)?eligible\s+to|willing\s+to)?\s*(?:obtain|acquire)\b",
    flags=re.IGNORECASE,
)
CLEARANCE_REQUIRED_BEFORE_PATTERN = re.compile(
    r"(?:"
    r"\b(?:must|shall)\s+(?:(?:already|currently)\s+)?"
    r"(?:possess|have|hold|maintain)\b|"
    r"\b(?:is|are)\s+required\s+to\s+(?:possess|have|hold|maintain)\b|"
    r"\brequires?\b|"
    r"\b(?:minimum|required)\s+qualifications?\b|"
    r"\brequirements?\s*:"
    r")[^.!?;\r\n]{0,80}$",
    flags=re.IGNORECASE,
)
CLEARANCE_REQUIRED_AFTER_PATTERN = re.compile(
    r"^\s*(?:[,(:\-]\s*)*(?:"
    r"(?:is\s+|are\s+)?(?:required|mandatory)\b|"
    r"(?:is\s+|are\s+)?(?:a\s+)?(?:minimum\s+)?requirement\b|"
    r"must\s+be\s+(?:possessed|held|maintained|current)\b"
    r")",
    flags=re.IGNORECASE,
)
CLEARANCE_REQUIRED_HTML_SECTION_PATTERN = re.compile(
    r"\b(?:required|minimum)\s+qualifications?\s*:\s*"
    r"(?:</?(?:b|strong)\b[^>]*>\s*)*</(?:div|h[1-6]|p)>\s*"
    r"(?:<(?:ol|ul)\b[^>]*>\s*)?"
    r"(?:<li\b[^>]*>.*?</li>\s*)*<li\b[^>]*>\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)
HISTORY_ARTIFACTS = {
    "orchestrator": "history-backfill.status.json",
    "union": "greenhouse-candidate-union.manifest.json",
    "scout": "greenhouse-scout.status.json",
    "resolver": "greenhouse-domain-resolver.status.json",
    "sync": "greenhouse-sync.status.json",
    "sync_checkpoint": "greenhouse-sync.checkpoint.json",
}
HISTORY_SCALAR_KEYS = (
    "schema_version",
    "stage",
    "state",
    "selected",
    "processed",
    "succeeded",
    "failed",
    "retryable",
    "terminal_failures",
    "exhausted_failures",
    "eligible",
    "accepted",
    "ambiguous",
    "manual_review",
    "unresolved",
    "registration_failed",
    "registration_conflicts",
    "registered",
    "existing",
    "skipped",
    "conflicts",
    "resumed",
    "network_requests",
    "cache_hits",
    "request_attempt_count",
    "search_query_count",
    "prompt_token_count",
    "candidates_token_count",
    "total_token_count",
    "union_token_count",
    "evidence_row_count",
    "total_observation_count",
    "attempt_count",
    "failed_stage",
    "paused_stage",
    "return_code",
    "started_at",
    "finished_at",
    "updated_at",
)
ACTIONABLE_CSV_FIELDS = (
    "company_name",
    "company_slug",
    "normalized_title",
    "representative_title",
    "role_match_status",
    "best_remote_eligibility",
    "posting_variant_count",
    "missing_location_variant_count",
    "locations",
    "role_status_distribution",
    "remote_eligibility_distribution",
    "provider_distribution",
    "provider",
    "external_job_id",
    "posting_url",
    "apply_url",
    "best_location",
    "department",
    "source_published_at",
    "source_updated_at",
    "remote_reasons",
    "remote_evidence",
    "eligibility_caveat",
)
REMOTE_LEADS_CSV_FIELDS = (
    "lead_tier",
    "role_scope",
    "work_arrangement",
    "geographic_eligibility",
    "company_name",
    "company_slug",
    "normalized_title",
    "representative_title",
    "role_match_status",
    "best_remote_eligibility",
    "posting_variant_count",
    "missing_location_variant_count",
    "locations",
    "role_status_distribution",
    "remote_eligibility_distribution",
    "provider_distribution",
    "provider",
    "external_job_id",
    "posting_url",
    "apply_url",
    "best_location",
    "department",
    "source_published_at",
    "source_updated_at",
    "review_note",
    "review_evidence",
    "remote_reasons",
    "remote_evidence",
    "eligibility_caveat",
)
REPORT_ARTIFACT_FILENAMES = (
    "job_funnel_report.json",
    "actionable_job_clusters.csv",
    "remote_role_leads.csv",
)


@dataclass(frozen=True)
class RoleAnalysis:
    matching_clusters: list[dict[str, Any]]
    role_summary: dict[str, Any]
    actionable_clusters: list[dict[str, Any]]
    remote_leads: list[dict[str, Any]]
    remote_leads_summary: dict[str, Any]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description=(
            "Measure the canonical job funnel and export a strict Pakistan/global queue plus "
            "tiered remote full-stack/software-engineering leads without modifying Postgres."
        )
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="Postgres URL; defaults to DATABASE_URL/DEFAULT_DATABASE_URL.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.runs_dir / f"job-funnel-{date.today().isoformat()}",
    )
    parser.add_argument(
        "--as-of",
        type=parse_as_of,
        default=datetime.now(UTC),
        help="ISO-8601 timestamp used for age buckets; defaults to the current UTC time.",
    )
    parser.add_argument(
        "--history-run-dir",
        type=Path,
        help="Optional Greenhouse backfill run directory whose JSON artifacts are summarized.",
    )
    parser.add_argument("--top-boards", type=positive_int, default=25)
    return parser.parse_args(argv)


def parse_as_of(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 date or timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#]+", " ", title.casefold())).strip()


def age_bucket(value: datetime | None, *, as_of: datetime) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    age_days = (as_of - value.astimezone(UTC)).total_seconds() / 86_400
    if age_days < 0:
        return "future"
    if age_days <= 30:
        return "0_30_days"
    if age_days <= 90:
        return "31_90_days"
    if age_days <= 180:
        return "91_180_days"
    if age_days <= 365:
        return "181_365_days"
    return "over_365_days"


def nearest_rank(values: Sequence[int], quantile: float) -> int | None:
    if not values:
        return None
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def ordered_counts(
    counts: Mapping[str, int], order: Mapping[str, int] | Sequence[str] | None = None
) -> dict[str, int]:
    if order is None:
        keys = sorted(counts)
    elif isinstance(order, Mapping):
        keys = sorted(counts, key=lambda key: (order.get(key, 10_000), key))
    else:
        positions = {value: index for index, value in enumerate(order)}
        keys = sorted(counts, key=lambda key: (positions.get(key, 10_000), key))
    return {key: int(counts[key]) for key in keys}


def iso_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return value


def classification_context(row: Mapping[str, Any]) -> str:
    return " ".join(
        str(value)
        for key in ("location", "department", "employment_type", "description_text")
        if (value := row.get(key))
    )


def is_expanded_remote_lead_title(title: str) -> bool:
    """Return whether a weak role has an explicit software/full-stack title."""
    return EXPANDED_TITLE_PATTERN.search(normalize_title(title)) is not None


def _clearance_clause_sides(
    description: str, *, start: int, end: int
) -> tuple[str, str]:
    clause_start = 0
    for boundary in CLEARANCE_CLAUSE_BOUNDARY_PATTERN.finditer(description, 0, start):
        clause_start = boundary.end()
    next_boundary = CLEARANCE_CLAUSE_BOUNDARY_PATTERN.search(description, end)
    clause_end = next_boundary.start() if next_boundary else len(description)
    return description[clause_start:start], description[end:clause_end]


def requires_active_government_clearance(
    description: Any, *, title: Any = ""
) -> bool:
    """Detect a required pre-existing Secret/TS-SCI clearance in title or description."""
    for value in (title, description):
        if not isinstance(value, str) or not value.strip():
            continue
        if LABELED_REQUIRED_CLEARANCE_PATTERN.search(value):
            return True
        for match in ACTIVE_CLEARANCE_PHRASE_PATTERN.finditer(value):
            html_section_before = value[max(0, match.start() - 4_000) : match.start()]
            before, after = _clearance_clause_sides(
                value, start=match.start(), end=match.end()
            )
            if CLEARANCE_OPTIONAL_BEFORE_PATTERN.search(before):
                continue
            after_nearby = after[:160]
            if CLEARANCE_OBTAIN_ALTERNATIVE_PATTERN.search(after_nearby):
                continue
            required_after = CLEARANCE_REQUIRED_AFTER_PATTERN.search(after_nearby)
            optional_after = CLEARANCE_OPTIONAL_AFTER_PATTERN.search(after_nearby)
            if optional_after and (
                required_after is None or optional_after.start() < required_after.start()
            ):
                continue
            if (
                required_after
                or CLEARANCE_REQUIRED_BEFORE_PATTERN.search(before)
                or CLEARANCE_REQUIRED_HTML_SECTION_PATTERN.search(html_section_before)
            ):
                return True
    return False


def _variant_from_classifications(
    row: Mapping[str, Any],
    *,
    role: Any,
    remote: Any,
    requires_active_clearance: bool,
) -> dict[str, Any]:
    title = str(row.get("title") or "").strip()
    return {
        "job_posting_id": int(row["id"]),
        "company_id": int(row["company_id"]),
        "company_name": str(row.get("company_name") or ""),
        "company_slug": str(row.get("company_slug") or ""),
        "normalized_title": normalize_title(title),
        "title": title,
        "role_match_status": role.status,
        "role_match_reasons": list(role.reasons),
        "remote_eligibility": remote.status,
        "remote_reasons": list(remote.reasons),
        "remote_evidence": list(remote.evidence),
        "requires_active_clearance": requires_active_clearance,
        "provider": str(row.get("provider") or ""),
        "external_job_id": str(row.get("external_job_id") or ""),
        "posting_url": str(row.get("posting_url") or ""),
        "apply_url": str(row.get("apply_url") or ""),
        "location": str(row.get("location") or "").strip(),
        "department": str(row.get("department") or "").strip(),
        "source_published_at": iso_value(row.get("source_published_at")),
        "source_updated_at": iso_value(row.get("source_updated_at")),
    }


def analyzed_variant(
    row: Mapping[str, Any],
    *,
    role: Any | None = None,
) -> dict[str, Any] | None:
    title = str(row.get("title") or "").strip()
    if role is None:
        role = classify_role_text(title, classification_context(row))
    if role.status not in {"strong", "possible"}:
        return None
    remote = classify_remote_eligibility(dict(row))
    return _variant_from_classifications(
        row,
        role=role,
        remote=remote,
        requires_active_clearance=requires_active_government_clearance(
            row.get("description_text"), title=title
        ),
    )


def _timestamp_rank(value: Any) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def best_variant(variants: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not variants:
        raise ValueError("cannot select a representative from an empty cluster")
    return min(
        variants,
        key=lambda item: (
            REMOTE_ELIGIBILITY_ORDER.get(str(item["remote_eligibility"]), 10_000),
            ROLE_STATUS_ORDER.get(str(item["role_match_status"]), 10_000),
            -len(item.get("remote_evidence") or []),
            -int(bool(item.get("apply_url") or item.get("posting_url"))),
            -_timestamp_rank(item.get("source_published_at")),
            str(item.get("provider") or ""),
            str(item.get("external_job_id") or ""),
            int(item.get("job_posting_id") or 0),
        ),
    )


def _cluster_variants(
    grouped: Mapping[tuple[int, str], Sequence[dict[str, Any]]],
    *,
    remote_leads: bool = False,
) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for variants in grouped.values():
        representative = best_variant(variants)
        role_distribution = Counter(str(item["role_match_status"]) for item in variants)
        remote_distribution = Counter(str(item["remote_eligibility"]) for item in variants)
        provider_distribution = Counter(str(item["provider"]) for item in variants)
        locations = sorted({str(item["location"]) for item in variants if item.get("location")})
        cluster = {
            "company_id": int(representative["company_id"]),
            "company_name": str(representative["company_name"]),
            "company_slug": str(representative["company_slug"]),
            "normalized_title": str(representative["normalized_title"]),
            "representative_title": str(representative["title"]),
            "role_match_status": str(representative["role_match_status"]),
            "best_remote_eligibility": str(representative["remote_eligibility"]),
            "posting_variant_count": len(variants),
            "required_active_clearance_variant_count": sum(
                bool(item.get("requires_active_clearance")) for item in variants
            ),
            "missing_location_variant_count": sum(
                not bool(item.get("location")) for item in variants
            ),
            "locations": locations,
            "role_status_distribution": ordered_counts(
                role_distribution, ROLE_STATUS_ORDER
            ),
            "remote_eligibility_distribution": ordered_counts(
                remote_distribution, REMOTE_ELIGIBILITY_ORDER
            ),
            "provider_distribution": ordered_counts(provider_distribution),
            "best_variant": {
                key: value
                for key, value in representative.items()
                if key
                not in {
                    "company_id",
                    "company_name",
                    "company_slug",
                    "normalized_title",
                }
            },
        }
        if remote_leads:
            remote_status = str(representative["remote_eligibility"])
            lead_tier = LEAD_TIER_BY_REMOTE_STATUS[remote_status]
            role_scope = min(
                (str(item["role_scope"]) for item in variants),
                key=lambda value: ROLE_SCOPE_ORDER[value],
            )
            review_evidence = list(representative.get("remote_evidence") or [])
            if not review_evidence:
                review_evidence = list(representative.get("remote_reasons") or [])
            cluster.update(
                {
                    "lead_tier": lead_tier,
                    "role_scope": role_scope,
                    "work_arrangement": "remote",
                    "geographic_eligibility": (
                        GEOGRAPHIC_ELIGIBILITY_BY_REMOTE_STATUS[remote_status]
                    ),
                    "review_note": LEAD_REVIEW_NOTES[lead_tier],
                    "review_evidence": review_evidence,
                }
            )
        clusters.append(cluster)

    if remote_leads:
        clusters.sort(
            key=lambda item: (
                LEAD_TIER_ORDER[item["lead_tier"]],
                ROLE_SCOPE_ORDER[item["role_scope"]],
                REMOTE_ELIGIBILITY_ORDER.get(
                    item["best_remote_eligibility"], 10_000
                ),
                ROLE_STATUS_ORDER.get(item["role_match_status"], 10_000),
                item["company_name"].casefold(),
                item["normalized_title"],
            )
        )
    else:
        clusters.sort(
            key=lambda item: (
                REMOTE_ELIGIBILITY_ORDER.get(
                    item["best_remote_eligibility"], 10_000
                ),
                ROLE_STATUS_ORDER.get(item["role_match_status"], 10_000),
                item["company_name"].casefold(),
                item["normalized_title"],
            )
        )
    return clusters


def _analyze_role_rows(
    rows: Iterable[Mapping[str, Any]],
) -> RoleAnalysis:
    prefilter_statuses: Counter[str] = Counter()
    remote_statuses: Counter[str] = Counter()
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    cleared_grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    lead_grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    lead_remote_statuses: Counter[str] = Counter()
    matching_clearance_required = 0
    actionable_clearance_excluded = 0
    lead_clearance_excluded = 0
    prefiltered_count = 0

    for row in rows:
        prefiltered_count += 1
        title = str(row.get("title") or "").strip()
        role = classify_role_text(title, classification_context(row))
        prefilter_statuses[role.status] += 1
        primary_target = role.status in {"strong", "possible"}
        expanded_title = role.status == "weak" and is_expanded_remote_lead_title(title)
        if not primary_target and not expanded_title:
            continue

        remote = classify_remote_eligibility(dict(row))
        requires_clearance = requires_active_government_clearance(
            row.get("description_text"), title=title
        )
        variant = _variant_from_classifications(
            row,
            role=role,
            remote=remote,
            requires_active_clearance=requires_clearance,
        )
        key = (int(variant["company_id"]), str(variant["normalized_title"]))
        remote_status = str(variant["remote_eligibility"])
        if primary_target:
            remote_statuses[remote_status] += 1
            grouped.setdefault(key, []).append(variant)
            if requires_clearance:
                matching_clearance_required += 1
                if remote_status in ACTIONABLE_REMOTE_STATUSES:
                    actionable_clearance_excluded += 1
            else:
                cleared_grouped.setdefault(key, []).append(variant)

        if remote_status not in REMOTE_LEAD_STATUSES:
            continue
        if requires_clearance:
            lead_clearance_excluded += 1
            continue
        lead_variant = {
            **variant,
            "role_scope": (
                "primary_target" if primary_target else "expanded_fullstack_software"
            ),
        }
        lead_remote_statuses[remote_status] += 1
        lead_grouped.setdefault(key, []).append(lead_variant)

    clusters = _cluster_variants(grouped)
    actionable_clusters = [
        item
        for item in _cluster_variants(cleared_grouped)
        if is_actionable_cluster(item)
    ]
    lead_clusters = _cluster_variants(lead_grouped, remote_leads=True)
    matching_raw = sum(len(variants) for variants in grouped.values())
    summary = {
        "title_prefiltered_job_count": prefiltered_count,
        "prefilter_role_status_distribution": ordered_counts(
            prefilter_statuses, ROLE_STATUS_ORDER
        ),
        "matching_raw_variant_count": matching_raw,
        "matching_company_title_cluster_count": len(clusters),
        "matching_duplicate_variant_count": matching_raw - len(clusters),
        "matching_variant_remote_status_distribution": ordered_counts(
            remote_statuses, REMOTE_ELIGIBILITY_ORDER
        ),
        "clearance_required_matching_variant_count": matching_clearance_required,
        "actionable_clearance_excluded_variant_count": actionable_clearance_excluded,
        "actionable_cluster_count": len(actionable_clusters),
    }
    lead_raw = sum(len(variants) for variants in lead_grouped.values())
    lead_summary = {
        "lead_raw_variant_count": lead_raw,
        "lead_company_title_cluster_count": len(lead_clusters),
        "lead_duplicate_variant_count": lead_raw - len(lead_clusters),
        "clearance_excluded_variant_count": lead_clearance_excluded,
        "lead_tier_distribution": ordered_counts(
            Counter(str(item["lead_tier"]) for item in lead_clusters),
            LEAD_TIER_ORDER,
        ),
        "role_scope_distribution": ordered_counts(
            Counter(str(item["role_scope"]) for item in lead_clusters),
            ROLE_SCOPE_ORDER,
        ),
        "work_arrangement_distribution": ordered_counts(
            Counter(str(item["work_arrangement"]) for item in lead_clusters)
        ),
        "geographic_eligibility_distribution": ordered_counts(
            Counter(str(item["geographic_eligibility"]) for item in lead_clusters),
            ("pakistan", "global", "unknown", "regional_unconfirmed"),
        ),
        "lead_variant_remote_status_distribution": ordered_counts(
            lead_remote_statuses, REMOTE_ELIGIBILITY_ORDER
        ),
    }
    return RoleAnalysis(
        matching_clusters=clusters,
        role_summary=summary,
        actionable_clusters=actionable_clusters,
        remote_leads=lead_clusters,
        remote_leads_summary=lead_summary,
    )


def build_role_clusters(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    analysis = _analyze_role_rows(rows)
    return analysis.matching_clusters, analysis.role_summary


def is_actionable_cluster(cluster: Mapping[str, Any]) -> bool:
    return (
        str(cluster.get("best_remote_eligibility") or "")
        in ACTIONABLE_REMOTE_STATUSES
        and not _cluster_has_required_active_clearance(cluster)
    )


def _cluster_has_required_active_clearance(cluster: Mapping[str, Any]) -> bool:
    if int(cluster.get("required_active_clearance_variant_count") or 0) > 0:
        return True
    best = cluster.get("best_variant")
    return isinstance(best, Mapping) and bool(best.get("requires_active_clearance"))


def actionable_csv_row(cluster: Mapping[str, Any]) -> dict[str, Any]:
    if _cluster_has_required_active_clearance(cluster):
        raise ValueError("actionable CSV rows cannot require active government clearance")
    if not is_actionable_cluster(cluster):
        raise ValueError("CSV rows require explicit Pakistan or global eligibility evidence")
    best = cluster["best_variant"]
    return {
        "company_name": cluster["company_name"],
        "company_slug": cluster["company_slug"],
        "normalized_title": cluster["normalized_title"],
        "representative_title": cluster["representative_title"],
        "role_match_status": cluster["role_match_status"],
        "best_remote_eligibility": cluster["best_remote_eligibility"],
        "posting_variant_count": cluster["posting_variant_count"],
        "missing_location_variant_count": cluster["missing_location_variant_count"],
        "locations": json.dumps(cluster["locations"], separators=(",", ":")),
        "role_status_distribution": json.dumps(
            cluster["role_status_distribution"], separators=(",", ":")
        ),
        "remote_eligibility_distribution": json.dumps(
            cluster["remote_eligibility_distribution"], separators=(",", ":")
        ),
        "provider_distribution": json.dumps(
            cluster["provider_distribution"], separators=(",", ":")
        ),
        "provider": best["provider"],
        "external_job_id": best["external_job_id"],
        "posting_url": best["posting_url"],
        "apply_url": best["apply_url"],
        "best_location": best["location"],
        "department": best["department"],
        "source_published_at": best["source_published_at"],
        "source_updated_at": best["source_updated_at"],
        "remote_reasons": json.dumps(best["remote_reasons"], separators=(",", ":")),
        "remote_evidence": json.dumps(best["remote_evidence"], separators=(",", ":")),
        "eligibility_caveat": REMOTE_ELIGIBILITY_CAVEAT,
    }


def remote_lead_csv_row(cluster: Mapping[str, Any]) -> dict[str, Any]:
    if _cluster_has_required_active_clearance(cluster):
        raise ValueError("remote lead rows cannot require active government clearance")
    remote_status = str(cluster.get("best_remote_eligibility") or "")
    if remote_status not in REMOTE_LEAD_STATUSES:
        raise ValueError("remote lead rows require an allowed remote eligibility status")
    lead_tier = str(cluster.get("lead_tier") or "")
    if lead_tier != LEAD_TIER_BY_REMOTE_STATUS[remote_status]:
        raise ValueError("remote lead tier does not match its eligibility status")
    role_scope = str(cluster.get("role_scope") or "")
    if role_scope not in ROLE_SCOPE_ORDER:
        raise ValueError("remote lead rows require a supported role scope")
    if cluster.get("work_arrangement") != "remote":
        raise ValueError("remote lead rows require work_arrangement=remote")
    geographic_eligibility = str(cluster.get("geographic_eligibility") or "")
    if geographic_eligibility != GEOGRAPHIC_ELIGIBILITY_BY_REMOTE_STATUS[remote_status]:
        raise ValueError(
            "remote lead geographic eligibility does not match its eligibility status"
        )
    best = cluster["best_variant"]
    return {
        "lead_tier": lead_tier,
        "role_scope": role_scope,
        "work_arrangement": "remote",
        "geographic_eligibility": geographic_eligibility,
        "company_name": cluster["company_name"],
        "company_slug": cluster["company_slug"],
        "normalized_title": cluster["normalized_title"],
        "representative_title": cluster["representative_title"],
        "role_match_status": cluster["role_match_status"],
        "best_remote_eligibility": remote_status,
        "posting_variant_count": cluster["posting_variant_count"],
        "missing_location_variant_count": cluster[
            "missing_location_variant_count"
        ],
        "locations": json.dumps(cluster["locations"], separators=(",", ":")),
        "role_status_distribution": json.dumps(
            cluster["role_status_distribution"], separators=(",", ":")
        ),
        "remote_eligibility_distribution": json.dumps(
            cluster["remote_eligibility_distribution"], separators=(",", ":")
        ),
        "provider_distribution": json.dumps(
            cluster["provider_distribution"], separators=(",", ":")
        ),
        "provider": best["provider"],
        "external_job_id": best["external_job_id"],
        "posting_url": best["posting_url"],
        "apply_url": best["apply_url"],
        "best_location": best["location"],
        "department": best["department"],
        "source_published_at": best["source_published_at"],
        "source_updated_at": best["source_updated_at"],
        "review_note": cluster["review_note"],
        "review_evidence": json.dumps(
            cluster["review_evidence"], separators=(",", ":")
        ),
        "remote_reasons": json.dumps(best["remote_reasons"], separators=(",", ":")),
        "remote_evidence": json.dumps(best["remote_evidence"], separators=(",", ":")),
        "eligibility_caveat": REMOTE_ELIGIBILITY_CAVEAT,
    }


def _grouped_counts(
    connection: Connection, columns: Sequence[Any], table: Any
) -> list[dict[str, Any]]:
    statement = select(*columns, func.count().label("count")).select_from(table).group_by(*columns)
    return [dict(row) for row in connection.execute(statement).mappings()]


def collect_provider_funnel(connection: Connection) -> dict[str, Any]:
    source_rows = _grouped_counts(
        connection,
        (
            career_sources_table.c.provider,
            career_sources_table.c.status,
            career_sources_table.c.last_sync_status,
        ),
        career_sources_table,
    )
    job_rows = _grouped_counts(
        connection,
        (job_postings_table.c.provider, job_postings_table.c.status),
        job_postings_table,
    )
    sync_rows = _grouped_counts(
        connection,
        (source_sync_runs_table.c.provider, source_sync_runs_table.c.status),
        source_sync_runs_table,
    )
    company_rows = connection.execute(
        select(
            job_postings_table.c.provider,
            func.count(func.distinct(job_postings_table.c.company_id)).label("count"),
        )
        .where(job_postings_table.c.status == "active")
        .group_by(job_postings_table.c.provider)
    ).mappings()
    companies_by_provider = {str(row["provider"]): int(row["count"]) for row in company_rows}

    providers: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        provider = str(row["provider"])
        record = providers.setdefault(provider, _empty_provider_record())
        count = int(row["count"])
        record["career_sources"]["total"] += count
        record["career_sources"]["status"][str(row["status"])] += count
        last_sync = str(row["last_sync_status"] or "never")
        record["career_sources"]["last_sync_status"][last_sync] += count
    for row in job_rows:
        provider = str(row["provider"])
        record = providers.setdefault(provider, _empty_provider_record())
        count = int(row["count"])
        record["jobs"]["total"] += count
        record["jobs"]["status"][str(row["status"])] += count
    for row in sync_rows:
        provider = str(row["provider"])
        record = providers.setdefault(provider, _empty_provider_record())
        count = int(row["count"])
        record["source_sync_runs"]["total"] += count
        record["source_sync_runs"]["status"][str(row["status"])] += count

    for provider, record in providers.items():
        record["companies_with_active_jobs"] = companies_by_provider.get(provider, 0)
        for section in ("career_sources", "jobs", "source_sync_runs"):
            record[section]["status"] = ordered_counts(record[section]["status"])
        record["career_sources"]["last_sync_status"] = ordered_counts(
            record["career_sources"]["last_sync_status"]
        )

    return {
        "provider_count": len(providers),
        "career_source_count": sum(
            record["career_sources"]["total"] for record in providers.values()
        ),
        "job_count": sum(record["jobs"]["total"] for record in providers.values()),
        "providers": {provider: providers[provider] for provider in sorted(providers)},
    }


def _empty_provider_record() -> dict[str, Any]:
    return {
        "career_sources": {
            "total": 0,
            "status": Counter(),
            "last_sync_status": Counter(),
        },
        "jobs": {"total": 0, "status": Counter()},
        "source_sync_runs": {"total": 0, "status": Counter()},
        "companies_with_active_jobs": 0,
    }


def collect_active_overview(connection: Connection, *, as_of: datetime) -> dict[str, Any]:
    raw_count = 0
    company_title_clusters: set[tuple[int, str]] = set()
    age_counts: Counter[str] = Counter()
    statement = select(
        job_postings_table.c.company_id,
        job_postings_table.c.title,
        job_postings_table.c.source_published_at,
    ).where(job_postings_table.c.status == "active")
    for row in connection.execution_options(stream_results=True).execute(statement).mappings():
        raw_count += 1
        company_title_clusters.add((int(row["company_id"]), normalize_title(str(row["title"]))))
        age_counts[age_bucket(row["source_published_at"], as_of=as_of)] += 1

    age_order = (
        "future",
        "0_30_days",
        "31_90_days",
        "91_180_days",
        "181_365_days",
        "over_365_days",
        "unknown",
    )
    cluster_count = len(company_title_clusters)
    return {
        "raw_active_job_count": raw_count,
        "company_normalized_title_cluster_count": cluster_count,
        "duplicate_location_or_board_variant_count": raw_count - cluster_count,
        "source_published_age_buckets": ordered_counts(age_counts, age_order),
    }


def collect_structured_evidence_coverage(connection: Connection) -> dict[str, Any]:
    rows = connection.execute(
        text(
            """
            SELECT
                provider,
                COUNT(*)::bigint AS active_jobs,
                COUNT(*) FILTER (WHERE structured_evidence <> '{}'::jsonb)::bigint
                    AS nonempty,
                COUNT(*) FILTER (
                    WHERE COALESCE(structured_evidence -> 'workplace', '{}'::jsonb)
                        <> '{}'::jsonb
                )::bigint AS workplace,
                COUNT(*) FILTER (
                    WHERE structured_evidence -> 'primary_location' IS NOT NULL
                      AND structured_evidence -> 'primary_location' <> 'null'::jsonb
                )::bigint AS primary_location,
                COUNT(*) FILTER (
                    WHERE jsonb_array_length(
                        COALESCE(structured_evidence -> 'eligibility_signals', '[]'::jsonb)
                    ) > 0
                )::bigint AS eligibility_signals
            FROM job_postings
            WHERE status = 'active'
            GROUP BY provider
            ORDER BY provider
            """
        )
    ).mappings()
    providers: dict[str, Any] = {}
    totals = Counter()
    for row in rows:
        total = int(row["active_jobs"])
        provider = str(row["provider"])
        counts = {
            "active_jobs": total,
            "nonempty": int(row["nonempty"]),
            "workplace": int(row["workplace"]),
            "primary_location": int(row["primary_location"]),
            "eligibility_signals": int(row["eligibility_signals"]),
        }
        providers[provider] = {
            **counts,
            "nonempty_percent": percent(counts["nonempty"], total),
            "workplace_percent": percent(counts["workplace"], total),
            "primary_location_percent": percent(counts["primary_location"], total),
            "eligibility_signals_percent": percent(counts["eligibility_signals"], total),
        }
        totals.update(counts)
    total_jobs = totals["active_jobs"]
    return {
        "active_jobs": total_jobs,
        "nonempty": totals["nonempty"],
        "nonempty_percent": percent(totals["nonempty"], total_jobs),
        "workplace": totals["workplace"],
        "primary_location": totals["primary_location"],
        "eligibility_signals": totals["eligibility_signals"],
        "providers": providers,
    }


def percent(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 2) if denominator else 0.0


def collect_jobs_per_board(connection: Connection, *, top_limit: int) -> dict[str, Any]:
    rows = list(
        connection.execute(
            select(
                career_sources_table.c.id.label("career_source_id"),
                career_sources_table.c.provider,
                career_sources_table.c.external_source_id,
                companies_table.c.name.label("company_name"),
                companies_table.c.slug.label("company_slug"),
                func.count(job_postings_table.c.id).label("active_job_count"),
            )
            .select_from(career_sources_table)
            .join(companies_table, companies_table.c.id == career_sources_table.c.company_id)
            .join(
                job_postings_table,
                (job_postings_table.c.career_source_id == career_sources_table.c.id)
                & (job_postings_table.c.status == "active"),
            )
            .group_by(
                career_sources_table.c.id,
                career_sources_table.c.provider,
                career_sources_table.c.external_source_id,
                companies_table.c.name,
                companies_table.c.slug,
            )
        ).mappings()
    )
    counts = [int(row["active_job_count"]) for row in rows]
    top_rows = sorted(
        rows,
        key=lambda row: (
            -int(row["active_job_count"]),
            str(row["provider"]),
            str(row["external_source_id"]),
        ),
    )[:top_limit]
    return {
        "boards_with_active_jobs": len(counts),
        "mean": round(sum(counts) / len(counts), 2) if counts else 0.0,
        "minimum": min(counts, default=0),
        "p50_nearest_rank": nearest_rank(counts, 0.50),
        "p90_nearest_rank": nearest_rank(counts, 0.90),
        "p99_nearest_rank": nearest_rank(counts, 0.99),
        "maximum": max(counts, default=0),
        "top_noisy_boards": [
            {
                "career_source_id": int(row["career_source_id"]),
                "provider": str(row["provider"]),
                "external_source_id": str(row["external_source_id"]),
                "company_name": str(row["company_name"]),
                "company_slug": str(row["company_slug"]),
                "active_job_count": int(row["active_job_count"]),
            }
            for row in top_rows
        ],
    }


def load_role_candidate_rows(connection: Connection) -> list[dict[str, Any]]:
    statement = (
        select(
            job_postings_table.c.id,
            job_postings_table.c.company_id,
            companies_table.c.name.label("company_name"),
            companies_table.c.slug.label("company_slug"),
            job_postings_table.c.provider,
            job_postings_table.c.external_job_id,
            job_postings_table.c.title,
            job_postings_table.c.posting_url,
            job_postings_table.c.apply_url,
            job_postings_table.c.location,
            job_postings_table.c.department,
            job_postings_table.c.employment_type,
            job_postings_table.c.structured_evidence,
            job_postings_table.c.source_published_at,
            job_postings_table.c.source_updated_at,
            job_posting_versions_table.c.description_text,
        )
        .select_from(job_postings_table)
        .join(companies_table, companies_table.c.id == job_postings_table.c.company_id)
        .outerjoin(
            job_posting_versions_table,
            job_posting_versions_table.c.id == job_postings_table.c.current_version_id,
        )
        .where(
            job_postings_table.c.status == "active",
            job_postings_table.c.title.op("~*")(ROLE_TITLE_PREFILTER_PATTERN),
        )
        .order_by(job_postings_table.c.id)
    )
    return [
        dict(row)
        for row in connection.execution_options(stream_results=True).execute(statement).mappings()
    ]


def summarize_history_run_dir(run_dir: Path | None) -> dict[str, Any] | None:
    if run_dir is None:
        return None
    resolved = run_dir.resolve()
    artifacts = {
        label: summarize_history_artifact(resolved / filename, kind=label)
        for label, filename in HISTORY_ARTIFACTS.items()
    }
    return {
        "run_dir": str(resolved),
        "exists": resolved.is_dir(),
        "available_artifact_count": sum(item["exists"] for item in artifacts.values()),
        "missing_artifacts": [
            item["filename"] for item in artifacts.values() if not item["exists"]
        ],
        "artifacts": artifacts,
    }


def summarize_history_artifact(path: Path, *, kind: str) -> dict[str, Any]:
    base: dict[str, Any] = {"filename": path.name, "exists": path.is_file()}
    if not path.is_file():
        return base
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {**base, "read_error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(payload, dict):
        return {**base, "read_error": "artifact root is not a JSON object"}

    summary = {
        key: payload[key]
        for key in HISTORY_SCALAR_KEYS
        if key in payload and isinstance(payload[key], (str, int, float, bool, type(None)))
    }
    if kind == "union" and isinstance(payload.get("inputs"), list):
        summary["inputs"] = [
            {
                key: item[key]
                for key in (
                    "crawl_id",
                    "input_row_count",
                    "token_count",
                    "marginal_new_tokens",
                    "observation_count",
                )
                if key in item
            }
            for item in payload["inputs"]
            if isinstance(item, dict)
        ]
    if kind == "orchestrator" and isinstance(payload.get("stages"), dict):
        summary["stages"] = {
            str(stage): {
                key: record[key]
                for key in ("state", "return_code", "started_at", "finished_at", "error")
                if key in record
            }
            for stage, record in payload["stages"].items()
            if isinstance(record, dict)
        }
    if kind == "sync_checkpoint" and isinstance(payload.get("sources"), dict):
        sources = [item for item in payload["sources"].values() if isinstance(item, dict)]
        summary["source_count"] = len(sources)
        summary["source_state_distribution"] = ordered_counts(
            Counter(str(item.get("state") or "unknown") for item in sources)
        )
        summary["attempt_distribution"] = ordered_counts(
            Counter(str(int(item.get("attempts") or 0)) for item in sources)
        )
        summary["retryable_source_count"] = sum(bool(item.get("retryable")) for item in sources)
    return {**base, "summary": summary}


def build_report(
    *,
    as_of: datetime,
    provider_funnel: dict[str, Any],
    active_overview: dict[str, Any],
    structured_evidence: dict[str, Any],
    jobs_per_board: dict[str, Any],
    candidate_rows: Sequence[Mapping[str, Any]],
    history_run_dir: Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    analysis = _analyze_role_rows(candidate_rows)
    clusters = analysis.matching_clusters
    role_summary = analysis.role_summary
    actionable = analysis.actionable_clusters
    remote_leads = analysis.remote_leads
    remote_leads_summary = analysis.remote_leads_summary
    raw_active = int(active_overview["raw_active_job_count"])
    matching = int(role_summary["matching_raw_variant_count"])
    prefilter_distribution = Counter(role_summary["prefilter_role_status_distribution"])
    all_role_distribution = {
        "strong": prefilter_distribution["strong"],
        "possible": prefilter_distribution["possible"],
        "weak": prefilter_distribution["weak"],
        "exclude": raw_active
        - prefilter_distribution["strong"]
        - prefilter_distribution["possible"]
        - prefilter_distribution["weak"],
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "scope": {
            "jobs": "Canonical active provider jobs in Postgres",
            "role_lane": "Senior backend / senior software engineering",
            "remote_eligibility": (
                "Deterministic role-specific public posting evidence; only pakistan_explicit and "
                "global_explicit are exported as actionable."
            ),
            "eligibility_caveat": REMOTE_ELIGIBILITY_CAVEAT,
            "description_handling": (
                "Descriptions were loaded only for SQL title-prefiltered engineering candidates "
                "and inform deterministic classification. Bounded evidence excerpts may be "
                "emitted; full descriptions and description_text fields are never written."
            ),
        },
        "provider_funnel": provider_funnel,
        "active_corpus": active_overview,
        "role_analysis": {
            **role_summary,
            "all_active_role_status_distribution": ordered_counts(
                all_role_distribution, ROLE_STATUS_ORDER
            ),
            "matching_raw_percent": percent(matching, raw_active),
        },
        "remote_leads_analysis": {
            **remote_leads_summary,
            "role_scope": (
                "Primary strong/possible roles plus weak roles whose titles explicitly name "
                "full-stack or software engineer/developer work, including common SDE and "
                "software-systems variants."
            ),
            "eligibility_scope": (
                "Confirmed means explicit Pakistan/worldwide evidence. verify_country and "
                "verify_region are research queues, never apply-now conclusions."
            ),
            "dimensions": {
                "work_arrangement": "All emitted leads have explicit remote evidence.",
                "geographic_eligibility": dict(
                    GEOGRAPHIC_ELIGIBILITY_BY_REMOTE_STATUS
                ),
            },
            "clearance_filter": (
                "Rows explicitly requiring an active Secret, TS-SCI, or DoD clearance "
                "remain measurable in matching-role analysis but are excluded from both CSV "
                "queues."
            ),
            "description_handling": (
                "Descriptions inform deterministic classification and clearance filtering. "
                "Bounded evidence excerpts may be emitted; full descriptions and "
                "description_text fields are never written."
            ),
            "eligibility_caveat": REMOTE_ELIGIBILITY_CAVEAT,
        },
        "structured_evidence_coverage": structured_evidence,
        "jobs_per_board": jobs_per_board,
        "history_backfill": summarize_history_run_dir(history_run_dir),
        "matching_role_clusters": clusters,
        "actionable_clusters": actionable,
        "remote_role_leads": remote_leads,
    }
    return report, actionable


def _serialize_json(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, default=iso_value) + "\n"
    ).encode()


def _serialize_csv(
    *,
    fieldnames: Sequence[str],
    clusters: Sequence[Mapping[str, Any]],
    row_builder: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> bytes:
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="raise")
    writer.writeheader()
    writer.writerows(row_builder(cluster) for cluster in clusters)
    return target.getvalue().encode("utf-8")


def _stage_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.stage-", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return temporary_path


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    staged = _stage_bytes(path, payload)
    try:
        os.replace(staged, path)
    except BaseException:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
        raise


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _write_bytes_atomic(path, _serialize_json(payload))


def write_actionable_csv_atomic(path: Path, clusters: Sequence[Mapping[str, Any]]) -> None:
    _write_bytes_atomic(
        path,
        _serialize_csv(
            fieldnames=ACTIONABLE_CSV_FIELDS,
            clusters=clusters,
            row_builder=actionable_csv_row,
        ),
    )


def write_remote_leads_csv_atomic(
    path: Path, clusters: Sequence[Mapping[str, Any]]
) -> None:
    _write_bytes_atomic(
        path,
        _serialize_csv(
            fieldnames=REMOTE_LEADS_CSV_FIELDS,
            clusters=clusters,
            row_builder=remote_lead_csv_row,
        ),
    )


def _report_artifact_payloads(
    report: Mapping[str, Any], actionable: Sequence[Mapping[str, Any]]
) -> dict[str, bytes]:
    remote_leads = report.get("remote_role_leads")
    if not isinstance(remote_leads, list):
        raise ValueError("report remote_role_leads must be a list")
    return {
        "job_funnel_report.json": _serialize_json(report),
        "actionable_job_clusters.csv": _serialize_csv(
            fieldnames=ACTIONABLE_CSV_FIELDS,
            clusters=actionable,
            row_builder=actionable_csv_row,
        ),
        "remote_role_leads.csv": _serialize_csv(
            fieldnames=REMOTE_LEADS_CSV_FIELDS,
            clusters=remote_leads,
            row_builder=remote_lead_csv_row,
        ),
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _backup_path(target: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.backup-", dir=target.parent
    )
    os.close(descriptor)
    backup = Path(name)
    backup.unlink()
    return backup


def publish_report_artifacts(
    output_dir: Path,
    *,
    report: Mapping[str, Any],
    actionable: Sequence[Mapping[str, Any]],
) -> dict[str, Path]:
    """Prevalidate and publish the three report artifacts as a rollback-safe set."""
    payloads = _report_artifact_payloads(report, actionable)
    if tuple(payloads) != REPORT_ARTIFACT_FILENAMES:
        raise RuntimeError("report artifact payload set is incomplete")

    targets = {name: output_dir / name for name in REPORT_ARTIFACT_FILENAMES}
    staged: dict[Path, Path] = {}
    try:
        for name, payload in payloads.items():
            target = targets[name]
            staged[target] = _stage_bytes(target, payload)
    except BaseException:
        for staged_path in staged.values():
            staged_path.unlink(missing_ok=True)
        raise

    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    try:
        for target in targets.values():
            if target.exists() or target.is_symlink():
                backup = _backup_path(target)
                os.replace(target, backup)
                backups[target] = backup
            os.replace(staged[target], target)
            installed.append(target)
        _fsync_directory(output_dir)
    except BaseException as publish_error:
        rollback_errors: list[str] = []
        for target in reversed(tuple(targets.values())):
            if target in installed:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    rollback_errors.append(f"remove {target.name}: {exc}")
            backup = backups.get(target)
            if backup is not None and backup.exists():
                try:
                    os.replace(backup, target)
                except OSError as exc:
                    rollback_errors.append(f"restore {target.name}: {exc}")
        for staged_path in staged.values():
            staged_path.unlink(missing_ok=True)
        if rollback_errors:
            raise RuntimeError(
                "report publication failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from publish_error
        for backup in backups.values():
            backup.unlink(missing_ok=True)
        raise
    else:
        for backup in backups.values():
            backup.unlink(missing_ok=True)
        _fsync_directory(output_dir)
    finally:
        for staged_path in staged.values():
            staged_path.unlink(missing_ok=True)

    return targets


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    engine = engine_from_url(args.database_url)
    try:
        with engine.connect() as connection, connection.begin():
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            provider_funnel = collect_provider_funnel(connection)
            active_overview = collect_active_overview(connection, as_of=args.as_of)
            structured_evidence = collect_structured_evidence_coverage(connection)
            jobs_per_board = collect_jobs_per_board(connection, top_limit=args.top_boards)
            candidate_rows = load_role_candidate_rows(connection)
    finally:
        engine.dispose()

    report, actionable = build_report(
        as_of=args.as_of,
        provider_funnel=provider_funnel,
        active_overview=active_overview,
        structured_evidence=structured_evidence,
        jobs_per_board=jobs_per_board,
        candidate_rows=candidate_rows,
        history_run_dir=args.history_run_dir,
    )
    published = publish_report_artifacts(
        args.output_dir,
        report=report,
        actionable=actionable,
    )
    json_path = published["job_funnel_report.json"]
    csv_path = published["actionable_job_clusters.csv"]
    remote_leads_path = published["remote_role_leads.csv"]

    print(
        f"Measured {active_overview['raw_active_job_count']} active jobs into "
        f"{active_overview['company_normalized_title_cluster_count']} company-title clusters."
    )
    print(
        f"Found {report['role_analysis']['matching_company_title_cluster_count']} matching "
        f"role clusters and {len(actionable)} explicit Pakistan/global evidence clusters."
    )
    print(f"Wrote JSON report: {json_path.resolve()}")
    print(f"Wrote actionable CSV: {csv_path.resolve()}")
    print(f"Wrote tiered remote leads CSV: {remote_leads_path.resolve()}")


if __name__ == "__main__":
    main()
