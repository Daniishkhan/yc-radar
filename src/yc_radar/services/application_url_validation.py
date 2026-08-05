"""Bounded, cached, sequential liveness checks for public job URLs."""

from __future__ import annotations

import ipaddress
import socket
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from yc_radar.services.http_cache import DiskHttpCache

APPLICATION_URL_CACHE_KIND = "application_url_validation_v1"
APPLICATION_URL_CACHE_PREFIX = f"{APPLICATION_URL_CACHE_KIND}:"
APPLICATION_URL_FIELDS = ("application_url", "apply_url", "posting_url")
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
DEAD_STATUSES = frozenset({404, 410})
BLOCKED_STATUSES = frozenset({401, 403, 407, 451})
BLOCKED_HOST_SUFFIXES = (".internal", ".local", ".localhost")


class UnsafePublicUrl(ValueError):
    """Raised when a target is not an ordinary public HTTP(S) URL."""


class DnsResolutionError(RuntimeError):
    """Raised when a public hostname cannot currently be resolved."""


@dataclass(frozen=True)
class UrlValidationResult:
    requested_url: str
    normalized_url: str | None
    final_url: str | None
    outcome: str
    status_code: int | None
    checked_at: str
    attempt_count: int
    redirect_count: int
    cache_source: str
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ProbeResponse:
    status_code: int
    url: str
    location: str | None
    retry_after: str | None


HostResolver = Callable[[str, int], Sequence[str]]


class ApplicationUrlValidator:
    """Validate one URL at a time with bounded redirects, retries, and cache TTLs."""

    request_headers = {
        "User-Agent": (
            "yc-radar/0.2 (+https://github.com/Daniishkhan/yc-radar; "
            "read-only-application-url-check)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.8,*/*;q=0.5",
    }

    def __init__(
        self,
        cache: DiskHttpCache,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        max_redirects: int = 5,
        request_delay_seconds: float = 1.0,
        max_retry_delay_seconds: float = 30.0,
        positive_cache_ttl_seconds: float = 86_400.0,
        negative_cache_ttl_seconds: float = 21_600.0,
        transient_cache_ttl_seconds: float = 900.0,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] | None = None,
        resolver: HostResolver | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        if request_delay_seconds < 0 or max_retry_delay_seconds < 0:
            raise ValueError("request delays must be non-negative")
        if min(
            positive_cache_ttl_seconds,
            negative_cache_ttl_seconds,
            transient_cache_ttl_seconds,
        ) < 0:
            raise ValueError("cache TTLs must be non-negative")

        self.cache = cache
        self._client = client
        self._owns_client = client is None
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_redirects = max_redirects
        self.request_delay_seconds = request_delay_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.positive_cache_ttl_seconds = positive_cache_ttl_seconds
        self.negative_cache_ttl_seconds = negative_cache_ttl_seconds
        self.transient_cache_ttl_seconds = transient_cache_ttl_seconds
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._clock = clock or (lambda: datetime.now(UTC))
        self._resolver = resolver or resolve_host_addresses
        self._last_request_at: float | None = None
        self._resolved_hosts: dict[tuple[str, int], tuple[str, ...]] = {}
        self.network_request_count = 0

    def validate(self, raw_url: str, *, refresh: bool = False) -> UrlValidationResult:
        checked_at = _as_utc(self._clock())
        try:
            normalized_url = normalize_public_http_url(raw_url)
        except UnsafePublicUrl as exc:
            return UrlValidationResult(
                requested_url=str(raw_url),
                normalized_url=None,
                final_url=None,
                outcome="invalid",
                status_code=None,
                checked_at=checked_at.isoformat(),
                attempt_count=0,
                redirect_count=0,
                cache_source="none",
                error=str(exc),
            )

        if not refresh:
            cached = self._load_cached(normalized_url, now=checked_at)
            if cached is not None:
                return cached

        result = self._validate_network(normalized_url, checked_at=checked_at)
        self._store_cached(result, now=checked_at)
        return result

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> ApplicationUrlValidator:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def _validate_network(
        self,
        normalized_url: str,
        *,
        checked_at: datetime,
    ) -> UrlValidationResult:
        current_url = normalized_url
        visited: set[str] = set()
        total_attempts = 0
        redirect_count = 0

        while True:
            if current_url in visited:
                return self._result(
                    normalized_url,
                    current_url,
                    "unhealthy",
                    checked_at,
                    total_attempts,
                    redirect_count,
                    error="redirect_loop",
                )
            visited.add(current_url)

            try:
                self._ensure_public_target(current_url)
            except UnsafePublicUrl as exc:
                return self._result(
                    normalized_url,
                    current_url,
                    "invalid",
                    checked_at,
                    total_attempts,
                    redirect_count,
                    error=str(exc),
                )
            except DnsResolutionError as exc:
                return self._result(
                    normalized_url,
                    current_url,
                    "transient_error",
                    checked_at,
                    total_attempts,
                    redirect_count,
                    error=str(exc),
                )

            response, request_error, attempts = self._request_with_retries(current_url)
            total_attempts += attempts
            if response is None:
                return self._result(
                    normalized_url,
                    current_url,
                    "transient_error",
                    checked_at,
                    total_attempts,
                    redirect_count,
                    error=request_error or "request_failed",
                )

            if response.status_code in REDIRECT_STATUSES:
                if not response.location:
                    return self._result(
                        normalized_url,
                        current_url,
                        "unhealthy",
                        checked_at,
                        total_attempts,
                        redirect_count,
                        status_code=response.status_code,
                        error="redirect_missing_location",
                    )
                redirect_count += 1
                if redirect_count > self.max_redirects:
                    return self._result(
                        normalized_url,
                        current_url,
                        "unhealthy",
                        checked_at,
                        total_attempts,
                        redirect_count,
                        status_code=response.status_code,
                        error="too_many_redirects",
                    )
                try:
                    current_url = normalize_public_http_url(
                        urljoin(response.url, response.location)
                    )
                except UnsafePublicUrl as exc:
                    return self._result(
                        normalized_url,
                        current_url,
                        "invalid",
                        checked_at,
                        total_attempts,
                        redirect_count,
                        status_code=response.status_code,
                        error=f"unsafe_redirect:{exc}",
                    )
                continue

            outcome = classify_http_status(response.status_code)
            error = (
                f"http_status:{response.status_code}"
                if outcome not in {"live", "dead", "blocked"}
                else None
            )
            return self._result(
                normalized_url,
                response.url,
                outcome,
                checked_at,
                total_attempts,
                redirect_count,
                status_code=response.status_code,
                error=error,
            )

    def _request_with_retries(
        self,
        url: str,
    ) -> tuple[_ProbeResponse | None, str | None, int]:
        client = self._client
        if client is None:
            client = httpx.Client(
                headers=self.request_headers,
                follow_redirects=False,
            )
            self._client = client

        last_error: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._pace_request()
            try:
                with client.stream(
                    "GET",
                    url,
                    headers=self.request_headers,
                    follow_redirects=False,
                    timeout=httpx.Timeout(self.timeout_seconds),
                ) as response:
                    probe = _ProbeResponse(
                        status_code=response.status_code,
                        url=str(response.url),
                        location=response.headers.get("Location"),
                        retry_after=response.headers.get("Retry-After"),
                    )
            except httpx.RequestError as exc:
                self.network_request_count += 1
                self._last_request_at = self._monotonic()
                last_error = f"{type(exc).__name__}:{exc}"
                if attempt < self.max_attempts:
                    self._sleeper(self._retry_delay(None, attempt))
                    continue
                return None, last_error, attempt

            self.network_request_count += 1
            self._last_request_at = self._monotonic()
            if probe.status_code in RETRYABLE_STATUSES and attempt < self.max_attempts:
                self._sleeper(self._retry_delay(probe.retry_after, attempt))
                continue
            return probe, last_error, attempt
        return None, last_error or "request_failed", self.max_attempts

    def _ensure_public_target(self, url: str) -> None:
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            raise UnsafePublicUrl("missing_hostname")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            cache_key = (host, port)
            addresses = self._resolved_hosts.get(cache_key)
            if addresses is None:
                try:
                    addresses = tuple(self._resolver(host, port))
                except OSError as exc:
                    raise DnsResolutionError(
                        f"dns_resolution_failed:{type(exc).__name__}:{exc}"
                    ) from exc
                if not addresses:
                    raise DnsResolutionError("dns_resolution_failed:no_addresses")
                self._resolved_hosts[cache_key] = addresses
            parsed_addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
            try:
                parsed_addresses = [ipaddress.ip_address(value) for value in addresses]
            except ValueError as exc:
                raise DnsResolutionError("dns_resolution_failed:invalid_address") from exc
        else:
            parsed_addresses = [literal]

        if any(not address.is_global for address in parsed_addresses):
            raise UnsafePublicUrl("non_public_network_target")

    def _pace_request(self) -> None:
        if self._last_request_at is None or self.request_delay_seconds == 0:
            return
        remaining = self.request_delay_seconds - (self._monotonic() - self._last_request_at)
        if remaining > 0:
            self._sleeper(remaining)

    def _retry_delay(self, retry_after: str | None, attempt: int) -> float:
        parsed = parse_retry_after(retry_after, now=_as_utc(self._clock()))
        fallback = float(2 ** (attempt - 1))
        return min(self.max_retry_delay_seconds, parsed if parsed is not None else fallback)

    def _load_cached(
        self,
        normalized_url: str,
        *,
        now: datetime,
    ) -> UrlValidationResult | None:
        cached = self.cache.load(_cache_key(normalized_url), allow_retryable=True)
        if (
            cached is None
            or cached.get("cache_kind") != APPLICATION_URL_CACHE_KIND
            or _optional_float(cached.get("expires_at")) is None
            or float(cached["expires_at"]) <= now.timestamp()
            or not isinstance(cached.get("validation_result"), Mapping)
        ):
            return None
        try:
            result = UrlValidationResult(**dict(cached["validation_result"]))
        except (TypeError, ValueError):
            return None
        return replace(result, cache_source="disk")

    def _store_cached(self, result: UrlValidationResult, *, now: datetime) -> None:
        if result.normalized_url is None:
            return
        ttl = self.negative_cache_ttl_seconds
        if result.outcome == "live":
            ttl = self.positive_cache_ttl_seconds
        elif result.outcome == "transient_error":
            ttl = self.transient_cache_ttl_seconds
        self.cache.store(
            _cache_key(result.normalized_url),
            metadata={
                "cache_kind": APPLICATION_URL_CACHE_KIND,
                "expires_at": now.timestamp() + ttl,
                "retryable": False,
                "validation_result": result.as_dict(),
            },
            text="",
        )

    @staticmethod
    def _result(
        normalized_url: str,
        final_url: str | None,
        outcome: str,
        checked_at: datetime,
        attempt_count: int,
        redirect_count: int,
        *,
        status_code: int | None = None,
        error: str | None = None,
    ) -> UrlValidationResult:
        return UrlValidationResult(
            requested_url=normalized_url,
            normalized_url=normalized_url,
            final_url=final_url,
            outcome=outcome,
            status_code=status_code,
            checked_at=checked_at.isoformat(),
            attempt_count=attempt_count,
            redirect_count=redirect_count,
            cache_source="network",
            error=error,
        )


def validate_queue_rows(
    queues: Mapping[str, Sequence[Mapping[str, Any]]],
    validator: ApplicationUrlValidator,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """Validate each distinct selected URL once and retain one result per queue row."""
    validations: list[dict[str, Any]] = []
    batch_results: dict[str, UrlValidationResult] = {}
    batch_reuses = 0
    for queue_name, rows in queues.items():
        for input_index, row in enumerate(rows):
            selected = select_application_url(row)
            if selected is None:
                checked_at = _as_utc(validator._clock()).isoformat()
                result = UrlValidationResult(
                    requested_url="",
                    normalized_url=None,
                    final_url=None,
                    outcome="invalid",
                    status_code=None,
                    checked_at=checked_at,
                    attempt_count=0,
                    redirect_count=0,
                    cache_source="none",
                    error="missing_application_or_posting_url",
                )
                url_field = None
            else:
                url_field, raw_url = selected
                try:
                    batch_key = normalize_public_http_url(raw_url)
                except UnsafePublicUrl:
                    batch_key = raw_url.strip()
                if batch_key in batch_results:
                    result = replace(batch_results[batch_key], cache_source="batch")
                    batch_reuses += 1
                else:
                    result = validator.validate(raw_url, refresh=refresh)
                    batch_results[batch_key] = result

            validations.append(
                {
                    "queue": queue_name,
                    "input_index": input_index,
                    "job_key": _first_text(row, "job_key", "cluster_key"),
                    "provider": _first_text(row, "provider") or "unknown",
                    "external_job_id": _first_text(row, "external_job_id"),
                    "company_slug": _first_text(row, "company_slug"),
                    "company_name": _first_text(row, "company_name"),
                    "title": _first_text(row, "title", "representative_title"),
                    "url_field": url_field,
                    **result.as_dict(),
                }
            )

    outcomes = Counter(str(row["outcome"]) for row in validations)
    dead_denominator = outcomes["live"] + outcomes["dead"]
    return {
        "schema_version": 1,
        "generated_at": _as_utc(validator._clock()).isoformat(),
        "summary": {
            "queue_row_count": len(validations),
            "unique_selected_url_count": len(batch_results),
            "batch_reuse_count": batch_reuses,
            "network_request_count": validator.network_request_count,
            "cache": dict(validator.cache.metrics),
            "outcomes": dict(sorted(outcomes.items())),
            "dead_link_count": outcomes["dead"],
            "dead_link_rate_denominator": dead_denominator,
            "dead_link_rate": (
                round(outcomes["dead"] / dead_denominator, 6)
                if dead_denominator
                else None
            ),
        },
        "validations": validations,
    }


def select_application_url(row: Mapping[str, Any]) -> tuple[str, str] | None:
    for field in APPLICATION_URL_FIELDS:
        value = row.get(field)
        if value is not None and str(value).strip():
            return field, str(value).strip()
    return None


def classify_http_status(status_code: int) -> str:
    if 200 <= status_code < 300:
        return "live"
    if status_code in DEAD_STATUSES:
        return "dead"
    if status_code in BLOCKED_STATUSES:
        return "blocked"
    if status_code in RETRYABLE_STATUSES or status_code >= 500:
        return "transient_error"
    return "unhealthy"


def normalize_public_http_url(raw_url: str) -> str:
    value = str(raw_url).strip()
    if not value or len(value) > 8192:
        raise UnsafePublicUrl("missing_or_oversized_url")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise UnsafePublicUrl("url_contains_whitespace_or_control_character")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UnsafePublicUrl("malformed_url") from exc
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not host:
        raise UnsafePublicUrl("url_must_be_absolute_http_or_https")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafePublicUrl("embedded_credentials_are_not_allowed")
    if port not in {None, 80, 443}:
        raise UnsafePublicUrl("non_standard_port_is_not_allowed")
    if host == "localhost" or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafePublicUrl("local_hostname_is_not_allowed")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafePublicUrl("invalid_hostname") from exc
    try:
        literal = ipaddress.ip_address(ascii_host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise UnsafePublicUrl("non_public_network_target")

    rendered_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    default_port = 443 if scheme == "https" else 80
    netloc = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def resolve_host_addresses(host: str, port: int) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(sockaddr[0])
                for _family, _socket_type, _protocol, _canonical_name, sockaddr in socket.getaddrinfo(
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                )
            }
        )
    )


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        retry_at = _as_utc(retry_at)
        seconds = (retry_at - _as_utc(now)).total_seconds()
    return max(0.0, seconds)


def _cache_key(normalized_url: str) -> str:
    return f"{APPLICATION_URL_CACHE_PREFIX}{normalized_url}"


def _first_text(row: Mapping[str, Any], *fields: str) -> str | None:
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
