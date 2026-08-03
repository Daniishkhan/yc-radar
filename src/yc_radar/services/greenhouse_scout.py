from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlunparse

import httpx

from yc_radar.services.http_cache import DiskHttpCache
from yc_radar.services.source_providers import is_ats_domain

SCOUT_USER_AGENT = (
    "yc-radar/0.2 (+https://github.com/Daniishkhan/yc-radar; "
    "read-only-greenhouse-source-scout)"
)
SCOUT_HEADERS = {
    "User-Agent": SCOUT_USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.8",
}
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
# Homepage verification is deliberately much tighter than board synchronization. A
# candidate domain is supporting identity evidence, so one unhealthy website must not
# hold the sequential scout open for minutes. Four timeout phases share each request's
# remaining wall-clock budget, redirects are followed manually, and both redirects and
# retries count toward this fixed request ceiling.
HOMEPAGE_WALL_CLOCK_BUDGET_SECONDS = 20.0
HOMEPAGE_REQUEST_PHASE_TIMEOUT_SECONDS = 5.0
HOMEPAGE_MAX_REDIRECTS = 5
HOMEPAGE_MAX_RETRIES = 2
HOMEPAGE_MAX_REQUESTS = 8
# A checkpointed ``homepage_unverified`` row is final for that immutable run manifest.
# This persistent negative cache covers crashes before the next checkpoint, but expires
# so a temporary outage never becomes cross-run identity truth.
HOMEPAGE_NEGATIVE_CACHE_TTL_SECONDS = 24 * 60 * 60
HOMEPAGE_POSITIVE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
HOMEPAGE_CACHE_KIND = "greenhouse_homepage_verification_v1"
# Large employers can expose thousands of jobs even when Greenhouse omits job
# descriptions. Keep the response bounded, but high enough to validate those boards
# instead of silently parsing a truncated JSON document.
MAX_SCOUT_TEXT_CHARS = 10_000_000
COMMON_JOB_HOST_PREFIXES = frozenset({"apply", "career", "careers", "job", "jobs", "join"})
BLOCKED_COMPANY_HOST_SUFFIXES = frozenset(
    {
        "facebook.com",
        "github.com",
        "github.io",
        "glassdoor.com",
        "google.com",
        "indeed.com",
        "linkedin.com",
        "notion.site",
        "notion.so",
        "substack.com",
        "twitter.com",
        "webflow.io",
        "x.com",
    }
)


@dataclass(frozen=True)
class GreenhouseBoardEvidence:
    board_token: str
    verification_status: str
    http_status: int | None
    company_name: str | None
    job_count: int
    external_job_origins: tuple[str, ...]
    board_page_origin: str | None = None
    error: str | None = None
    cache_source: str = "network"
    attempt_count: int = 0


@dataclass(frozen=True)
class CompanyResolution:
    status: str
    company_id: int | None = None
    website_candidate: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _FetchResult:
    status_code: int | None
    final_url: str
    text: str
    error: str | None
    attempt_count: int
    cache_source: str
    truncated: bool = False


@dataclass(frozen=True)
class _HomepageProbeResult:
    verified_origin: str | None
    status_code: int | None
    final_url: str
    error: str | None
    request_count: int


class GreenhouseBoardScout:
    """Sequential, cached reader for the public Greenhouse jobs-list endpoint."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        delay_seconds: float = 1.0,
        timeout_seconds: float = 15.0,
        homepage_wall_clock_budget_seconds: float = HOMEPAGE_WALL_CLOCK_BUDGET_SECONDS,
        homepage_request_phase_timeout_seconds: float = (
            HOMEPAGE_REQUEST_PHASE_TIMEOUT_SECONDS
        ),
        homepage_negative_cache_ttl_seconds: float = (
            HOMEPAGE_NEGATIVE_CACHE_TTL_SECONDS
        ),
        homepage_positive_cache_ttl_seconds: float = (
            HOMEPAGE_POSITIVE_CACHE_TTL_SECONDS
        ),
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be zero or greater")
        if homepage_wall_clock_budget_seconds <= 0:
            raise ValueError("homepage wall-clock budget must be positive")
        if homepage_request_phase_timeout_seconds <= 0:
            raise ValueError("homepage request phase timeout must be positive")
        if homepage_negative_cache_ttl_seconds <= 0:
            raise ValueError("homepage negative cache TTL must be positive")
        if homepage_positive_cache_ttl_seconds <= 0:
            raise ValueError("homepage positive cache TTL must be positive")
        self.cache = DiskHttpCache(cache_dir)
        self._client = client
        self._sleeper = sleeper
        self._clock = clock
        self._wall_clock = wall_clock
        self._delay_seconds = delay_seconds
        self._timeout_seconds = timeout_seconds
        self._homepage_wall_clock_budget_seconds = homepage_wall_clock_budget_seconds
        self._homepage_request_phase_timeout_seconds = (
            homepage_request_phase_timeout_seconds
        )
        self._homepage_negative_cache_ttl_seconds = homepage_negative_cache_ttl_seconds
        self._homepage_positive_cache_ttl_seconds = homepage_positive_cache_ttl_seconds
        self._last_request_at: float | None = None

    def verify(self, board_token: str) -> GreenhouseBoardEvidence:
        board_token = board_token.lower()
        url = (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{quote(board_token, safe='')}/jobs?content=false"
        )
        fetched = self._get(url)
        if fetched.truncated:
            return GreenhouseBoardEvidence(
                board_token=board_token,
                verification_status="failed",
                http_status=fetched.status_code,
                company_name=None,
                job_count=0,
                external_job_origins=(),
                error=fetched.error or "response_too_large",
                cache_source=fetched.cache_source,
                attempt_count=fetched.attempt_count,
            )
        if fetched.status_code != 200:
            status = "not_found" if fetched.status_code in {404, 410} else "failed"
            return GreenhouseBoardEvidence(
                board_token=board_token,
                verification_status=status,
                http_status=fetched.status_code,
                company_name=None,
                job_count=0,
                external_job_origins=(),
                error=fetched.error or f"http_status:{fetched.status_code}",
                cache_source=fetched.cache_source,
                attempt_count=fetched.attempt_count,
            )
        try:
            payload = json.loads(fetched.text)
        except json.JSONDecodeError as exc:
            return GreenhouseBoardEvidence(
                board_token=board_token,
                verification_status="invalid",
                http_status=200,
                company_name=None,
                job_count=0,
                external_job_origins=(),
                error=f"invalid_json:{exc}",
                cache_source=fetched.cache_source,
                attempt_count=fetched.attempt_count,
            )
        evidence = analyze_greenhouse_jobs_payload(board_token, payload)
        return GreenhouseBoardEvidence(
            **{
                **evidence.__dict__,
                "http_status": 200,
                "cache_source": fetched.cache_source,
                "attempt_count": fetched.attempt_count,
            }
        )

    def enrich_from_board_page(
        self,
        evidence: GreenhouseBoardEvidence,
    ) -> GreenhouseBoardEvidence:
        """Use only the hosted board redirect or configured logo link as domain evidence."""
        if evidence.verification_status != "verified" or evidence.board_page_origin:
            return evidence
        url = f"https://job-boards.greenhouse.io/{quote(evidence.board_token, safe='')}"
        fetched = self._get(
            url,
            follow_redirects=True,
            headers={
                "User-Agent": SCOUT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )
        if fetched.status_code != 200:
            return replace(
                evidence,
                error=evidence.error or fetched.error or f"board_page_http:{fetched.status_code}",
            )
        redirected_origin = external_job_origin(fetched.final_url)
        if redirected_origin:
            return replace(
                evidence,
                board_page_origin=choose_company_website((redirected_origin,)),
            )
        parser = _LogoLinkParser()
        try:
            parser.feed(fetched.text)
        except ValueError:
            return replace(evidence, error=evidence.error or "invalid_board_page_html")
        origins = tuple(
            sorted(
                {
                    origin
                    for href in parser.hrefs
                    if (origin := external_job_origin(urljoin(fetched.final_url, href))) is not None
                }
            )
        )
        return replace(evidence, board_page_origin=choose_company_website(origins))

    def verify_homepage(self, url: str) -> str | None:
        """Return a safe canonical origin from a bounded, persistent verification.

        Positive results are cached for seven days by default. Negative results are
        cached for one day: long enough to make an immediate restart cheap, but never a
        permanent assertion that a company website is unavailable. The scout's atomic
        output checkpoint separately makes ``homepage_unverified`` final within one
        immutable input/run.
        """
        cache_key = _homepage_cache_key(url)
        cached = self.cache.load(cache_key, allow_retryable=True)
        now = self._wall_clock()
        if (
            cached is not None
            and cached.get("cache_kind") == HOMEPAGE_CACHE_KIND
            and (_optional_float(cached.get("expires_at")) or 0) > now
        ):
            return _optional_string(cached.get("verified_origin"))

        result = self._probe_homepage(url)
        ttl = (
            self._homepage_positive_cache_ttl_seconds
            if result.verified_origin
            else self._homepage_negative_cache_ttl_seconds
        )
        self.cache.store(
            cache_key,
            metadata={
                "cache_kind": HOMEPAGE_CACHE_KIND,
                "requested_homepage": url,
                "status_code": result.status_code,
                "final_url": result.final_url,
                "verified_origin": result.verified_origin,
                "error": result.error,
                "attempt_count": result.request_count,
                "checked_at": now,
                "expires_at": now + ttl,
                # DiskHttpCache normally excludes transient failures. Homepage
                # negatives use their explicit TTL instead so a restart cannot repeat
                # the same bounded-but-expensive probe before its next checkpoint.
                "retryable": False,
            },
            text="",
        )
        return result.verified_origin

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> GreenhouseBoardScout:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def _get(
        self,
        url: str,
        *,
        follow_redirects: bool = False,
        headers: Mapping[str, str] = SCOUT_HEADERS,
    ) -> _FetchResult:
        cached = self.cache.load(url)
        if cached is not None:
            return _FetchResult(
                status_code=_optional_int(cached.get("status_code")),
                final_url=str(cached.get("final_url") or url),
                text=str(cached.get("text") or ""),
                error=_optional_string(cached.get("error")),
                attempt_count=int(cached.get("attempt_count") or 0),
                cache_source="disk",
                truncated=bool(cached.get("truncated")),
            )

        client = self._client
        if client is None:
            client = httpx.Client(
                timeout=httpx.Timeout(self._timeout_seconds),
                headers=SCOUT_HEADERS,
                follow_redirects=False,
            )
            self._client = client

        response: httpx.Response | None = None
        error: str | None = None
        retryable_request_error = False
        attempts = 0
        for attempts in range(1, 5):
            self._pace_request()
            try:
                response = client.get(
                    url,
                    headers=dict(headers),
                    follow_redirects=follow_redirects,
                )
                self._last_request_at = self._clock()
            except httpx.RequestError as exc:
                self._last_request_at = self._clock()
                error = f"{type(exc).__name__}:{exc}"
                if not isinstance(exc, httpx.TransportError):
                    break
                retryable_request_error = True
                if attempts < 4:
                    self._sleeper(float(2 ** (attempts - 1)))
                continue
            if response.status_code not in RETRYABLE_STATUS_CODES:
                break
            error = f"http_status:{response.status_code}"
            if attempts < 4:
                self._sleeper(_retry_delay(response, attempts))

        status_code = response.status_code if response is not None else None
        response_text = response.text if response is not None else ""
        truncated = len(response_text) > MAX_SCOUT_TEXT_CHARS
        text = response_text[:MAX_SCOUT_TEXT_CHARS]
        if truncated:
            error = f"response_too_large:{len(response_text)}"
        final_url = str(response.url) if response is not None else url
        retryable = (
            status_code in RETRYABLE_STATUS_CODES or retryable_request_error or truncated
        )
        self.cache.store(
            url,
            metadata={
                "status_code": status_code,
                "final_url": final_url,
                "error": error,
                "attempt_count": attempts,
                "retryable": retryable,
                "truncated": truncated,
            },
            text=text,
        )
        return _FetchResult(
            status_code=status_code,
            final_url=final_url,
            text=text,
            error=error,
            attempt_count=attempts,
            cache_source="network",
            truncated=truncated,
        )

    def _probe_homepage(self, url: str) -> _HomepageProbeResult:
        parsed = _safe_urlparse(url)
        if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return _HomepageProbeResult(None, None, url, "invalid_homepage_url", 0)

        client = self._ensure_client()
        deadline = self._clock() + self._homepage_wall_clock_budget_seconds
        current_url = url
        status_code: int | None = None
        error: str | None = None
        request_count = 0
        redirect_count = 0
        retry_count = 0
        headers = {
            "User-Agent": SCOUT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.8",
        }

        while request_count < HOMEPAGE_MAX_REQUESTS:
            self._pace_request(deadline=deadline)
            remaining = deadline - self._clock()
            if remaining <= 0:
                error = "homepage_wall_clock_budget_exhausted"
                break
            # HTTPX applies a timeout to four independent phases (pool, connect,
            # write, read). Giving each at most one quarter of the remaining budget
            # bounds the whole request even when more than one phase times out.
            phase_timeout = max(
                0.001,
                min(self._homepage_request_phase_timeout_seconds, remaining / 4),
            )
            request_count += 1
            response: httpx.Response | None = None
            retry_delay: float | None = None
            try:
                with client.stream(
                    "GET",
                    current_url,
                    headers=headers,
                    follow_redirects=False,
                    timeout=httpx.Timeout(phase_timeout),
                ) as response:
                    status_code = response.status_code
                    location = response.headers.get("Location")
                    if status_code in RETRYABLE_STATUS_CODES:
                        error = f"http_status:{status_code}"
                        retry_delay = _retry_delay(response, retry_count + 1)
            except httpx.RequestError as exc:
                error = f"{type(exc).__name__}:{exc}"
                if isinstance(exc, httpx.TransportError):
                    retry_delay = float(2**retry_count)
            finally:
                self._last_request_at = self._clock()

            if response is None:
                if retry_delay is None:
                    break
            elif status_code in RETRYABLE_STATUS_CODES:
                pass
            elif status_code is not None and 300 <= status_code < 400:
                if not location:
                    error = f"redirect_without_location:{status_code}"
                    break
                redirect_count += 1
                if redirect_count > HOMEPAGE_MAX_REDIRECTS:
                    error = "homepage_redirect_limit_exhausted"
                    break
                redirected = urljoin(current_url, location)
                redirected_parsed = _safe_urlparse(redirected)
                if (
                    redirected_parsed is None
                    or redirected_parsed.scheme not in {"http", "https"}
                    or not redirected_parsed.hostname
                    or redirected_parsed.username is not None
                    or redirected_parsed.password is not None
                    or is_ats_domain(redirected_parsed.hostname)
                    or not domains_compatible(url, redirected_parsed.hostname)
                ):
                    error = "invalid_homepage_redirect"
                    break
                current_url = redirected
                continue
            elif status_code is not None and 200 <= status_code < 300:
                final = _safe_urlparse(current_url)
                host = ((final.hostname if final else None) or "").lower().rstrip(".")
                if (
                    final is None
                    or final.scheme not in {"http", "https"}
                    or not host
                    or final.username is not None
                    or final.password is not None
                    or is_ats_domain(host)
                    or not domains_compatible(url, host)
                ):
                    error = "homepage_identity_mismatch"
                    break
                origin = urlunparse((final.scheme, host, "", "", "", ""))
                return _HomepageProbeResult(
                    origin,
                    status_code,
                    current_url,
                    None,
                    request_count,
                )
            else:
                error = f"http_status:{status_code}"
                break

            if retry_delay is None or retry_count >= HOMEPAGE_MAX_RETRIES:
                break
            retry_count += 1
            remaining = deadline - self._clock()
            if retry_delay >= remaining:
                error = "homepage_wall_clock_budget_exhausted"
                break
            self._sleeper(retry_delay)

        if request_count >= HOMEPAGE_MAX_REQUESTS and error is None:
            error = "homepage_request_limit_exhausted"
        return _HomepageProbeResult(None, status_code, current_url, error, request_count)

    def _ensure_client(self) -> httpx.Client:
        client = self._client
        if client is None:
            client = httpx.Client(
                timeout=httpx.Timeout(self._timeout_seconds),
                headers=SCOUT_HEADERS,
                follow_redirects=False,
            )
            self._client = client
        return client

    def _pace_request(self, *, deadline: float | None = None) -> None:
        if self._last_request_at is None or not self._delay_seconds:
            return
        remaining = self._delay_seconds - (self._clock() - self._last_request_at)
        if remaining > 0:
            if deadline is not None:
                remaining = min(remaining, max(0.0, deadline - self._clock()))
            self._sleeper(remaining)


def analyze_greenhouse_jobs_payload(
    board_token: str,
    payload: Any,
) -> GreenhouseBoardEvidence:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        return _invalid_evidence(board_token, "expected_jobs_list")
    jobs = payload["jobs"]
    total = (payload.get("meta") or {}).get("total") if isinstance(payload.get("meta"), dict) else None
    if isinstance(total, int) and total != len(jobs):
        return _invalid_evidence(board_token, "incomplete_jobs_list")
    if not jobs:
        return GreenhouseBoardEvidence(
            board_token=board_token,
            verification_status="empty",
            http_status=200,
            company_name=None,
            job_count=0,
            external_job_origins=(),
        )
    if any(not isinstance(job, dict) for job in jobs):
        return _invalid_evidence(board_token, "job_is_not_an_object")
    job_ids = [_optional_string(job.get("id")) for job in jobs]
    if any(job_id is None for job_id in job_ids) or len(set(job_ids)) != len(job_ids):
        return _invalid_evidence(board_token, "invalid_or_duplicate_job_id")
    company_names = {
        name
        for job in jobs
        if (name := _optional_string(job.get("company_name"))) is not None
    }
    if len(company_names) != 1:
        return _invalid_evidence(board_token, "missing_or_ambiguous_company_name")
    origins = sorted(
        {
            origin
            for job in jobs
            if (origin := external_job_origin(_optional_string(job.get("absolute_url"))))
            is not None
        }
    )
    return GreenhouseBoardEvidence(
        board_token=board_token,
        verification_status="verified",
        http_status=200,
        company_name=next(iter(company_names)),
        job_count=len(jobs),
        external_job_origins=tuple(origins),
    )


def resolve_company(
    evidence: GreenhouseBoardEvidence,
    *,
    companies: list[Mapping[str, Any]],
    existing_source_company_id: int | None = None,
) -> CompanyResolution:
    if existing_source_company_id is not None:
        return CompanyResolution("already_registered", company_id=existing_source_company_id)
    if evidence.verification_status != "verified" or not evidence.company_name:
        return CompanyResolution(evidence.verification_status, reason=evidence.error)

    normalized_name = normalize_observed_name(evidence.company_name)
    name_matches = [
        company
        for company in companies
        if normalize_observed_name(str(company.get("name") or "")) == normalized_name
    ]
    origins = evidence.external_job_origins + (
        (evidence.board_page_origin,) if evidence.board_page_origin else ()
    )
    website_candidate = choose_company_website(origins)
    candidate_domain = identity_domain_for_url(website_candidate)

    if len(name_matches) > 1:
        return CompanyResolution(
            "ambiguous_name",
            website_candidate=website_candidate,
            reason=f"{len(name_matches)} exact normalized-name matches",
        )
    if len(name_matches) == 1:
        company = name_matches[0]
        stored_domain = _optional_string(company.get("primary_domain"))
        if candidate_domain and stored_domain and not domains_compatible(candidate_domain, stored_domain):
            return CompanyResolution(
                "identity_conflict",
                website_candidate=website_candidate,
                reason=f"name matched but domains differ: {stored_domain} vs {candidate_domain}",
            )
        if candidate_domain and stored_domain:
            return CompanyResolution(
                "existing_exact_name",
                company_id=int(company["id"]),
                website_candidate=website_candidate,
            )
        return CompanyResolution(
            "ambiguous_name",
            website_candidate=website_candidate,
            reason="exact name match lacks independent domain corroboration",
        )

    if website_candidate and candidate_domain:
        domain_matches = [
            company
            for company in companies
            if (stored := _optional_string(company.get("primary_domain")))
            and domains_compatible(candidate_domain, stored)
        ]
        if len(domain_matches) > 1:
            return CompanyResolution(
                "ambiguous_domain",
                website_candidate=website_candidate,
                reason=f"{len(domain_matches)} compatible domain matches",
            )
        if len(domain_matches) == 1:
            company = domain_matches[0]
            return CompanyResolution(
                "identity_conflict",
                website_candidate=website_candidate,
                reason=(
                    f"domain matched company_id={company['id']} but names differ: "
                    f"{company.get('name')} vs {evidence.company_name}"
                ),
            )
        return CompanyResolution(
            "new_company_domain_candidate",
            website_candidate=website_candidate,
        )

    return CompanyResolution("unresolved_no_domain")


def choose_company_website(origins: tuple[str, ...]) -> str | None:
    if not origins:
        return None
    by_domain: dict[str, list[str]] = {}
    for origin in origins:
        domain = identity_domain_for_url(origin)
        if domain:
            by_domain.setdefault(domain, []).append(origin)
    if len(by_domain) != 1:
        return None
    domain, domain_origins = next(iter(by_domain.items()))
    scheme_counts = Counter(urlparse(origin).scheme for origin in domain_origins)
    scheme = "https" if scheme_counts["https"] else "http"
    return urlunparse((scheme, domain, "", "", "", ""))


def external_job_origin(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or is_ats_domain(host)
        or _host_has_blocked_suffix(host)
    ):
        return None
    if parsed.port not in {None, 80, 443}:
        return None
    return urlunparse((parsed.scheme, host, "", "", "", ""))


def identity_domain_for_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = (urlparse(url).hostname or url).lower().strip(".")
    except ValueError:
        return None
    labels = host.removeprefix("www.").split(".")
    if len(labels) >= 3 and labels[0] in COMMON_JOB_HOST_PREFIXES:
        labels = labels[1:]
    domain = ".".join(labels)
    return domain or None


def domains_compatible(first: str, second: str) -> bool:
    left = identity_domain_for_url(first)
    right = identity_domain_for_url(second)
    if not left or not right:
        return False
    return left == right or left.endswith(f".{right}") or right.endswith(f".{left}")


def normalize_observed_name(value: str) -> str:
    return " ".join(value.lower().split())


def _invalid_evidence(board_token: str, error: str) -> GreenhouseBoardEvidence:
    return GreenhouseBoardEvidence(
        board_token=board_token,
        verification_status="invalid",
        http_status=200,
        company_name=None,
        job_count=0,
        external_job_origins=(),
        error=error,
    )


def _host_has_blocked_suffix(host: str) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in BLOCKED_COMPANY_HOST_SUFFIXES)


class _LogoLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        href = values.get("href")
        if "logo" in classes and href:
            self.hrefs.append(href)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            parsed = float(retry_after)
        except ValueError:
            parsed = 0.0
        if 0 < parsed <= 60:
            return parsed
    return float(2 ** (attempt - 1))


def _homepage_cache_key(url: str) -> str:
    # Namespace the key so a header-only homepage probe can never shadow a full-body
    # cache entry for the same public URL.
    return f"{HOMEPAGE_CACHE_KIND}:{url}"


def _safe_urlparse(url: str):
    try:
        return urlparse(url)
    except ValueError:
        return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
