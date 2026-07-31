#!/usr/bin/env python3
"""Export YC's public company directory from the Algolia index used by the site."""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from html.parser import HTMLParser
import json
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

from yc_radar.core.config import get_settings
from yc_radar.services.database import (
    create_schema,
    engine_from_url,
    upsert_yc_companies,
    upsert_yc_job_postings,
)


APP_ID = "45BWZJ1SGC"
API_KEY = (
    "NzllNTY5MzJiZGM2OTY2ZTQwMDEzOTNhYWZiZGRjODlhYzVkNjBmOGRjNzJi"
    "MWM4ZTU0ZDlhYTZjOTJiMjlhMWFuYWx5dGljc1RhZ3M9eWNkYyZyZXN0cmlj"
    "dEluZGljZXM9WUNDb21wYW55X3Byb2R1Y3Rpb24lMkNZQ0NvbXBhbnlfQnlf"
    "TGF1bmNoX0RhdGVfcHJvZHVjdGlvbiZ0YWdGaWx0ZXJzPSU1QiUyMnljZGNf"
    "cHVibGljJTIyJTVE"
)
INDEX_NAME = "YCCompany_production"
ENDPOINT = f"https://{APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
COMPANY_PAGE_CONCURRENCY = 8
USER_AGENT = "yc-radar/0.2 (+https://github.com/Daniishkhan/yc-radar; read-only research)"
JSON_ACCEPT = "application/json"
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_LANGUAGE = "en-US,en;q=0.8"


CSV_FIELDS = [
    "id",
    "name",
    "slug",
    "yc_url",
    "website",
    "one_liner",
    "batch",
    "status",
    "stage",
    "team_size",
    "isHiring",
    "all_locations",
    "regions",
    "industry",
    "subindustry",
    "industries",
    "tags",
    "prototype_score",
    "prototype_angle",
    "job_count",
]

JOB_CSV_FIELDS = [
    "id",
    "company_id",
    "company_slug",
    "company_name",
    "company_yc_url",
    "title",
    "url",
    "absolute_url",
    "apply_url",
    "location",
    "type",
    "role",
    "role_specific_type",
    "pretty_role",
    "salary_range",
    "equity_range",
    "min_experience",
    "min_school_year",
    "visa",
    "skills",
    "is_incomplete",
    "created_at",
    "last_active",
]


def algolia_query(
    page: int,
    hits_per_page: int = 1000,
    facet_filters: list[str] | None = None,
    include_facets: bool = False,
) -> dict[str, Any]:
    param_values: dict[str, Any] = {
        "query": "",
        "hitsPerPage": hits_per_page,
        "page": page,
        "maxValuesPerFacet": 1000,
        "tagFilters": "",
    }
    if facet_filters:
        param_values["facetFilters"] = json.dumps(facet_filters)
    if include_facets:
        param_values["facets"] = json.dumps(["batch"])
        param_values["attributesToRetrieve"] = json.dumps([])
        param_values["attributesToHighlight"] = json.dumps([])
        param_values["analytics"] = "false"

    params = urllib.parse.urlencode(param_values)
    body = json.dumps({"requests": [{"indexName": INDEX_NAME, "params": params}]})
    result = subprocess.run(
        [
            "curl",
            "-sS",
            ENDPOINT,
            "-H",
            "Content-Type: application/json",
            "-H",
            f"Accept: {JSON_ACCEPT}",
            "-H",
            f"Accept-Language: {ACCEPT_LANGUAGE}",
            "-H",
            f"User-Agent: {USER_AGENT}",
            "-H",
            f"X-Algolia-Application-Id: {APP_ID}",
            "-H",
            f"X-Algolia-API-Key: {API_KEY}",
            "-H",
            "Origin: https://www.ycombinator.com",
            "-H",
            "Referer: https://www.ycombinator.com/companies",
            "--data-binary",
            "@-",
        ],
        input=body,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)["results"][0]


def strip_search_metadata(company: dict[str, Any]) -> dict[str, Any]:
    company = dict(company)
    for key in ("_highlightResult", "_snippetResult", "_rankingInfo"):
        company.pop(key, None)
    return company


class DataPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.data_pages: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        data_page = attrs_dict.get("data-page")
        if data_page:
            self.data_pages.append(data_page)


def fetch_company_page(slug: str) -> str:
    result = subprocess.run(
        [
            "curl",
            "-L",
            "-sS",
            "--compressed",
            f"https://www.ycombinator.com/companies/{slug}",
            "-H",
            f"Accept: {HTML_ACCEPT}",
            "-H",
            f"Accept-Language: {ACCEPT_LANGUAGE}",
            "-H",
            f"User-Agent: {USER_AGENT}",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def extract_page_props(html: str) -> dict[str, Any]:
    parser = DataPageParser()
    parser.feed(html)
    if not parser.data_pages:
        return {}
    page = json.loads(unescape(parser.data_pages[0]))
    props = page.get("props")
    return props if isinstance(props, dict) else {}


def extract_company_job_postings(company: dict[str, Any]) -> list[dict[str, Any]]:
    html = fetch_company_page(str(company["slug"]))
    props = extract_page_props(html)
    job_postings = props.get("jobPostings") or []
    if not isinstance(job_postings, list):
        return []

    jobs: list[dict[str, Any]] = []
    for job in job_postings:
        if not isinstance(job, dict):
            continue
        job = dict(job)
        job["company_id"] = company.get("id")
        job["company_slug"] = company.get("slug")
        job["company_name"] = company.get("name")
        job["company_yc_url"] = f"https://www.ycombinator.com/companies/{company.get('slug', '')}"
        jobs.append(job)
    return jobs


def extract_all_job_postings(
    companies: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    hiring_companies = [company for company in companies if company.get("isHiring")]
    jobs_by_slug: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=COMPANY_PAGE_CONCURRENCY) as executor:
        future_to_company = {
            executor.submit(extract_company_job_postings, company): company
            for company in hiring_companies
        }
        completed = 0
        for future in as_completed(future_to_company):
            company = future_to_company[future]
            completed += 1
            try:
                jobs_by_slug[str(company["slug"])] = future.result()
            except Exception as exc:
                errors.append(f"{company.get('slug')}: {exc}")
                jobs_by_slug[str(company["slug"])] = []

            if completed % 100 == 0:
                print(
                    f"Fetched job postings for {completed} / {len(hiring_companies)} hiring companies."
                )

    return jobs_by_slug, errors


def as_joined(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return str(value)


def csv_row(company: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": company.get("id", ""),
        "name": company.get("name", ""),
        "slug": company.get("slug", ""),
        "yc_url": f"https://www.ycombinator.com/companies/{company.get('slug', '')}",
        "website": company.get("website", ""),
        "one_liner": company.get("one_liner", ""),
        "batch": company.get("batch", ""),
        "status": company.get("status", ""),
        "stage": company.get("stage", ""),
        "team_size": company.get("team_size", ""),
        "isHiring": company.get("isHiring", ""),
        "all_locations": company.get("all_locations", ""),
        "regions": as_joined(company.get("regions")),
        "industry": company.get("industry", ""),
        "subindustry": company.get("subindustry", ""),
        "industries": as_joined(company.get("industries")),
        "tags": as_joined(company.get("tags")),
        "prototype_score": company.get("prototype_score", ""),
        "prototype_angle": company.get("prototype_angle", ""),
        "job_count": len(company.get("jobPostings") or []),
    }


def job_csv_row(job: dict[str, Any]) -> dict[str, Any]:
    relative_url = job.get("url") or ""
    absolute_url = urllib.parse.urljoin("https://www.ycombinator.com", relative_url)
    return {
        "id": job.get("id", ""),
        "company_id": job.get("company_id", ""),
        "company_slug": job.get("company_slug", ""),
        "company_name": job.get("company_name", ""),
        "company_yc_url": job.get("company_yc_url", ""),
        "title": job.get("title", ""),
        "url": relative_url,
        "absolute_url": absolute_url,
        "apply_url": job.get("applyUrl", ""),
        "location": job.get("location", ""),
        "type": job.get("type", ""),
        "role": job.get("role", ""),
        "role_specific_type": job.get("roleSpecificType", ""),
        "pretty_role": job.get("prettyRole", ""),
        "salary_range": job.get("salaryRange", ""),
        "equity_range": job.get("equityRange", ""),
        "min_experience": job.get("minExperience", ""),
        "min_school_year": job.get("minSchoolYear", ""),
        "visa": job.get("visa", ""),
        "skills": as_joined(job.get("skills")),
        "is_incomplete": job.get("isIncomplete", ""),
        "created_at": job.get("createdAt", ""),
        "last_active": job.get("lastActive", ""),
    }


def text_blob(company: dict[str, Any]) -> str:
    parts = [
        company.get("name"),
        company.get("one_liner"),
        company.get("long_description"),
        company.get("industry"),
        company.get("subindustry"),
        " ".join(company.get("industries") or []),
        " ".join(company.get("tags") or []),
        " ".join(company.get("regions") or []),
    ]
    return " ".join(str(part) for part in parts if part).lower()


def prototype_angle(company: dict[str, Any]) -> str:
    blob = text_blob(company)
    if "open source" in blob or "developer tools" in blob or "infrastructure" in blob:
        return "Build a repo PR, SDK/plugin, or runnable integration demo with docs and a Loom."
    if "security" in blob or "compliance" in blob:
        return "Build an audit-trail workflow or security automation demo against a realistic sample app."
    if "customer support" in blob or "customer success" in blob:
        return "Build an AI triage/insights demo from sample support tickets and show before/after workflow time."
    if "fintech" in blob or "finance" in blob or "payments" in blob:
        return "Build a reconciliation/risk dashboard with explainable flags and exportable audit evidence."
    if "healthcare" in blob:
        return "Build a HIPAA-conscious intake/ops workflow demo using synthetic patient/provider data."
    if "recruiting" in blob or "human resources" in blob:
        return "Build a candidate/job matching or workflow automation prototype with transparent scoring."
    if "ai" in blob or "agent" in blob:
        return "Build a focused AI agent demo that automates one painful user workflow end to end."
    return (
        "Build a narrow workflow prototype tied to their one-liner, with a 60-second Loom and repo."
    )


def prototype_score(company: dict[str, Any]) -> int:
    blob = text_blob(company)
    score = 0
    team_size = company.get("team_size") or 0

    if company.get("status") == "Active":
        score += 5
    if company.get("isHiring"):
        score += 3
    if company.get("website"):
        score += 1
    if not company.get("top_company"):
        score += 1

    if team_size <= 3:
        score += 6
    elif team_size <= 5:
        score += 5
    elif team_size <= 10:
        score += 4
    elif team_size <= 25:
        score += 2

    for term, points in {
        "open source": 6,
        "developer tools": 5,
        "infrastructure": 4,
        "artificial intelligence": 4,
        "ai": 3,
        "agent": 3,
        "security": 3,
        "analytics": 2,
        "productivity": 2,
        "b2b": 2,
    }.items():
        if term in blob:
            score += points

    regions = set(company.get("regions") or [])
    if regions.intersection({"Remote", "Fully Remote", "Partly Remote"}):
        score += 3
    if regions.intersection({"Pakistan", "India", "South Asia", "Middle East and North Africa"}):
        score += 2

    return score


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(
            {
                key: _clean_csv_value(value)
                for key, value in row.items()
            }
            for row in rows
        )
    tmp_path.replace(path)


def _clean_csv_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n"))


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Refresh YC companies/jobs into Postgres and lightweight CSV snapshots."
    )
    parser.add_argument("--snapshot-dir", type=Path, default=settings.snapshots_dir)
    parser.add_argument("--write-raw-json", action="store_true")
    parser.add_argument(
        "--raw-output-dir",
        type=Path,
        default=settings.local_debug_dir / "yc_extract",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.snapshot_dir.mkdir(parents=True, exist_ok=True)
    engine = engine_from_url()
    create_schema(engine)

    facet_page = algolia_query(page=0, hits_per_page=1, include_facets=True)
    batch_counts = facet_page["facets"]["batch"]
    nb_hits = facet_page["nbHits"]

    by_object_id: dict[str, dict[str, Any]] = {}
    for batch in sorted(batch_counts):
        first_page = algolia_query(page=0, facet_filters=[f"batch:{batch}"])
        batch_pages = first_page["nbPages"]
        for hit in first_page["hits"]:
            by_object_id[hit["objectID"]] = strip_search_metadata(hit)

        for page in range(1, batch_pages):
            result = algolia_query(page=page, facet_filters=[f"batch:{batch}"])
            for hit in result["hits"]:
                by_object_id[hit["objectID"]] = strip_search_metadata(hit)
            time.sleep(0.15)

    companies = sorted(by_object_id.values(), key=lambda company: str(company.get("id", "")))
    jobs_by_slug, job_errors = extract_all_job_postings(companies)
    for company in companies:
        company["jobPostings"] = jobs_by_slug.get(str(company.get("slug")), [])
        company["prototype_score"] = prototype_score(company)
        company["prototype_angle"] = prototype_angle(company)

    job_postings = [
        job
        for company in companies
        for job in company.get("jobPostings", [])
        if isinstance(job, dict)
    ]

    upsert_yc_companies(engine, companies)
    upsert_yc_job_postings(engine, job_postings)

    write_csv(
        args.snapshot_dir / "yc_companies.csv",
        [csv_row(company) for company in companies],
        CSV_FIELDS,
    )
    write_csv(
        args.snapshot_dir / "yc_job_postings.csv",
        [job_csv_row(job) for job in job_postings],
        JOB_CSV_FIELDS,
    )
    if args.write_raw_json:
        write_json(args.raw_output_dir / "yc_companies_raw.json", companies)
        write_json(args.raw_output_dir / "yc_job_postings_raw.json", job_postings)

    print(f"Fetched {len(companies)} / {nb_hits} companies across {len(batch_counts)} YC batches.")
    print(
        f"Fetched {len(job_postings)} job postings from "
        f"{sum(1 for c in companies if c.get('isHiring'))} hiring companies."
    )
    if job_errors:
        print(f"Job posting page errors: {len(job_errors)}")
    print(f"Wrote {args.snapshot_dir / 'yc_companies.csv'}")
    print(f"Wrote {args.snapshot_dir / 'yc_job_postings.csv'}")
    if args.write_raw_json:
        print(f"Wrote raw JSON debug files under {args.raw_output_dir}")
    print(f"Wrote {engine.url.database}")


if __name__ == "__main__":
    main()
