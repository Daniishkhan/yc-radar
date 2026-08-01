"""Preserve provider-native structured job evidence in current state and history."""

from __future__ import annotations

import json
import re
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_job_structured_evidence"
down_revision = "0004_source_registries"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB
_BATCH_SIZE = 500
_ELIGIBILITY_TERMS = (
    "authorisation",
    "authorization",
    "citizen",
    "country",
    "countries",
    "eligib",
    "location",
    "region",
    "remote",
    "residen",
    "sponsor",
    "time zone",
    "timezone",
    "visa",
)
_COUNTRY_NAMES = frozenset(
    {
        "allowed countries",
        "allowed country",
        "countries",
        "country",
        "eligible countries",
        "eligible country",
        "hiring countries",
        "hiring country",
        "location countries",
        "location country",
        "work countries",
        "work country",
    }
)
_COUNTRY_SEPARATOR_RE = re.compile(r"\s*[,;|]\s*")


def upgrade() -> None:
    op.add_column("job_postings", _evidence_column())
    op.add_column("job_posting_versions", _evidence_column())
    _backfill_versions()
    op.get_bind().exec_driver_sql(
        """
        UPDATE job_postings AS job
        SET structured_evidence = version.structured_evidence
        FROM job_posting_versions AS version
        WHERE version.id = job.current_version_id
        """
    )


def downgrade() -> None:
    op.drop_column("job_posting_versions", "structured_evidence")
    op.drop_column("job_postings", "structured_evidence")


def _evidence_column() -> sa.Column[Any]:
    return sa.Column(
        "structured_evidence",
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )


def _backfill_versions() -> None:
    """Page legacy JSONB rows so upgrades do not load the whole history into memory."""
    connection = op.get_bind()
    select_statement = sa.text(
        """
        SELECT version.id, job.provider, version.raw_payload
        FROM job_posting_versions AS version
        JOIN job_postings AS job ON job.id = version.job_posting_id
        WHERE version.id > :last_id
        ORDER BY version.id
        LIMIT :batch_size
        """
    )
    update_statement = sa.text(
        """
        UPDATE job_posting_versions
        SET structured_evidence = :structured_evidence
        WHERE id = :id
        """
    ).bindparams(sa.bindparam("structured_evidence", type_=JSONB))
    last_id = 0
    while True:
        rows = connection.execute(
            select_statement,
            {"last_id": last_id, "batch_size": _BATCH_SIZE},
        ).mappings().all()
        if not rows:
            break
        batch = [
            {
                "id": int(row["id"]),
                "structured_evidence": _structured_evidence(
                    str(row["provider"]),
                    row["raw_payload"] if isinstance(row["raw_payload"], dict) else {},
                ),
            }
            for row in rows
        ]
        connection.execute(update_statement, batch)
        last_id = int(rows[-1]["id"])


def _structured_evidence(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    if provider == "greenhouse":
        metadata = _greenhouse_metadata(payload.get("metadata"))
        location = payload.get("location")
        label = _string(location.get("name")) if isinstance(location, dict) else _string(location)
        return {
            "schema_version": 1,
            "provider": provider,
            "workplace": {},
            "primary_location": {"label": label} if label else None,
            "secondary_locations": [],
            "offices": _records(
                _compact(name=item.get("name"), location=item.get("location"))
                for item in _objects(payload.get("offices"))
            ),
            "countries": _metadata_countries(metadata),
            "provider_metadata": metadata,
            "eligibility_signals": [
                {"kind": "provider_metadata", **item}
                for item in metadata
                if any(term in str(item.get("name") or "").casefold() for term in _ELIGIBILITY_TERMS)
            ],
            "application": _compact(
                is_listed=True,
                posting_url=payload.get("absolute_url"),
                language=payload.get("language"),
                application_deadline=payload.get("application_deadline"),
            ),
        }
    if provider == "ashby":
        primary = _ashby_location(payload.get("location"), payload.get("address"))
        secondary = _records(
            location
            for item in _objects(payload.get("secondaryLocations"))
            for location in [_ashby_location(item.get("location"), item.get("address"))]
            if location
        )
        locations = [item for item in [primary, *secondary] if item]
        is_remote = payload.get("isRemote")
        is_listed = payload.get("isListed")
        return {
            "schema_version": 1,
            "provider": provider,
            "workplace": _compact(
                type=_workplace_type(payload.get("workplaceType")),
                is_remote=is_remote if isinstance(is_remote, bool) else None,
            ),
            "primary_location": primary,
            "secondary_locations": secondary,
            "offices": [],
            "countries": _strings(item.get("country") for item in locations),
            "provider_metadata": [],
            "eligibility_signals": [],
            "application": _compact(
                is_listed=is_listed if isinstance(is_listed, bool) else None,
                posting_url=payload.get("jobUrl"),
                apply_url=payload.get("applyUrl"),
            ),
        }
    return {
        "schema_version": 1,
        "provider": provider,
        "workplace": {},
        "primary_location": None,
        "secondary_locations": [],
        "offices": [],
        "countries": [],
        "provider_metadata": [],
        "eligibility_signals": [],
        "application": {},
    }


def _greenhouse_metadata(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in _objects(value):
        record = _compact(name=item.get("name"), value_type=item.get("value_type"))
        if "value" in item:
            record["value"] = _json_value(item.get("value"))
        if record:
            items.append(record)
    return _records(items)


def _metadata_countries(metadata: list[dict[str, Any]]) -> list[str]:
    countries: list[str] = []
    for item in metadata:
        if str(item.get("name") or "").casefold().strip() not in _COUNTRY_NAMES:
            continue
        countries.extend(_country_values(item.get("value")))
    return _strings(countries)


def _country_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in _COUNTRY_SEPARATOR_RE.split(value) if part]
    if isinstance(value, list):
        return [country for item in value for country in _country_values(item)]
    if isinstance(value, dict):
        return [country for item in value.values() for country in _country_values(item)]
    return []


def _ashby_location(label: Any, value: Any) -> dict[str, Any] | None:
    address = value if isinstance(value, dict) else {}
    postal = address.get("postalAddress")
    if isinstance(postal, dict):
        address = postal
    result = _compact(
        label=label,
        locality=address.get("addressLocality"),
        region=address.get("addressRegion"),
        country=address.get("addressCountry"),
    )
    return result or None


def _workplace_type(value: Any) -> str | None:
    raw = _string(value)
    if raw is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
    return {"onsite": "on_site", "on_site": "on_site"}.get(normalized, normalized) or None


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _compact(**values: Any) -> dict[str, Any]:
    return {
        key: normalized
        for key, value in values.items()
        if (normalized := _string(value) if isinstance(value, str) else value) is not None
    }


def _records(values: Any) -> list[dict[str, Any]]:
    distinct: dict[str, dict[str, Any]] = {}
    for value in values:
        if not value:
            continue
        canonical = _json_value(value)
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        distinct[encoded] = canonical
    return [distinct[key] for key in sorted(distinct)]


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _strings(values: Any) -> list[str]:
    distinct: dict[str, str] = {}
    for value in values:
        normalized = _string(value)
        if normalized:
            distinct.setdefault(normalized.casefold(), normalized)
    return [distinct[key] for key in sorted(distinct)]


def _string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
