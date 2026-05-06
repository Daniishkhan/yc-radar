from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "YC Radar"
    app_env: str = "development"
    data_dir: Path = Field(default=Path("data"), validation_alias="DATA_DIR")
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", validation_alias="OPENAI_MODEL")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def targets_csv_path(self) -> Path:
        return self.data_dir / "yc_companies_prototype_targets.csv"

    @property
    def companies_csv_path(self) -> Path:
        return self.data_dir / "yc_companies.csv"


@lru_cache
def get_settings() -> Settings:
    return Settings()
