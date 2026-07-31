import pytest

from yc_radar.services.commoncrawl_greenhouse import (
    build_candidate_query,
    build_partition_query,
    deduplicate_candidate_rows,
)


def test_candidate_query_is_partition_bounded_and_includes_us_and_eu_hosts() -> None:
    query = build_candidate_query("radar_commoncrawl", "CC-MAIN-2026-30")

    assert "crawl = 'CC-MAIN-2026-30'" in query
    assert "subset = 'warc'" in query
    assert "'job-boards.greenhouse.io'" in query
    assert "'job-boards.eu.greenhouse.io'" in query
    assert "url_extract_parameter(url, 'for')" in query
    assert "SELECT lower(board_token) AS board_token" in query
    assert "GROUP BY board_token" in query


def test_partition_query_registers_only_the_requested_crawl() -> None:
    query = build_partition_query("radar_commoncrawl", "CC-MAIN-2026-30")

    assert "ALTER TABLE radar_commoncrawl.url_index" in query
    assert query.count("CC-MAIN-2026-30") == 2
    assert "MSCK REPAIR" not in query


@pytest.mark.parametrize(
    ("database", "crawl"),
    [("bad-name", "CC-MAIN-2026-30"), ("radar_commoncrawl", "2026-30")],
)
def test_query_builders_reject_untrusted_identifiers(database: str, crawl: str) -> None:
    with pytest.raises(ValueError):
        build_candidate_query(database, crawl)


def test_candidate_rows_collapse_case_variants_before_provider_registration() -> None:
    rows = deduplicate_candidate_rows(
        [
            {
                "board_token": "OKX",
                "canonical_source_url": "https://job-boards.greenhouse.io/OKX",
                "example_observed_url": "https://job-boards.greenhouse.io/OKX/jobs/2",
                "observation_count": "2",
            },
            {
                "board_token": "okx",
                "canonical_source_url": "https://job-boards.greenhouse.io/okx",
                "example_observed_url": "https://job-boards.greenhouse.io/okx/jobs/1",
                "observation_count": "3",
            },
        ]
    )

    assert rows == [
        {
            "board_token": "okx",
            "canonical_source_url": "https://job-boards.greenhouse.io/okx",
            "example_observed_url": "https://job-boards.greenhouse.io/OKX/jobs/2",
            "observation_count": "5",
        }
    ]
