from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot

_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ASHBY_BOARD_HOST = "jobs.ashbyhq.com"
_ASHBY_API_HOST = "api.ashbyhq.com"


class AshbyAdapter:
    """Read-only client for Ashby's public lightweight job-posting API."""

    provider = "ashby"
    adapter_version = "1"
    source_kind = "ats_board"
    user_agent = (
        "yc-radar/0.2 (+https://github.com/Daniishkhan/yc-radar; "
        "read-only-public-job-sync)"
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
    ) -> None:
        self._client = client
        self._sleeper = sleeper
        self._timeout_seconds = timeout_seconds

    def extract_source_id(self, url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or host not in {_ASHBY_BOARD_HOST, _ASHBY_API_HOST}
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        candidate: str | None = None
        if host == _ASHBY_BOARD_HOST and parts:
            candidate = parts[0]
        elif host == _ASHBY_API_HOST and len(parts) >= 3:
            if parts[:2] == ["posting-api", "job-board"]:
                candidate = parts[2]
        if not candidate or "/" in candidate or not _SOURCE_ID_RE.fullmatch(candidate):
            return None
        return candidate

    def canonical_source_url(self, external_source_id: str) -> str:
        if not _SOURCE_ID_RE.fullmatch(external_source_id):
            raise ValueError("invalid Ashby job-board name")
        return f"https://jobs.ashbyhq.com/{quote(external_source_id, safe='')}"

    async def fetch_snapshot(self, external_source_id: str) -> SourceSnapshot:
        source_id = external_source_id if _SOURCE_ID_RE.fullmatch(external_source_id) else None
        if source_id is None:
            return self._failure_snapshot(external_source_id, "invalid_source_id")
        url = (
            "https://api.ashbyhq.com/posting-api/job-board/"
            f"{quote(source_id, safe='')}"
        )
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            headers=self.request_headers,
            follow_redirects=False,
        )
        try:
            return await self._fetch_with_client(client, source_id, url)
        except httpx.HTTPError as exc:
            return self._failure_snapshot(source_id, type(exc).__name__, str(exc))
        finally:
            if own_client:
                await client.aclose()

    async def _fetch_with_client(
        self,
        client: httpx.AsyncClient,
        source_id: str,
        url: str,
    ) -> SourceSnapshot:
        response: httpx.Response | None = None
        transport_error: httpx.TransportError | None = None
        attempts = 0
        for attempts in range(1, 5):
            try:
                response = await client.get(
                    url,
                    params={"includeCompensation": "true"},
                    headers=self.request_headers,
                )
            except httpx.TransportError as exc:
                transport_error = exc
                if attempts < 4:
                    await self._sleeper(self._retry_delay(None, attempts))
                continue
            if response.status_code != 429 and response.status_code < 500:
                break
            if attempts < 4:
                await self._sleeper(self._retry_delay(response, attempts))
        metadata = {
            "url": url,
            "attempts": attempts,
            "request_method": "GET",
            "user_agent": self.user_agent,
            "accept": self.request_headers["Accept"],
        }
        if response is None:
            assert transport_error is not None
            return self._failure_snapshot(
                source_id,
                type(transport_error).__name__,
                str(transport_error),
                request_metadata=metadata,
            )
        if response.status_code != 200:
            return self._failure_snapshot(
                source_id,
                "http_status",
                str(response.status_code),
                response.status_code,
                metadata,
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            return self._failure_snapshot(source_id, "invalid_json", str(exc), 200, metadata)
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            return self._failure_snapshot(
                source_id,
                "invalid_wrapper",
                "expected jobs list",
                200,
                metadata,
            )
        try:
            jobs = [
                normalize_ashby_job(raw)
                for raw in payload["jobs"]
                if isinstance(raw, dict) and raw.get("isListed") is not False
            ]
        except ValueError as exc:
            return self._failure_snapshot(source_id, "invalid_job", str(exc), 200, metadata)
        external_ids = [job.external_job_id for job in jobs]
        if len(external_ids) != len(set(external_ids)):
            return self._failure_snapshot(
                source_id,
                "duplicate_external_job_id",
                "duplicate job IDs",
                200,
                metadata,
            )
        return SourceSnapshot(
            provider=self.provider,
            external_source_id=source_id,
            adapter_version=self.adapter_version,
            is_complete=True,
            jobs=jobs,
            http_status=200,
            request_metadata=metadata,
        )

    def _failure_snapshot(
        self,
        source_id: str,
        kind: str,
        message: str | None = None,
        http_status: int | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> SourceSnapshot:
        return SourceSnapshot(
            provider=self.provider,
            external_source_id=source_id,
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


def normalize_ashby_job(payload: Any) -> NormalizedJob:
    if not isinstance(payload, dict):
        raise ValueError("job is not an object")
    raw_id = payload.get("id")
    title = _optional_string(payload.get("title"))
    if raw_id in (None, "") or title is None:
        raise ValueError("job requires id and title")
    location = _ashby_location(payload)
    department = _joined_distinct(
        [_optional_string(payload.get("department")), _optional_string(payload.get("team"))]
    )
    description_html = _optional_string(payload.get("descriptionHtml"))
    description_text = _optional_string(payload.get("descriptionPlain"))
    posting_url = _optional_string(payload.get("jobUrl"))
    apply_url = _optional_string(payload.get("applyUrl"))
    employment_type = _optional_string(payload.get("employmentType"))
    content = {
        "title": title,
        "description_text": description_text,
        "location": location,
        "department": department,
        "employment_type": employment_type,
        "posting_url": posting_url,
        "apply_url": apply_url,
        "compensation": payload.get("compensation"),
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
        source_published_at=_parse_timestamp(payload.get("publishedAt")),
        content_hash=hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        raw_payload=payload,
    )


def _ashby_location(payload: dict[str, Any]) -> str | None:
    locations = [_optional_string(payload.get("location"))]
    secondary = payload.get("secondaryLocations")
    if isinstance(secondary, list):
        locations.extend(
            _optional_string(item.get("location"))
            for item in secondary
            if isinstance(item, dict)
        )
    return _joined_distinct(locations)


def _joined_distinct(values: list[str | None]) -> str | None:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return " / ".join(result) or None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
