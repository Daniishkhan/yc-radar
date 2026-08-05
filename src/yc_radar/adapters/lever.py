from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot

_SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TAG_RE = re.compile(r"<[^>]+>")
_SITE_HOSTS = {
    "jobs.lever.co": "global",
    "jobs.eu.lever.co": "eu",
}
_API_HOSTS = {
    "api.lever.co": "global",
    "api.eu.lever.co": "eu",
}
_MIN_REASONABLE_CREATED_AT_MS = 946_684_800_000  # 2000-01-01T00:00:00Z


class LeverAdapter:
    """Read-only client for Lever's unauthenticated public Postings API."""

    provider = "lever"
    adapter_version = "1"
    source_kind = "ats_board"
    user_agent = (
        "yc-radar/0.2 (+https://github.com/Daniishkhan/yc-radar; read-only-public-job-sync)"
    )
    request_headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
    }

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout_seconds: float = 15.0,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self._client = client
        self._sleeper = sleeper
        self._timeout_seconds = timeout_seconds
        self._page_size = page_size
        self._max_pages = max_pages

    def extract_source_id(self, url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or host not in {*_SITE_HOSTS, *_API_HOSTS}
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None

        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if host in _API_HOSTS:
            if len(parts) < 3 or parts[:2] != ["v0", "postings"]:
                return None
            site = parts[2]
            instance = _API_HOSTS[host]
        else:
            if not parts:
                return None
            site = parts[0]
            instance = _SITE_HOSTS[host]
        if not _SITE_RE.fullmatch(site):
            return None
        normalized_site = site.lower()
        return f"eu:{normalized_site}" if instance == "eu" else normalized_site

    def canonical_source_url(self, external_source_id: str) -> str:
        parsed = _parse_external_source_id(external_source_id)
        if parsed is None:
            raise ValueError("invalid Lever site name")
        instance, site = parsed
        host = "jobs.eu.lever.co" if instance == "eu" else "jobs.lever.co"
        return f"https://{host}/{quote(site, safe='')}"

    async def fetch_snapshot(self, external_source_id: str) -> SourceSnapshot:
        parsed = _parse_external_source_id(external_source_id)
        if parsed is None:
            return self._failure_snapshot(external_source_id, "invalid_source_id")
        instance, site = parsed
        normalized_source_id = f"eu:{site}" if instance == "eu" else site
        api_host = "api.eu.lever.co" if instance == "eu" else "api.lever.co"
        url = f"https://{api_host}/v0/postings/{quote(site, safe='')}"
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            headers=self.request_headers,
            follow_redirects=False,
        )
        try:
            return await self._fetch_with_client(client, normalized_source_id, url)
        except httpx.HTTPError as exc:
            return self._failure_snapshot(
                normalized_source_id,
                type(exc).__name__,
                str(exc),
            )
        finally:
            if own_client:
                await client.aclose()

    async def _fetch_with_client(
        self,
        client: httpx.AsyncClient,
        external_source_id: str,
        url: str,
    ) -> SourceSnapshot:
        raw_jobs: list[Any] = []
        total_attempts = 0
        pages_requested = 0
        skip = 0

        for _page_number in range(1, self._max_pages + 1):
            response: httpx.Response | None = None
            transport_error: httpx.TransportError | None = None
            for attempt in range(1, 5):
                total_attempts += 1
                try:
                    response = await client.get(
                        url,
                        params={
                            "mode": "json",
                            "limit": self._page_size,
                            "skip": skip,
                        },
                        headers=self.request_headers,
                    )
                except httpx.TransportError as exc:
                    transport_error = exc
                    if attempt < 4:
                        await self._sleeper(self._retry_delay(None, attempt))
                    continue
                if response.status_code != 429 and response.status_code < 500:
                    break
                if attempt < 4:
                    await self._sleeper(self._retry_delay(response, attempt))

            pages_requested += 1
            metadata = self._request_metadata(
                url=url,
                attempts=total_attempts,
                pages_requested=pages_requested,
            )
            if response is None:
                assert transport_error is not None
                return self._failure_snapshot(
                    external_source_id,
                    type(transport_error).__name__,
                    str(transport_error),
                    request_metadata=metadata,
                )
            if response.status_code != 200:
                return self._failure_snapshot(
                    external_source_id,
                    "http_status",
                    str(response.status_code),
                    response.status_code,
                    metadata,
                )
            try:
                page = response.json()
            except json.JSONDecodeError as exc:
                return self._failure_snapshot(
                    external_source_id,
                    "invalid_json",
                    str(exc),
                    200,
                    metadata,
                )
            if not isinstance(page, list):
                return self._failure_snapshot(
                    external_source_id,
                    "invalid_wrapper",
                    "expected jobs list",
                    200,
                    metadata,
                )
            if not page:
                break
            raw_jobs.extend(page)
            skip += len(page)
        else:
            return self._failure_snapshot(
                external_source_id,
                "pagination_limit",
                f"snapshot exceeded {self._max_pages} pages",
                200,
                self._request_metadata(
                    url=url,
                    attempts=total_attempts,
                    pages_requested=pages_requested,
                ),
            )

        try:
            jobs = [normalize_lever_job(raw) for raw in raw_jobs]
        except ValueError as exc:
            return self._failure_snapshot(
                external_source_id,
                "invalid_job",
                str(exc),
                200,
                self._request_metadata(
                    url=url,
                    attempts=total_attempts,
                    pages_requested=pages_requested,
                ),
            )
        external_ids = [job.external_job_id for job in jobs]
        if len(external_ids) != len(set(external_ids)):
            return self._failure_snapshot(
                external_source_id,
                "duplicate_external_job_id",
                "duplicate job IDs",
                200,
                self._request_metadata(
                    url=url,
                    attempts=total_attempts,
                    pages_requested=pages_requested,
                ),
            )
        return SourceSnapshot(
            provider=self.provider,
            external_source_id=external_source_id,
            adapter_version=self.adapter_version,
            is_complete=True,
            jobs=jobs,
            http_status=200,
            request_metadata=self._request_metadata(
                url=url,
                attempts=total_attempts,
                pages_requested=pages_requested,
            ),
        )

    def _request_metadata(
        self,
        *,
        url: str,
        attempts: int,
        pages_requested: int,
    ) -> dict[str, Any]:
        return {
            "url": url,
            "attempts": attempts,
            "pages_requested": pages_requested,
            "page_size": self._page_size,
            "request_method": "GET",
            "user_agent": self.user_agent,
            "accept": self.request_headers["Accept"],
        }

    def _failure_snapshot(
        self,
        external_source_id: str,
        kind: str,
        message: str | None = None,
        http_status: int | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> SourceSnapshot:
        return SourceSnapshot(
            provider=self.provider,
            external_source_id=external_source_id,
            adapter_version=self.adapter_version,
            is_complete=False,
            http_status=http_status,
            errors=[{"kind": kind, "message": message or kind}],
            request_metadata=request_metadata or {},
        )

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            try:
                parsed = float(retry_after)
            except ValueError:
                parsed = 0.0
            if 0 < parsed <= 60:
                return parsed
        return float(2 ** (attempt - 1))


def normalize_lever_job(payload: Any) -> NormalizedJob:
    if not isinstance(payload, dict):
        raise ValueError("job is not an object")
    raw_id = payload.get("id")
    title = _optional_string(payload.get("text"))
    if raw_id in (None, "") or title is None:
        raise ValueError("job requires id and title")

    categories = payload.get("categories")
    categories = categories if isinstance(categories, dict) else {}
    locations = _lever_locations(categories)
    location = " / ".join(locations) or None
    department = _joined_distinct(
        [
            _optional_string(categories.get("department")),
            _optional_string(categories.get("team")),
        ]
    )
    employment_type = _optional_string(categories.get("commitment"))
    description_html = _lever_description_html(payload)
    description_text = _lever_description_text(payload)
    posting_url = _optional_string(payload.get("hostedUrl"))
    apply_url = _optional_string(payload.get("applyUrl"))
    structured_evidence = lever_structured_evidence(payload)
    content = {
        "title": title,
        "description_text": description_text,
        "location": location,
        "department": department,
        "employment_type": employment_type,
        "posting_url": posting_url,
        "apply_url": apply_url,
        "salary_range": payload.get("salaryRange"),
        "salary_description": _optional_string(payload.get("salaryDescriptionPlain")),
        "structured_evidence": structured_evidence,
    }
    return NormalizedJob(
        external_job_id=str(raw_id),
        title=title,
        posting_url=posting_url,
        apply_url=apply_url,
        location=location,
        department=department,
        employment_type=employment_type,
        description_html=description_html,
        description_text=description_text,
        source_published_at=_parse_epoch_milliseconds(payload.get("createdAt")),
        content_hash=hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        structured_evidence=structured_evidence,
        raw_payload=payload,
    )


def lever_structured_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    categories = payload.get("categories")
    categories = categories if isinstance(categories, dict) else {}
    locations = _lever_locations(categories)
    country = _optional_string(payload.get("country"))
    primary_location = _compact_mapping(
        label=locations[0] if locations else None,
        country=country,
    )
    secondary_locations = [{"label": location} for location in locations[1:]]
    workplace_type = _workplace_type(payload.get("workplaceType"))
    is_remote: bool | None = None
    if workplace_type == "remote":
        is_remote = True
    elif workplace_type == "on_site":
        is_remote = False

    metadata = _canonical_records(
        _compact_mapping(name=name, value=_optional_string(categories.get(key)))
        for name, key in (
            ("Commitment", "commitment"),
            ("Department", "department"),
            ("Level", "level"),
            ("Team", "team"),
        )
    )
    return {
        "schema_version": 1,
        "provider": "lever",
        "requisition_id": None,
        "workplace": _compact_mapping(type=workplace_type, is_remote=is_remote),
        "primary_location": primary_location or None,
        "secondary_locations": _canonical_records(secondary_locations),
        "offices": [],
        "countries": [country] if country else [],
        "provider_metadata": metadata,
        # Lever's country and locations describe the posting, not applicant eligibility.
        "eligibility_signals": [],
        "application": _compact_mapping(
            is_listed=True,
            posting_url=_optional_string(payload.get("hostedUrl")),
            apply_url=_optional_string(payload.get("applyUrl")),
        ),
    }


def _parse_external_source_id(value: str) -> tuple[str, str] | None:
    normalized = value.strip().lower()
    if normalized.startswith("eu:"):
        instance = "eu"
        site = normalized[3:]
    else:
        instance = "global"
        site = normalized
    if not _SITE_RE.fullmatch(site):
        return None
    return instance, site


def _lever_locations(categories: dict[str, Any]) -> list[str]:
    values = [_optional_string(categories.get("location"))]
    all_locations = categories.get("allLocations")
    if isinstance(all_locations, list):
        values.extend(_optional_string(value) for value in all_locations)
    return _distinct_strings(values)


def _lever_description_html(payload: dict[str, Any]) -> str | None:
    parts = [_optional_string(payload.get("description"))]
    lists = payload.get("lists")
    if isinstance(lists, list):
        for item in lists:
            if not isinstance(item, dict):
                continue
            heading = _optional_string(item.get("text"))
            content = _optional_string(item.get("content"))
            if heading:
                parts.append(f"<h3>{html.escape(heading)}</h3>")
            if content:
                parts.append(f"<ul>{content}</ul>")
    parts.extend(
        [
            _optional_string(payload.get("additional")),
            _optional_string(payload.get("salaryDescription")),
        ]
    )
    return "\n".join(part for part in parts if part) or None


def _lever_description_text(payload: dict[str, Any]) -> str | None:
    parts = [_optional_string(payload.get("descriptionPlain"))]
    lists = payload.get("lists")
    if isinstance(lists, list):
        for item in lists:
            if not isinstance(item, dict):
                continue
            heading = _optional_string(item.get("text"))
            content = _clean_text(_optional_string(item.get("content")))
            parts.append(" ".join(part for part in (heading, content) if part) or None)
    parts.extend(
        [
            _optional_string(payload.get("additionalPlain")),
            _optional_string(payload.get("salaryDescriptionPlain")),
        ]
    )
    return " ".join(part for part in parts if part) or None


def _workplace_type(value: Any) -> str | None:
    raw = _optional_string(value)
    if raw is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
    return {"onsite": "on_site", "on_site": "on_site"}.get(normalized, normalized) or None


def _parse_epoch_milliseconds(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    milliseconds = float(value)
    if not math.isfinite(milliseconds) or milliseconds < _MIN_REASONABLE_CREATED_AT_MS:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(html.unescape(_TAG_RE.sub(" ", value)).split()) or None


def _joined_distinct(values: list[str | None]) -> str | None:
    result = _distinct_strings(values)
    return " / ".join(result) or None


def _distinct_strings(values: Any) -> list[str]:
    by_casefold: dict[str, str] = {}
    for value in values:
        normalized = _optional_string(value)
        if normalized:
            by_casefold.setdefault(normalized.casefold(), normalized)
    return list(by_casefold.values())


def _compact_mapping(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _canonical_records(values: Any) -> list[dict[str, Any]]:
    by_json: dict[str, dict[str, Any]] = {}
    for value in values:
        if not value:
            continue
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        by_json[encoded] = value
    return [by_json[key] for key in sorted(by_json)]


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None
