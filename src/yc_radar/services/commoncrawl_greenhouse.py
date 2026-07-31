from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CRAWL_RE = re.compile(r"^CC-MAIN-\d{4}-\d{2}$")
GREENHOUSE_HOSTS = (
    "boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
    "boards-api.greenhouse.io",
)


def deduplicate_candidate_rows(
    rows: Iterable[Mapping[str, str]],
) -> list[dict[str, str]]:
    deduplicated: dict[str, dict[str, str]] = {}
    for source_row in rows:
        row = dict(source_row)
        token = row["board_token"].lower()
        current = deduplicated.get(token)
        if current is None:
            deduplicated[token] = {
                **row,
                "board_token": token,
                "canonical_source_url": f"https://job-boards.greenhouse.io/{token}",
            }
            continue
        current["observation_count"] = str(
            int(current.get("observation_count") or 0)
            + int(row.get("observation_count") or 0)
        )
        current["example_observed_url"] = min(
            current["example_observed_url"], row["example_observed_url"]
        )
        if current.get("first_observed_at") and row.get("first_observed_at"):
            current["first_observed_at"] = min(
                current["first_observed_at"], row["first_observed_at"]
            )
        if current.get("last_observed_at") and row.get("last_observed_at"):
            current["last_observed_at"] = max(
                current["last_observed_at"], row["last_observed_at"]
            )
    return list(deduplicated.values())


def build_partition_query(database: str, crawl: str) -> str:
    _validate_query_identifiers(database, crawl)
    return f"""
ALTER TABLE {database}.url_index ADD IF NOT EXISTS
PARTITION (crawl = '{crawl}', subset = 'warc')
LOCATION 's3://commoncrawl/cc-index/table/cc-main/warc/crawl={crawl}/subset=warc/'
""".strip()


def build_candidate_query(database: str, crawl: str) -> str:
    _validate_query_identifiers(database, crawl)
    hosts = ",\n      ".join(f"'{host}'" for host in GREENHOUSE_HOSTS)
    return f"""
WITH greenhouse_urls AS (
  SELECT
    CASE
      WHEN url_host_name = 'boards-api.greenhouse.io' THEN
        regexp_extract(url_path, '^/v1/boards/([A-Za-z0-9_-]{{1,128}})(?:/|$)', 1)
      WHEN regexp_like(url_path, '^/embed/(?:job_board|job_app)(?:/|$)') THEN
        url_extract_parameter(url, 'for')
      ELSE
        regexp_extract(url_path, '^/([A-Za-z0-9_-]{{1,128}})(?:/|$)', 1)
    END AS board_token,
    url,
    fetch_time,
    fetch_status
  FROM {database}.url_index
  WHERE crawl = '{crawl}'
    AND subset = 'warc'
    AND url_host_name IN (
      {hosts}
    )
    AND fetch_status BETWEEN 200 AND 399
),
valid_tokens AS (
  SELECT lower(board_token) AS board_token, url, fetch_time, fetch_status
  FROM greenhouse_urls
  WHERE regexp_like(board_token, '^[A-Za-z0-9_-]{{1,128}}$')
    AND lower(board_token) NOT IN ('embed', 'internal', 'v1')
)
SELECT
  board_token,
  concat('https://job-boards.greenhouse.io/', board_token) AS canonical_source_url,
  min(url) AS example_observed_url,
  count(*) AS observation_count,
  min(fetch_time) AS first_observed_at,
  max(fetch_time) AS last_observed_at
FROM valid_tokens
GROUP BY board_token
ORDER BY observation_count DESC, board_token
""".strip()


def _validate_query_identifiers(database: str, crawl: str) -> None:
    if not IDENTIFIER_RE.fullmatch(database):
        raise ValueError("invalid Athena database identifier")
    if not CRAWL_RE.fullmatch(crawl):
        raise ValueError("invalid Common Crawl ID")
