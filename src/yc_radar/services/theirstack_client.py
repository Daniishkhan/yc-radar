"""Credit-safe synchronous client for the small TheirStack API surface we use."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


_API_ROOT = "https://api.theirstack.com"
_CREDIT_BALANCE_URL = f"{_API_ROOT}/v0/billing/credit-balance"
_JOB_SEARCH_URL = f"{_API_ROOT}/v1/jobs/search"
_CACHE_SCHEMA_VERSION = 1
_MAX_ATTEMPTS = 4
_MAX_RETRY_AFTER_SECONDS = 300.0
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


class TheirStackApiError(RuntimeError):
    """A bounded TheirStack request, response, or safety validation failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        request_hash: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.request_hash = request_hash


@dataclass(frozen=True)
class CreditBalance:
    api_credits: int
    used_api_credits: int

    @property
    def remaining(self) -> int:
        """Credits still available in the UI's ``used / allocation`` accounting model."""
        return max(0, self.api_credits - self.used_api_credits)


@dataclass(frozen=True)
class SearchResult:
    payload: dict[str, Any]
    request_hash: str
    cache_source: str


class TheirStackRequestCache:
    """Atomic JSON response cache keyed by the complete request identity."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.entries_dir = self.root / "entries"

    @staticmethod
    def request_hash(
        method: str,
        url: str,
        body: Mapping[str, Any] | None = None,
    ) -> str:
        canonical_body = _canonical_json(body)
        identity = f"{method.strip().upper()}\0{url}\0{canonical_body}".encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    key_for = request_hash

    def load(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any] | None = None,
        *,
        max_age_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        if max_age_seconds is not None and max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative or None")
        request_hash = self.request_hash(method, url, body)
        path = self._entry_path(request_hash)
        try:
            if (
                max_age_seconds is not None
                and time.time() - path.stat().st_mtime > max_age_seconds
            ):
                return None
            entry = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(entry, dict)
                or entry.get("schema_version") != _CACHE_SCHEMA_VERSION
                or entry.get("request_hash") != request_hash
            ):
                return None
            payload = entry.get("payload")
            return dict(payload) if isinstance(payload, dict) else None
        except (FileNotFoundError, OSError, UnicodeError, ValueError, TypeError):
            return None

    def store(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any] | None,
        payload: Mapping[str, Any],
    ) -> str:
        if _contains_authorization_field(payload):
            raise ValueError("refusing to cache an authorization-bearing payload")
        request_hash = self.request_hash(method, url, body)
        entry = {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "request_hash": request_hash,
            "method": method.strip().upper(),
            "url": url,
            "body_hash": hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest(),
            "payload": dict(payload),
        }
        self._atomic_write_json(self._entry_path(request_hash), entry)
        return request_hash

    def _entry_path(self, request_hash: str) -> Path:
        return self.entries_dir / request_hash[:2] / f"{request_hash}.json"

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
        encoded = (_canonical_json(payload) + "\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            try:
                directory = os.open(path.parent, os.O_DIRECTORY)
            except (AttributeError, OSError):
                return
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


class TheirStackClient:
    """Minimal synchronous TheirStack client with explicit paid-search consent."""

    def __init__(
        self,
        api_key: str,
        cache: TheirStackRequestCache,
        *,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 20.0,
    ) -> None:
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ValueError("TheirStack API key is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._api_key = normalized_key
        self.cache = cache
        self._client = client
        self._sleeper = sleeper
        self._timeout_seconds = timeout_seconds

    def credit_balance(self) -> CreditBalance:
        response = self._request("GET", _CREDIT_BALANCE_URL, body=None)
        payload = self._json_object(response, operation="credit balance")
        api_credits = _non_negative_int(payload.get("api_credits"), field="api_credits")
        used_api_credits = _non_negative_int(
            payload.get("used_api_credits"),
            field="used_api_credits",
        )
        return CreditBalance(
            api_credits=api_credits,
            used_api_credits=used_api_credits,
        )

    def search(
        self,
        body: Mapping[str, Any],
        *,
        allow_paid: bool = False,
        cache_max_age_seconds: float | None = None,
        force_refresh: bool = False,
    ) -> SearchResult:
        request_body = dict(body)
        _validate_search_page(request_body)
        request_hash = self.cache.request_hash("POST", _JOB_SEARCH_URL, request_body)
        if request_body.get("blur_company_data") is not True and allow_paid is not True:
            raise TheirStackApiError(
                "paid TheirStack search requires allow_paid=True; use "
                "blur_company_data=true for preview mode",
                request_hash=request_hash,
            )

        cached = None
        if not force_refresh:
            cached = self.cache.load(
                "POST",
                _JOB_SEARCH_URL,
                request_body,
                max_age_seconds=cache_max_age_seconds,
            )
        if cached is not None:
            self._validate_search_payload(cached, request_hash=request_hash)
            return SearchResult(
                payload=cached,
                request_hash=request_hash,
                cache_source="disk",
            )

        response = self._request(
            "POST",
            _JOB_SEARCH_URL,
            body=request_body,
            request_hash=request_hash,
        )
        payload = self._json_object(
            response,
            operation="job search",
            request_hash=request_hash,
        )
        self._validate_search_payload(payload, request_hash=request_hash)
        if self._api_key in _canonical_json(payload):
            raise TheirStackApiError(
                "TheirStack response unexpectedly contained the request credential",
                request_hash=request_hash,
            )
        try:
            stored_hash = self.cache.store("POST", _JOB_SEARCH_URL, request_body, payload)
        except (OSError, TypeError, ValueError) as exc:
            raise TheirStackApiError(
                "TheirStack response cache write failed",
                request_hash=request_hash,
            ) from exc
        if stored_hash != request_hash:
            raise TheirStackApiError(
                "TheirStack response cache identity changed unexpectedly",
                request_hash=request_hash,
            )
        return SearchResult(
            payload=payload,
            request_hash=request_hash,
            cache_source="network",
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, Any] | None,
        request_hash: str | None = None,
    ) -> httpx.Response:
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=httpx.Timeout(self._timeout_seconds))
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    response = client.request(
                        method,
                        url,
                        headers=headers,
                        json=dict(body) if body is not None else None,
                    )
                except httpx.HTTPError as exc:
                    raise TheirStackApiError(
                        f"TheirStack transport error: {self._redact(str(exc))}",
                        retryable=True,
                        request_hash=request_hash,
                    ) from None
                if response.status_code != 429:
                    break
                if attempt == _MAX_ATTEMPTS:
                    raise TheirStackApiError(
                        "TheirStack API remained rate limited after bounded retries",
                        status_code=429,
                        retryable=True,
                        request_hash=request_hash,
                    )
                self._sleeper(_retry_after_seconds(response, attempt))
            else:  # pragma: no cover - range is statically non-empty
                raise AssertionError("unreachable request loop")

            if response.status_code != 200:
                summary = self._response_error_summary(response)
                suffix = f": {summary}" if summary else ""
                raise TheirStackApiError(
                    f"TheirStack API returned HTTP {response.status_code}{suffix}",
                    status_code=response.status_code,
                    retryable=response.status_code >= 500,
                    request_hash=request_hash,
                )
            return response
        finally:
            if owns_client:
                client.close()

    def _json_object(
        self,
        response: httpx.Response,
        *,
        operation: str,
        request_hash: str | None = None,
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise TheirStackApiError(
                f"TheirStack {operation} returned invalid JSON",
                status_code=response.status_code,
                request_hash=request_hash,
            ) from exc
        if not isinstance(payload, dict):
            raise TheirStackApiError(
                f"TheirStack {operation} response must be a JSON object",
                status_code=response.status_code,
                request_hash=request_hash,
            )
        return dict(payload)

    @staticmethod
    def _validate_search_payload(
        payload: Mapping[str, Any],
        *,
        request_hash: str,
    ) -> None:
        if not isinstance(payload.get("data"), list):
            raise TheirStackApiError(
                "TheirStack job search response must contain a data list",
                status_code=200,
                request_hash=request_hash,
            )
        if not isinstance(payload.get("metadata"), dict):
            raise TheirStackApiError(
                "TheirStack job search response must contain a metadata object",
                status_code=200,
                request_hash=request_hash,
            )

    def _response_error_summary(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        if isinstance(error, dict):
            parts = [error.get(name) for name in ("code", "title", "description")]
            summary = " - ".join(str(part) for part in parts if part not in (None, ""))
        elif isinstance(error, str):
            summary = error
        else:
            summary = ""
        return self._redact(summary)[:500]

    def _redact(self, value: str) -> str:
        redacted = value.replace(self._api_key, "[REDACTED]")
        return _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _validate_search_page(body: Mapping[str, Any]) -> None:
    limit = body.get("limit", 25)
    page = body.get("page", 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
        raise ValueError("TheirStack search limit must be an integer between 1 and 25")
    if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= 4:
        raise ValueError("TheirStack search page must be an integer between 0 and 4")


def _non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TheirStackApiError(f"TheirStack credit balance has invalid {field}")
    return value


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After")
    try:
        seconds = float(raw) if raw is not None else float(2 ** (attempt - 1))
    except ValueError:
        seconds = float(2 ** (attempt - 1))
    return min(_MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))


def _contains_authorization_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() == "authorization" or _contains_authorization_field(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_authorization_field(item) for item in value)
    return False
