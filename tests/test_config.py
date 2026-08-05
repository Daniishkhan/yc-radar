from pathlib import Path

from yc_radar.core.config import Settings


def test_settings_derive_data_paths_from_data_dir() -> None:
    settings = Settings(DATA_DIR="workspace-data", _env_file=None)

    assert settings.database_url == (
        "postgresql+psycopg://yc_radar:yc_radar@localhost:5433/yc_radar"
    )
    assert settings.companies_csv_path == Path("workspace-data/snapshots/yc_companies.csv")
    assert settings.yc_job_postings_csv_path == Path("workspace-data/snapshots/yc_job_postings.csv")
    assert settings.candidate_profile_path == Path(
        "workspace-data/local/profile/candidate_profile.json"
    )
    assert settings.runs_dir == Path("workspace-data/local/runs")


def test_database_url_override_wins_over_derived_default() -> None:
    settings = Settings(
        DATA_DIR="workspace-data",
        DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/custom",
        _env_file=None,
    )

    assert settings.database_url == "postgresql+psycopg://user:pass@localhost:5432/custom"


def test_blank_database_url_uses_postgres_default() -> None:
    settings = Settings(DATA_DIR="workspace-data", DATABASE_URL="", _env_file=None)

    assert settings.database_url == (
        "postgresql+psycopg://yc_radar:yc_radar@localhost:5433/yc_radar"
    )


def test_theirstack_api_key_is_optional_and_reads_environment_alias(monkeypatch) -> None:
    monkeypatch.delenv("THEIRSTACK_API_KEY", raising=False)
    assert Settings(_env_file=None).theirstack_api_key is None

    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-theirstack-key")
    assert Settings(_env_file=None).theirstack_api_key == "test-theirstack-key"
