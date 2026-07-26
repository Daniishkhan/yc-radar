"""Conservative, deterministic URL canonicalization and inventory quality rules."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from yc_radar.services.source_providers import is_ats_domain, is_company_ats_url

POLICY_VERSION = "url-quality-v3"
TRACKING_QUERY_KEYS = {
    "campaign",
    "fbclid",
    "gh_src",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
    "source",
}
CAREER_LISTING_FILTER_KEYS = {
    "department",
    "filter",
    "location",
    "page",
    "q",
    "search",
    "team",
}
ATS_BOARD_SELECTION_KEYS = {"ashby_jid", "gh_jid"}
GENERIC_CAREER_PATHS = {
    "/career",
    "/careers",
    "/job",
    "/jobs",
    "/job-openings",
    "/open-positions",
    "/open-roles",
    "/current-openings",
    "/openings",
    "/positions",
    "/join-us",
    "/join-our-team",
    "/work-with-us",
}
CAREER_PATH_TERMS = {
    "career",
    "careers",
    "job",
    "jobs",
    "job-openings",
    "open-positions",
    "open-roles",
    "current-openings",
    "openings",
    "positions",
    "join-us",
    "join-our-team",
    "work-with-us",
}
LOW_VALUE_PATH_SEGMENTS = {
    "academy",
    "blog",
    "docs",
    "documentation",
    "facebook",
    "instagram",
    "linkedin",
    "login",
    "news",
    "pricing",
    "privacy",
    "signin",
    "signup",
    "templates",
    "terms",
    "twitter",
}


def normalize_url(base_url: str, href: str) -> str | None:
    """Resolve an HTTP(S) link and drop only safe tracking/listing filters."""
    if not href:
        return None
    href = href.strip()
    if href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    parsed = urlparse(urljoin(base_url, href))
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        host = _normalized_netloc(parsed)
    except ValueError:
        return None
    path = parsed.path.rstrip("/") or "/"
    query = normalized_query(
        parsed.query,
        path=path,
        is_ats_board=is_company_ats_url(urlunparse((parsed.scheme, host, path, "", "", ""))),
    )
    return urlunparse((parsed.scheme.lower(), host, path, "", query, ""))


def normalized_query(query: str, *, path: str, is_ats_board: bool = False) -> str:
    """Keep semantic query values while dropping known tracking/listing filters."""
    generic_career_path = _has_career_path(path)
    pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        normalized_key = key.lower()
        if normalized_key.startswith("utm_") or normalized_key in TRACKING_QUERY_KEYS:
            continue
        if generic_career_path and normalized_key in CAREER_LISTING_FILTER_KEYS:
            continue
        if is_ats_board and normalized_key in ATS_BOARD_SELECTION_KEYS:
            continue
        pairs.append((key, value))
    return urlencode(sorted(pairs), doseq=True)


def canonical_url_key(url: str) -> str | None:
    """Return a scheme/www/tracking-insensitive key without collapsing role URLs."""
    normalized = normalize_url(url, url)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    return urlunparse(("https", _normalized_netloc(parsed, remove_www=True), parsed.path, "", parsed.query, ""))


def inventory_rejection_reason(company_slug: str, url: str) -> str | None:
    """Known, audited inventory misassignments that are safe to quarantine.

    These are deliberately narrow company+host/path rules from the live URL audit;
    arbitrary third-party detail URLs remain audit-only.
    """
    normalized = normalize_url(url, url)
    if not normalized:
        return "malformed_or_non_http_url"
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    slug = company_slug.lower()
    if slug == "elph" and host == "brex.com":
        return "known_cross_company_redirect_elph_to_brex"
    if slug == "edexia" and host == "clever.com":
        return "known_cross_company_redirect_edexia_to_clever"
    if slug == "kalibrr" and host == "kalibrr.com" and path != "/c/kalibrr-ph/jobs":
        return "third_party_multi_tenant_sitemap_fanout"
    if slug == "landed-2" and host == "gotlanded.com" and path not in {"/careers", "/jobs"}:
        return "third_party_multi_tenant_sitemap_fanout"
    if slug == "ashby" and host.endswith("ashbyhq.com"):
        if not is_company_ats_url(normalized) and path != "/careers":
            return "vendor_navigation_not_company_careers"
    if slug == "clever" and host.endswith("clever.com") and path != "/about/careers":
        return "vendor_navigation_not_company_careers"
    if slug == "lever" and host.endswith("lever.co"):
        if not is_company_ats_url(normalized) and not _has_career_path(path):
            return "vendor_navigation_not_company_careers"
    if slug == "cspa" and host.endswith("wellfound.com") and not is_company_ats_url(normalized):
        return "vendor_navigation_not_company_careers"
    return None


def quality_rejection_reason(url: str) -> str | None:
    """Return only deterministic deactivation reasons; ambiguity remains active."""
    normalized = normalize_url(url, url)
    if not normalized:
        return "malformed_or_non_http_url"
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if is_ats_domain(host) and not is_company_ats_url(normalized):
        return "ats_vendor_marketing_or_root"
    path_parts = {
        part.lower().replace("_", "-")
        for part in parsed.path.split("/")
        if part
    }
    if path_parts & CAREER_PATH_TERMS or is_company_ats_url(normalized):
        return None
    if path_parts & LOW_VALUE_PATH_SEGMENTS:
        return "deterministic_low_value_destination"
    return None


def is_career_listing_path(url: str) -> bool:
    parsed = urlparse(url)
    return _has_career_path(parsed.path)


def _has_career_path(path: str) -> bool:
    parts = [part.lower().replace("_", "-") for part in path.split("/") if part]
    return bool(parts and parts[-1] in CAREER_PATH_TERMS)


def _normalized_netloc(parsed, *, remove_www: bool = False) -> str:
    host = (parsed.hostname or "").lower()
    if remove_www:
        host = host.removeprefix("www.")
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        return f"{host}:{port}"
    return host
