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


@lru_cache
def get_settings() -> Settings:
    return Settings()
