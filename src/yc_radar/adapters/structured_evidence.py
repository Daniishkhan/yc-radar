from __future__ import annotations

import json
import re
from typing import Any


EVIDENCE_SCHEMA_VERSION = 1

_ELIGIBILITY_METADATA_TERMS = (
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
_COUNTRY_METADATA_NAMES = frozenset(
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


def greenhouse_structured_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize Greenhouse's public structured fields without inferring eligibility.

    Office membership is retained as supporting evidence only. It is not promoted to a hiring
    country because an organizational office does not prove where a remote applicant may reside.
    Provider IDs remain available in ``raw_payload``; the normalized view intentionally uses the
    less volatile semantic office and metadata fields.
    """
    posting_url = _optional_string(payload.get("absolute_url"))
    metadata = _greenhouse_metadata(payload.get("metadata"))
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "provider": "greenhouse",
        "requisition_id": _explicit_requisition_id(payload),
        "workplace": {},
        "primary_location": _labelled_location(payload.get("location")),
        "secondary_locations": [],
        "offices": _greenhouse_offices(payload.get("offices")),
        "countries": _metadata_countries(metadata),
        "provider_metadata": metadata,
        "eligibility_signals": [
            {"kind": "provider_metadata", **entry}
            for entry in metadata
            if any(
                term in str(entry.get("name") or "").casefold()
                for term in _ELIGIBILITY_METADATA_TERMS
            )
        ],
        "application": _compact_mapping(
            is_listed=True,
            posting_url=posting_url,
            language=_optional_string(payload.get("language")),
            application_deadline=_optional_string(payload.get("application_deadline")),
        ),
    }


def ashby_structured_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    primary_location = _ashby_location(payload.get("location"), payload.get("address"))
    secondary_locations = _ashby_secondary_locations(payload.get("secondaryLocations"))
    locations = [
        location
        for location in [primary_location, *secondary_locations]
        if location is not None
    ]
    is_remote = payload.get("isRemote")
    workplace = _compact_mapping(
        type=_workplace_type(payload.get("workplaceType")),
        is_remote=is_remote if isinstance(is_remote, bool) else None,
    )
    is_listed = payload.get("isListed")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "provider": "ashby",
        "requisition_id": _explicit_requisition_id(payload),
        "workplace": workplace,
        "primary_location": primary_location,
        "secondary_locations": secondary_locations,
        "offices": [],
        "countries": _distinct_strings(
            location.get("country") for location in locations
        ),
        "provider_metadata": [],
        # Address countries describe posting locations, not applicant eligibility. Keep this empty
        # until the provider supplies an explicit eligibility or work-authorization field.
        "eligibility_signals": [],
        "application": _compact_mapping(
            is_listed=is_listed if isinstance(is_listed, bool) else None,
            posting_url=_optional_string(payload.get("jobUrl")),
            apply_url=_optional_string(payload.get("applyUrl")),
        ),
    }


def _greenhouse_offices(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return _canonical_records(
        _compact_mapping(
            name=_optional_string(item.get("name")),
            location=_optional_string(item.get("location")),
        )
        for item in value
        if isinstance(item, dict)
    )


def _greenhouse_metadata(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        record = _compact_mapping(
            name=_optional_string(item.get("name")),
            value_type=_optional_string(item.get("value_type")),
        )
        # A null custom-field value is meaningful and must not become indistinguishable from a
        # missing value. The provider-owned ID remains available in the immutable raw payload.
        if "value" in item:
            record["value"] = _canonical_json_value(item.get("value"))
        if record:
            records.append(record)
    return _canonical_records(records)


def _metadata_countries(metadata: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in metadata:
        name = str(item.get("name") or "").casefold().strip()
        if name not in _COUNTRY_METADATA_NAMES or "value" not in item:
            continue
        values.extend(_country_values(item["value"]))
    return _distinct_strings(values)


def _country_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in _COUNTRY_SEPARATOR_RE.split(value) if part]
    if isinstance(value, list):
        return [country for item in value for country in _country_values(item)]
    if isinstance(value, dict):
        return [country for item in value.values() for country in _country_values(item)]
    return []


def _ashby_secondary_locations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return _canonical_records(
        location
        for item in value
        if isinstance(item, dict)
        for location in [_ashby_location(item.get("location"), item.get("address"))]
        if location is not None
    )


def _ashby_location(label: Any, address: Any) -> dict[str, Any] | None:
    result = _compact_mapping(label=_optional_string(label), **_postal_address(address))
    return result or None


def _postal_address(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    postal = value.get("postalAddress")
    address = postal if isinstance(postal, dict) else value
    return _compact_mapping(
        locality=_optional_string(address.get("addressLocality")),
        region=_optional_string(address.get("addressRegion")),
        country=_optional_string(address.get("addressCountry")),
    )


def _labelled_location(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        label = _optional_string(value.get("name"))
    else:
        label = _optional_string(value)
    return {"label": label} if label else None


def _workplace_type(value: Any) -> str | None:
    raw = _optional_string(value)
    if raw is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
    return {"onsite": "on_site", "on_site": "on_site"}.get(normalized, normalized) or None


def _compact_mapping(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _canonical_records(values: Any) -> list[dict[str, Any]]:
    by_json: dict[str, dict[str, Any]] = {}
    for value in values:
        if not value:
            continue
        canonical = _canonical_json_value(value)
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        by_json[encoded] = canonical
    return [by_json[key] for key in sorted(by_json)]


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_canonical_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _distinct_strings(values: Any) -> list[str]:
    by_casefold: dict[str, str] = {}
    for value in values:
        normalized = _optional_string(value)
        if normalized:
            by_casefold.setdefault(normalized.casefold(), normalized)
    return [by_casefold[key] for key in sorted(by_casefold)]


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _explicit_requisition_id(payload: dict[str, Any]) -> str | None:
    for key in (
        "requisition_id",
        "requisitionId",
        "requisitionID",
        "requisition_number",
        "requisitionNumber",
        "req_id",
        "reqId",
        "job_code",
        "jobCode",
    ):
        if value := _optional_string(payload.get(key)):
            return value
    return None
