import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "classify_discovered_urls.py"
SPEC = importlib.util.spec_from_file_location("classify_discovered_urls", SCRIPT_PATH)
assert SPEC and SPEC.loader
classify_discovered_urls = importlib.util.module_from_spec(SPEC)
sys.modules["classify_discovered_urls"] = classify_discovered_urls
SPEC.loader.exec_module(classify_discovered_urls)


def test_classifier_identifies_individual_job_detail_page() -> None:
    result = classify_discovered_urls.classify_page(
        url="https://example.com/careers/senior-backend-engineer",
        title="Senior Backend Engineer at Example",
        text=(
            "Senior Backend Engineer About the role Apply now Responsibilities "
            "Requirements Python Postgres distributed systems"
        ),
        http_status=200,
        url_kind="jobs_page",
    )

    assert result.page_kind == "job_detail"
    assert result.job_title == "Senior Backend Engineer"


def test_classifier_identifies_general_career_listing_page() -> None:
    result = classify_discovered_urls.classify_page(
        url="https://example.com/careers",
        title="Careers at Example",
        text=(
            "Careers Current openings Open roles Senior Backend Engineer "
            "Product Designer Forward Deployed Engineer"
        ),
        http_status=200,
        url_kind="careers_page",
    )

    assert result.page_kind == "job_listing"
    assert result.job_title is None
    assert len(result.role_titles) >= 2


def test_classifier_keeps_ats_index_separate_from_job_details() -> None:
    result = classify_discovered_urls.classify_page(
        url="https://jobs.ashbyhq.com/example",
        title="Example Jobs",
        text="Open roles Senior Software Engineer Product Manager Apply to any matching role.",
        http_status=200,
        url_kind="ats",
    )

    assert result.page_kind == "ats_listing"
    assert result.job_title is None


def test_short_role_terms_do_not_match_inside_company_slug() -> None:
    result = classify_discovered_urls.classify_page(
        url="https://jobs.ashbyhq.com/aiprise",
        title="AiPrise Jobs",
        text="AiPrise Jobs We're hiring",
        http_status=200,
        url_kind="ats",
    )

    assert result.page_kind == "ats_listing"
    assert result.job_title is None
