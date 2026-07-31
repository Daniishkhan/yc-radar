from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TAG_RE = re.compile(r"<[^>]+>")
_GREENHOUSE_HOSTS = frozenset(
    {
        "boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
        "boards-api.greenhouse.io",
    }
)


class GreenhouseAdapter:
    """Read-only client for Greenhouse's unauthenticated public job-board API."""

    provider = "greenhouse"
    adapter_version = "2"
    source_kind = "ats_board"
    user_agent = (
        "yc-radar/0.2 (+https://github.com/Daniishkhan/yc-radar; "
        "read-only-public-job-sync)"
    )
    request_headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.8",
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

    def extract_board_token(self, url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or host not in _GREENHOUSE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query, keep_blank_values=True)
        candidate: str | None = None
        if host == "boards-api.greenhouse.io":
            if len(parts) >= 3 and parts[:2] == ["v1", "boards"]:
                candidate = parts[2]
        elif parts[:2] in (["embed", "job_board"], ["embed", "job_app"]):
            is_job_board_script = parts[:2] == ["embed", "job_board"] and parts[2:] == ["js"]
            if len(parts) != 2 and not is_job_board_script:
                return None
            values = query.get("for", [])
            if len(values) == 1:
                candidate = unquote(values[0])
        elif parts and parts[0] != "embed":
            candidate = parts[0]
        if not candidate or "/" in candidate or not _TOKEN_RE.fullmatch(candidate):
            return None
        return candidate.lower()

    def extract_source_id(self, url: str) -> str | None:
        """Return the stable board identity required by the provider registry."""
        return self.extract_board_token(url)

    def canonical_source_url(self, external_source_id: str) -> str:
        if not _TOKEN_RE.fullmatch(external_source_id):
            raise ValueError("invalid Greenhouse board token")
        token = external_source_id.lower()
        return f"https://job-boards.greenhouse.io/{quote(token, safe='')}"

    async def fetch_snapshot(self, external_source_id: str) -> SourceSnapshot:
        token = external_source_id.lower() if _TOKEN_RE.fullmatch(external_source_id) else None
        if token is None:
            return self._failure_snapshot(external_source_id, "invalid_token")
        url = f"https://boards-api.greenhouse.io/v1/boards/{quote(token, safe='')}/jobs"
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            headers=self.request_headers,
            follow_redirects=False,
        )
        try:
            return await self._fetch_with_client(client, token, url)
        except httpx.HTTPError as exc:
            return self._failure_snapshot(token, type(exc).__name__, message=str(exc))
        finally:
            if own_client:
                await client.aclose()

    async def _fetch_with_client(
        self,
        client: httpx.AsyncClient,
        token: str,
        url: str,
    ) -> SourceSnapshot:
        response: httpx.Response | None = None
        transport_error: httpx.TransportError | None = None
        attempts = 0
        for attempts in range(1, 5):
            try:
                response = await client.get(
                    url,
                    params={"content": "true"},
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
                token,
                type(transport_error).__name__,
                str(transport_error),
                request_metadata=metadata,
            )
        if response.status_code != 200:
            return SourceSnapshot(
                provider=self.provider,
                external_source_id=token,
                adapter_version=self.adapter_version,
                is_complete=False,
                http_status=response.status_code,
                errors=[{"kind": "http_status", "message": str(response.status_code)}],
                request_metadata=metadata,
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            return self._failure_snapshot(token, "invalid_json", str(exc), 200, metadata)
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            return self._failure_snapshot(token, "invalid_wrapper", "expected jobs list", 200, metadata)
        try:
            jobs = [normalize_greenhouse_job(raw) for raw in payload["jobs"]]
        except ValueError as exc:
            return self._failure_snapshot(token, "invalid_job", str(exc), 200, metadata)
        external_ids = [job.external_job_id for job in jobs]
        if len(external_ids) != len(set(external_ids)):
            return self._failure_snapshot(token, "duplicate_external_job_id", "duplicate job IDs", 200, metadata)
        return SourceSnapshot(
            provider=self.provider,
            external_source_id=token,
            adapter_version=self.adapter_version,
            is_complete=True,
            jobs=jobs,
            http_status=200,
            request_metadata=metadata,
        )

    def _failure_snapshot(
        self,
        token: str,
        kind: str,
        message: str | None = None,
        http_status: int | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> SourceSnapshot:
        return SourceSnapshot(
            provider=self.provider,
            external_source_id=token,
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


def normalize_greenhouse_job(payload: Any) -> NormalizedJob:
    if not isinstance(payload, dict):
        raise ValueError("job is not an object")
    raw_id = payload.get("id")
    title = str(payload.get("title") or "").strip()
    if raw_id in (None, "") or not title:
        raise ValueError("job requires id and title")
    description_html = _optional_string(payload.get("content"))
    location = _location_name(payload.get("location"))
    department = _joined_names(payload.get("departments"))
    if not department:
        department = _joined_names(payload.get("offices"))
    posting_url = _optional_string(payload.get("absolute_url"))
    content = {
        "title": title,
        "description_text": _clean_text(description_html),
        "location": location,
        "department": department,
        "employment_type": None,
        "posting_url": posting_url,
        "apply_url": None,
    }
    return NormalizedJob(
        external_job_id=str(raw_id),
        title=title,
        posting_url=posting_url,
        location=location,
        department=department,
        description_html=description_html,
        description_text=content["description_text"],
        source_published_at=_parse_timestamp(payload.get("first_published")),
        source_updated_at=_parse_timestamp(payload.get("updated_at")),
        content_hash=hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        raw_payload=payload,
    )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _location_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _optional_string(value.get("name"))
    return _optional_string(value)


def _joined_names(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    names = [
        _optional_string(item.get("name"))
        for item in value
        if isinstance(item, dict) and _optional_string(item.get("name"))
    ]
    return " / ".join(name for name in names if name) or None


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(html.unescape(_TAG_RE.sub(" ", value)).split()) or None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
