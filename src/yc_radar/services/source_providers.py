"""Shared provider metadata for deterministic URL classification, not adapter behavior."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

# These are ATS board hosts/suffixes, not arbitrary vendor marketing domains. Host
# matching must respect label boundaries so, for example, clever.com is never
# mistaken for lever.co.
ATS_DOMAINS = (
    "jobs.ashbyhq.com",
    "jobs.eu.ashbyhq.com",
    "boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
    "boards-api.greenhouse.io",
    "jobs.lever.co",
    "jobs.eu.lever.co",
    "workable.com",
    "workdayjobs.com",
    "myworkdayjobs.com",
    "bamboohr.com",
    "recruitee.com",
    "jobs.smartrecruiters.com",
    "careers.smartrecruiters.com",
    "applytojob.com",
    "app.dover.com",
    "wellfound.com",
)


def is_ats_domain(domain: str) -> bool:
    """Return whether a hostname is a known ATS board host or tenant subdomain."""
    host = domain.partition(":")[0].strip(".").lower().removeprefix("www.")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in ATS_DOMAINS)


def is_company_ats_url(url: str) -> bool:
    """Reject vendor marketing/navigation URLs that are not company-specific boards."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not is_ats_domain(host):
        return False

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if host == "wellfound.com":
        return len(parts) >= 2 and parts[0] == "company"
    if host == "app.dover.com":
        return (
            len(parts) >= 2 and parts[0] == "jobs"
        ) or (
            len(parts) >= 3 and parts[:2] == ["dover", "careers"]
        )
    if host in {
        "boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }:
        if parts and parts[0] != "embed":
            return True
        return bool(parse_qs(parsed.query).get("for"))
    if host == "boards-api.greenhouse.io":
        return len(parts) >= 3 and parts[:2] == ["v1", "boards"]
    if host in {"jobs.ashbyhq.com", "jobs.eu.ashbyhq.com", "jobs.lever.co", "jobs.eu.lever.co"}:
        return bool(parts)
    if host == "jobs.smartrecruiters.com" or host == "careers.smartrecruiters.com":
        return bool(parts)
    if host.endswith(".bamboohr.com"):
        return host != "bamboohr.com" and (not parts or "careers" in parts)
    if host.endswith(".recruitee.com"):
        return host != "recruitee.com"
    if host == "apply.workable.com":
        return bool(parts)
    if host.endswith(".workable.com"):
        return host != "workable.com"
    if host.endswith(".applytojob.com"):
        return host != "applytojob.com"
    if host.endswith(".workdayjobs.com") or host.endswith(".myworkdayjobs.com"):
        return bool(parts)
    return False
