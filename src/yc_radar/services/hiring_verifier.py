from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlparse, urlunparse

from yc_radar.domain.models import Company
from yc_radar.services.candidate_fit import profile_text
from yc_radar.services.source_providers import is_company_ats_url

HiringStatus = Literal["hiring", "not_hiring", "unknown"]

CAREER_KEYWORDS = (
    "career",
    "careers",
    "job",
    "jobs",
    "hiring",
    "join",
    "opening",
    "openings",
    "roles",
    "work with us",
)
LOW_VALUE_LINK_TERMS = (
    "blog",
    "docs",
    "documentation",
    "pricing",
    "login",
    "signin",
    "signup",
    "privacy",
    "terms",
    "twitter",
    "linkedin",
    "facebook",
)
NEGATIVE_HIRING_PATTERNS = (
    "no open roles",
    "no open positions",
    "no current openings",
    "no positions available",
    "not hiring",
    "not currently hiring",
    "we are not hiring",
    "we're not hiring",
    "currently no openings",
)
ROLE_FIT_TERMS = (
    "ai",
    "llm",
    "machine learning",
    "backend",
    "full stack",
    "full-stack",
    "data engineer",
    "infrastructure",
    "platform",
    "software engineer",
    "founding engineer",
    "python",
    "typescript",
    "node",
    "react",
)
ROLE_TITLE_PATTERN = re.compile(
    r"\b("
    r"(?:founding|senior|staff|principal|lead|full[- ]stack|backend|frontend|"
    r"software|ai|ml|machine learning|data|platform|infrastructure|product|"
    r"devops|site reliability|applied ai|research|solutions)"
    r"(?:\s+[a-z/&+-]+){0,4}\s+"
    r"(?:engineer|developer|architect|scientist|lead|manager)"
    r"|"
    r"(?:engineer|developer|architect|scientist),\s+[a-z][a-z /&+-]{2,60}"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScrapedPage:
    url: str
    markdown: str = ""
    html: str = ""
    links: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(part for part in (self.markdown, strip_html(self.html)) if part)


@dataclass
class HiringVerification:
    verified_hiring_status: HiringStatus
    career_page_url: str | None
    verified_roles: list[str]
    role_fit: str
    verification_source_url: str | None
    verification_checked_at: str
    verification_confidence: float
    firecrawl_pages_used: int
    verification_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PageScraper(Protocol):
    def scrape(self, url: str) -> ScrapedPage:
        """Scrape one exact URL."""


class FirecrawlPageScraper:
    def __init__(self, api_key: str, *, timeout_seconds: int = 30) -> None:
        from firecrawl import Firecrawl

        self.client = Firecrawl(api_key=api_key, timeout=timeout_seconds)
        self.timeout_seconds = timeout_seconds

    def scrape(self, url: str) -> ScrapedPage:
        document = self.client.scrape(
            url,
            formats=["markdown", "html", "links"],
            only_main_content=True,
            timeout=self.timeout_seconds * 1000,
            remove_base64_images=True,
        )
        return document_to_scraped_page(url, document)


def document_to_scraped_page(url: str, document: Any) -> ScrapedPage:
    return ScrapedPage(
        url=url,
        markdown=_document_value(document, "markdown") or "",
        html=_document_value(document, "html") or "",
        links=list(_document_value(document, "links") or []),
    )


def _document_value(document: Any, key: str) -> Any:
    if isinstance(document, dict):
        return document.get(key)
    return getattr(document, key, None)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href")
        if href:
            self._current_href = href
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href:
            text = " ".join(part.strip() for part in self._current_text if part.strip())
            self.anchors.append((self._current_href, text))
            self._current_href = None
            self._current_text = []


def strip_html(html: str) -> str:
    if not html:
        return ""
    without_scripts = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    return re.sub(r"\s+", " ", without_tags).strip()


def detect_career_links(homepage_url: str, page: ScrapedPage, *, limit: int = 2) -> list[str]:
    candidates: list[tuple[str, str]] = []
    candidates.extend((link, "") for link in page.links)

    parser = _AnchorParser()
    if page.html:
        parser.feed(page.html)
        candidates.extend(parser.anchors)

    markdown_links = re.findall(r"\[([^\]]{1,140})\]\(([^)]+)\)", page.markdown)
    candidates.extend((href, text) for text, href in markdown_links)

    ranked: dict[str, int] = {}
    for href, text in candidates:
        url = normalize_url(homepage_url, href)
        if not url:
            continue
        score = career_link_score(homepage_url, url, text)
        if score <= 0:
            continue
        ranked[url] = max(ranked.get(url, 0), score)

    return [
        url
        for url, _score in sorted(ranked.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def normalize_url(base_url: str, href: str) -> str | None:
    href = href.strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    parsed = urlparse(urljoin(base_url, href))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", parsed.query, "")
    )


def career_link_score(homepage_url: str, url: str, text: str = "") -> int:
    parsed_home = urlparse(homepage_url)
    parsed_url = urlparse(url)
    home_domain = _clean_domain(parsed_home.netloc)
    link_domain = _clean_domain(parsed_url.netloc)
    combined = f"{url} {text}".lower()

    same_domain = link_domain == home_domain or link_domain.endswith(f".{home_domain}")
    known_ats = is_company_ats_url(url)
    if not same_domain and not known_ats:
        return 0

    has_career_signal = any(keyword in combined for keyword in CAREER_KEYWORDS)
    if same_domain and not known_ats and not has_career_signal:
        return 0

    if any(term in combined for term in LOW_VALUE_LINK_TERMS) and not any(
        keyword in combined for keyword in CAREER_KEYWORDS
    ):
        return 0

    score = 5 if same_domain else 0
    if known_ats:
        score += 14
    for keyword in CAREER_KEYWORDS:
        if keyword in combined:
            score += 8
    if "/careers" in combined or "/jobs" in combined:
        score += 8
    if "linkedin.com" in link_domain:
        score -= 6
    return score


def _clean_domain(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def parse_roles(text: str, *, limit: int = 12) -> list[str]:
    roles: list[str] = []
    for raw_line in re.split(r"[\n\r]+", text):
        line = re.sub(r"\s+", " ", raw_line).strip(" -*•|")
        if not line or len(line) > 140:
            continue
        line_lower = line.lower()
        if any(term in line_lower for term in ("privacy", "terms", "cookie", "subscribe")):
            continue
        if ROLE_TITLE_PATTERN.search(line):
            roles.append(_clean_role_title(line))

    for match in ROLE_TITLE_PATTERN.finditer(text):
        roles.append(_clean_role_title(match.group(1)))

    return _dedupe_preserve_order(role for role in roles if 4 <= len(role) <= 100)[:limit]


def _clean_role_title(value: str) -> str:
    value = re.sub(r"^\[[^\]]+\]\s*", "", value)
    value = re.sub(r"^#+\s*", "", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+(remote|hybrid|onsite|full[- ]time|part[- ]time)$", "", value, flags=re.I)
    return value.strip(" -|:•")


def infer_role_fit(roles: list[str], profile: dict[str, Any]) -> str:
    if not roles:
        return "unknown"
    candidate_text = profile_text(profile)
    joined_roles = " ".join(roles).lower()
    if any(term in joined_roles and term in candidate_text for term in ROLE_FIT_TERMS):
        return "strong"
    if any(term in joined_roles for term in ROLE_FIT_TERMS):
        return "possible"
    return "possible"


def has_explicit_no_openings(text: str) -> bool:
    text_lower = re.sub(r"\s+", " ", text.lower())
    return any(pattern in text_lower for pattern in NEGATIVE_HIRING_PATTERNS)


def is_likely_hiring_page(homepage_url: str, page: ScrapedPage) -> bool:
    parsed_home = urlparse(homepage_url)
    parsed_page = urlparse(page.url)
    page_domain = _clean_domain(parsed_page.netloc)
    known_ats = is_company_ats_url(page.url)
    if known_ats:
        return True

    path_signal = f"{parsed_page.path} {parsed_page.query}".lower()
    if any(keyword in path_signal for keyword in CAREER_KEYWORDS):
        return True

    home_domain = _clean_domain(parsed_home.netloc)
    same_homepage = page_domain == home_domain and parsed_page.path.rstrip("/") in {"", "/"}
    if not same_homepage:
        return False

    text_lower = re.sub(r"\s+", " ", page.text.lower())
    strong_homepage_markers = (
        "open roles",
        "open positions",
        "job openings",
        "we are hiring",
        "we're hiring",
        "join our team",
    )
    return any(marker in text_lower for marker in strong_homepage_markers)


def verify_company_hiring(
    company: Company,
    scraper: PageScraper,
    profile: dict[str, Any],
    *,
    max_pages_per_company: int = 3,
    checked_at: str | None = None,
) -> HiringVerification:
    checked_at = checked_at or datetime.now(UTC).isoformat()
    pages_used = 0
    pages: list[ScrapedPage] = []

    def scrape_limited(url: str) -> ScrapedPage:
        nonlocal pages_used
        if pages_used >= max_pages_per_company:
            raise RuntimeError("Firecrawl page budget exhausted for company.")
        pages_used += 1
        return scraper.scrape(url)

    if not company.website:
        return _unknown_verification(checked_at, pages_used, error="Company has no website.")

    try:
        homepage = scrape_limited(company.website)
        pages.append(homepage)
        career_links = detect_career_links(
            company.website, homepage, limit=max_pages_per_company - 1
        )
        for career_url in career_links:
            if pages_used >= max_pages_per_company:
                break
            pages.append(scrape_limited(career_url))
    except Exception as exc:
        return _unknown_verification(checked_at, pages_used, error=str(exc))

    role_source_url: str | None = None
    roles: list[str] = []
    for page in pages:
        if not is_likely_hiring_page(company.website, page):
            continue
        page_roles = parse_roles(page.text)
        if page_roles and not role_source_url:
            role_source_url = page.url
        roles.extend(page_roles)
    roles = _dedupe_preserve_order(roles)

    career_page_url = pages[1].url if len(pages) > 1 else None
    negative_page = next((page for page in pages if has_explicit_no_openings(page.text)), None)

    if roles:
        role_fit = infer_role_fit(roles, profile)
        confidence = 0.9 if role_fit == "strong" else 0.78
        return HiringVerification(
            verified_hiring_status="hiring",
            career_page_url=career_page_url or role_source_url,
            verified_roles=roles[:12],
            role_fit=role_fit,
            verification_source_url=role_source_url,
            verification_checked_at=checked_at,
            verification_confidence=confidence,
            firecrawl_pages_used=pages_used,
        )

    if negative_page:
        return HiringVerification(
            verified_hiring_status="not_hiring",
            career_page_url=career_page_url or negative_page.url,
            verified_roles=[],
            role_fit="unknown",
            verification_source_url=negative_page.url,
            verification_checked_at=checked_at,
            verification_confidence=0.72,
            firecrawl_pages_used=pages_used,
        )

    return HiringVerification(
        verified_hiring_status="unknown",
        career_page_url=career_page_url,
        verified_roles=[],
        role_fit="unknown",
        verification_source_url=career_page_url or company.website,
        verification_checked_at=checked_at,
        verification_confidence=0.25,
        firecrawl_pages_used=pages_used,
    )


def _unknown_verification(
    checked_at: str,
    pages_used: int,
    *,
    error: str | None = None,
) -> HiringVerification:
    return HiringVerification(
        verified_hiring_status="unknown",
        career_page_url=None,
        verified_roles=[],
        role_fit="unknown",
        verification_source_url=None,
        verification_checked_at=checked_at,
        verification_confidence=0.0,
        firecrawl_pages_used=pages_used,
        verification_error=error,
    )


def verification_cache_key(company: Company) -> str:
    return f"{company.slug}:{company.website or ''}"


def load_hiring_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "items" in payload and isinstance(payload["items"], dict):
        return payload["items"]
    if isinstance(payload, dict):
        return payload
    return {}


def save_hiring_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(UTC).isoformat(),
        "items": cache,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _dedupe_preserve_order(values: list[str] | Any) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", " ", str(value)).strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped
