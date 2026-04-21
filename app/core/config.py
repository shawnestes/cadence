from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CADENCE_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str
    log_level: str = "INFO"
    environment: str = "dev"
    app_name: str = "cadence"
    api_key: str
    otto_unsub_endpoint: str | None = None
    otto_api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
