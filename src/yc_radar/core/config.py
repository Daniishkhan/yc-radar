from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    data_dir: Path = Field(default=Path("data"), validation_alias="DATA_DIR")
    database_url_override: str | None = Field(default=None, validation_alias="DATABASE_URL")
    default_database_url: str = Field(
        default="postgresql+psycopg://yc_radar:yc_radar@localhost:5433/yc_radar",
        validation_alias="DEFAULT_DATABASE_URL",
    )
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", validation_alias="OPENAI_MODEL")
    firecrawl_api_key: str | None = Field(default=None, validation_alias="FIRECRAWL_API_KEY")
    theirstack_api_key: str | None = Field(default=None, validation_alias="THEIRSTACK_API_KEY")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def database_url(self) -> str:
        if self.database_url_override and self.database_url_override.strip():
            return self.database_url_override
        return self.default_database_url

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def local_dir(self) -> Path:
        return self.data_dir / "local"

    @property
    def local_debug_dir(self) -> Path:
        return self.local_dir / "debug"

    @property
    def companies_csv_path(self) -> Path:
        return self.snapshots_dir / "yc_companies.csv"

    @property
    def yc_job_postings_csv_path(self) -> Path:
        return self.snapshots_dir / "yc_job_postings.csv"

    @property
    def resume_path(self) -> Path:
        return self.local_dir / "resume" / "resume.pdf"

    @property
    def candidate_profile_path(self) -> Path:
        return self.local_dir / "profile" / "candidate_profile.json"

    @property
    def resume_text_path(self) -> Path:
        return self.local_dir / "profile" / "resume_text.txt"

    @property
    def runs_dir(self) -> Path:
        return self.local_dir / "runs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
