from __future__ import annotations

import argparse
import asyncio
import csv
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from yc_radar.agents.llm import OpenAIResponsesClient
from yc_radar.core.config import get_settings
from yc_radar.domain.models import Company
from yc_radar.services.candidate_fit import (
    apply_current_opportunity_score,
    enrich_targets_with_llm,
    load_candidate_profile,
    rank_companies,
    rerank_verified_targets,
    role_focus_record,
    target_record,
)
from yc_radar.services.company_repository import CompanyRepository
from yc_radar.services.database import engine_from_url, fetch_yc_job_rows
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.hiring_verifier import (
    FirecrawlPageScraper,
    HiringVerification,
    load_hiring_cache,
    save_hiring_cache,
    verification_cache_key,
    verify_company_hiring,
)

CSV_FIELDS = [
    "rank",
    "name",
    "slug",
    "yc_url",
    "website",
    "one_liner",
    "batch",
    "status",
    "stage",
    "team_size",
    "yc_is_hiring",
    "all_locations",
    "regions",
    "industry",
    "subindustry",
    "industries",
    "tags",
    "prototype_score",
    "prototype_angle",
    "company_fit_score",
    "opportunity_score",
    "opportunity_score_reasons",
    "fit_score",
    "fit_reasons",
    "candidate_strength_matches",
    "target_role_lane",
    "matching_job_titles",
    "canonical_active_job_count",
    "canonical_raw_active_job_count",
    "canonical_duplicate_posting_count",
    "canonical_matching_job_count",
    "canonical_raw_matching_job_count",
    "canonical_duplicate_matching_job_count",
    "canonical_role_match_status",
    "canonical_matching_jobs",
    "matching_job_provenance",
    "best_remote_eligibility",
    "pakistan_explicit_matching_job_count",
    "global_explicit_matching_job_count",
    "regional_unconfirmed_matching_job_count",
    "remote_unclear_matching_job_count",
    # Compatibility aliases for older downstream CSV consumers.
    "globally_remote_matching_job_count",
    "pakistan_compatible_matching_job_count",
    "remote_matching_job_count",
    "role_match_status",
    "role_match_reasons",
    "application_angle",
    "proof_points_to_emphasize",
    "verified_hiring_status",
    "career_page_url",
    "verified_roles",
    "role_fit",
    "verification_source_url",
    "verification_checked_at",
    "verification_confidence",
    "firecrawl_pages_used",
    "llm_used",
    "why_you_fit",
    "why_they_might_care",
    "prototype_idea",
    "best_playbook",
    "risks",
    "next_action",
]


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Generate a source-neutral weekly target list from current job evidence."
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--candidate-pool", type=int, default=100)
    parser.add_argument("--max-team-size", type=int)
    parser.add_argument("--max-pages-per-company", type=int, default=3)
    parser.add_argument("--firecrawl-concurrency", type=int, default=2)
    parser.add_argument(
        "--profile-path",
        type=Path,
        default=settings.candidate_profile_path,
    )
    parser.add_argument("--output-dir", type=Path, default=None)

    verify_group = parser.add_mutually_exclusive_group()
    verify_group.add_argument("--verify-hiring", dest="verify_hiring", action="store_true")
    verify_group.add_argument("--no-verify-hiring", dest="verify_hiring", action="store_false")
    parser.set_defaults(verify_hiring=None)

    llm_group = parser.add_mutually_exclusive_group()
    llm_group.add_argument("--llm", dest="use_llm", action="store_true")
    llm_group.add_argument("--no-llm", dest="use_llm", action="store_false")
    parser.set_defaults(use_llm=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    output_dir = args.output_dir or settings.runs_dir / args.date
    output_dir.mkdir(parents=True, exist_ok=True)

    profile = load_candidate_profile(args.profile_path)
    companies = CompanyRepository().list()
    jobs_by_slug = load_yc_jobs_by_slug(settings.database_url)
    canonical_jobs_by_slug = load_canonical_jobs_by_slug(settings.database_url)
    ranked = rank_companies(companies, profile, max_team_size=args.max_team_size)
    candidate_targets = [
        target_record(
            score,
            rank=index,
            yc_jobs=jobs_by_slug.get(score.company.slug, []),
            canonical_jobs=canonical_jobs_by_slug.get(score.company.slug, []),
        )
        for index, score in enumerate(ranked, start=1)
    ]
    candidate_targets.sort(key=lambda target: int(target["fit_score"]), reverse=True)
    targets = candidate_targets[: args.candidate_pool]
    candidate_pool_size = len(targets)
    for index, target in enumerate(targets, start=1):
        target["rank"] = index

    verification_cache_path = output_dir / "hiring_verifications.json"
    cache = load_hiring_cache(verification_cache_path)
    verify_hiring = (
        args.verify_hiring if args.verify_hiring is not None else bool(settings.firecrawl_api_key)
    )
    new_firecrawl_pages = 0
    cached_verifications = 0

    companies_by_slug = {company.slug: company for company in companies}
    if verify_hiring and settings.firecrawl_api_key:
        cached_verifications, new_firecrawl_pages = verify_targets(
            targets=targets,
            companies_by_slug=companies_by_slug,
            profile=profile,
            cache=cache,
            api_key=settings.firecrawl_api_key,
            max_pages_per_company=max(1, args.max_pages_per_company),
            concurrency=args.firecrawl_concurrency,
        )
        save_hiring_cache(verification_cache_path, cache)
    else:
        save_hiring_cache(verification_cache_path, cache)

    refresh_role_focus(targets, companies_by_slug, jobs_by_slug, canonical_jobs_by_slug)
    targets = rerank_verified_targets(targets)[: args.limit]
    use_llm = args.use_llm if args.use_llm is not None else bool(settings.openai_api_key)
    if use_llm and settings.openai_api_key:
        asyncio.run(enrich_targets_with_llm(targets, profile, OpenAIResponsesClient(settings)))

    generated_at = datetime.now(UTC).isoformat()
    json_path = output_dir / "weekly_targets.json"
    csv_path = output_dir / "weekly_targets.csv"
    write_json(
        json_path,
        {
            "schema_version": 3,
            "generated_at": generated_at,
            "candidate_pool_size": candidate_pool_size,
            "target_count": len(targets),
            "firecrawl": {
                "enabled": bool(verify_hiring and settings.firecrawl_api_key),
                "max_pages_per_company": max(1, args.max_pages_per_company),
                "concurrency": min(max(1, args.firecrawl_concurrency), 2),
                "new_pages_used": new_firecrawl_pages,
                "cached_verifications": cached_verifications,
                "cache_path": str(verification_cache_path),
            },
            "targets": targets,
        },
    )
    write_csv(csv_path, targets)

    print(
        f"Loaded {len(companies)} companies and "
        f"{sum(map(len, canonical_jobs_by_slug.values()))} active canonical jobs."
    )
    print(f"Ranked candidate pool: {candidate_pool_size} companies.")
    print(f"Wrote {len(targets)} weekly targets: {json_path}")
    print(f"Wrote CSV: {csv_path}")
    if verify_hiring and not settings.firecrawl_api_key:
        print(
            "Firecrawl verification requested but FIRECRAWL_API_KEY is not set; hiring is unknown."
        )
    else:
        print(f"Firecrawl pages used this run: {new_firecrawl_pages}")
        print(f"Cached hiring verifications reused: {cached_verifications}")


def load_yc_jobs_by_slug(database_url: str) -> dict[str, list[dict[str, Any]]]:
    engine = engine_from_url(database_url)
    jobs_by_slug: dict[str, list[dict[str, Any]]] = {}
    for job in fetch_yc_job_rows(engine):
        jobs_by_slug.setdefault(job["company_slug"], []).append(job)
    return jobs_by_slug


def load_canonical_jobs_by_slug(database_url: str) -> dict[str, list[dict[str, Any]]]:
    engine = engine_from_url(database_url)
    jobs_by_slug: dict[str, list[dict[str, Any]]] = {}
    for job in JobRepository(engine).active_job_rows():
        jobs_by_slug.setdefault(str(job["company_slug"]), []).append(job)
    return jobs_by_slug


def refresh_role_focus(
    targets: list[dict[str, Any]],
    companies_by_slug: dict[str, Company],
    jobs_by_slug: dict[str, list[dict[str, Any]]],
    canonical_jobs_by_slug: dict[str, list[dict[str, Any]]],
) -> None:
    for target in targets:
        company = companies_by_slug.get(target["slug"])
        if company is None:
            continue
        target.update(
            role_focus_record(
                company,
                yc_jobs=jobs_by_slug.get(company.slug, []),
                canonical_jobs=canonical_jobs_by_slug.get(company.slug, []),
                verified_roles=target.get("verified_roles") or [],
            )
        )
        apply_current_opportunity_score(target)


def verify_targets(
    *,
    targets: list[dict[str, Any]],
    companies_by_slug: dict[str, Company],
    profile: dict[str, Any],
    cache: dict[str, dict[str, Any]],
    api_key: str,
    max_pages_per_company: int,
    concurrency: int,
) -> tuple[int, int]:
    max_workers = min(max(1, concurrency), 2)
    pending: list[tuple[dict[str, Any], Company, str]] = []
    cached_count = 0
    new_pages_used = 0

    for target in targets:
        company = companies_by_slug[target["slug"]]
        cache_key = verification_cache_key(company)
        cached = cache.get(cache_key)
        if cached:
            target.update(cached)
            cached_count += 1
            continue
        pending.append((target, company, cache_key))

    if not pending:
        return cached_count, new_pages_used

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                verify_one_company,
                company=company,
                profile=profile,
                api_key=api_key,
                max_pages_per_company=max_pages_per_company,
            ): (target, cache_key)
            for target, company, cache_key in pending
        }
        for future in as_completed(futures):
            target, cache_key = futures[future]
            verification = future.result()
            payload = verification.to_dict()
            target.update(payload)
            cache[cache_key] = payload
            new_pages_used += verification.firecrawl_pages_used

    return cached_count, new_pages_used


def verify_one_company(
    *,
    company: Company,
    profile: dict[str, Any],
    api_key: str,
    max_pages_per_company: int,
) -> HiringVerification:
    try:
        scraper = FirecrawlPageScraper(api_key)
        return verify_company_hiring(
            company,
            scraper,
            profile,
            max_pages_per_company=max_pages_per_company,
        )
    except Exception as exc:
        return HiringVerification(
            verified_hiring_status="unknown",
            career_page_url=None,
            verified_roles=[],
            role_fit="unknown",
            verification_source_url=None,
            verification_checked_at=datetime.now(UTC).isoformat(),
            verification_confidence=0.0,
            firecrawl_pages_used=0,
            verification_error=str(exc),
        )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_csv(path: Path, targets: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for target in targets:
            writer.writerow({field: csv_value(target.get(field)) for field in CSV_FIELDS})


def csv_value(value: Any) -> str | int | float | bool | None:
    if isinstance(value, list):
        if any(isinstance(item, dict) for item in value):
            return json.dumps(value, sort_keys=True, default=str)
        return "; ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return value


if __name__ == "__main__":
    main()
