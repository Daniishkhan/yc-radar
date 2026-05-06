#!/usr/bin/env python3
"""Export YC's public company directory from the Algolia index used by the site."""

from __future__ import annotations

import csv
import json
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any


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
OUT_DIR = Path("data")


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
    return "Build a narrow workflow prototype tied to their one-liner, with a 60-second Loom and repo."


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
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)

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

    ranked_rows = []
    for company in companies:
        row = csv_row(company)
        row["prototype_score"] = prototype_score(company)
        row["prototype_angle"] = prototype_angle(company)
        ranked_rows.append(row)

    ranked_rows.sort(
        key=lambda row: (
            int(row["prototype_score"]),
            int(row["team_size"] or 999999) * -1,
        ),
        reverse=True,
    )

    write_json(OUT_DIR / "yc_companies_raw.json", companies)
    write_csv(OUT_DIR / "yc_companies.csv", [csv_row(company) for company in companies], CSV_FIELDS)
    write_csv(
        OUT_DIR / "yc_companies_prototype_targets.csv",
        ranked_rows,
        ["prototype_score", "prototype_angle", *CSV_FIELDS],
    )

    print(f"Fetched {len(companies)} / {nb_hits} companies across {len(batch_counts)} YC batches.")
    print(f"Wrote {OUT_DIR / 'yc_companies_raw.json'}")
    print(f"Wrote {OUT_DIR / 'yc_companies.csv'}")
    print(f"Wrote {OUT_DIR / 'yc_companies_prototype_targets.csv'}")


if __name__ == "__main__":
    main()
