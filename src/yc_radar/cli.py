from __future__ import annotations

import argparse

from yc_radar.services.company_repository import CompanyRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect YC Radar prototype targets.")
    parser.add_argument("--query", default=None)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-team-size", type=int, default=10)
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()

    companies = CompanyRepository().search(
        query=args.query,
        hiring=True,
        remote=True if args.remote else None,
        max_team_size=args.max_team_size,
    )

    for company in companies[: args.limit]:
        print(
            f"{company.prototype_score or 0:>2} | {company.name} | "
            f"team={company.team_size or '?'} | {company.website or company.yc_url} | "
            f"{company.one_liner or ''}"
        )


if __name__ == "__main__":
    main()
