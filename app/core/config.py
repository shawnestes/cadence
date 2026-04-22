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

    # ESP / SES
    esp_backend: str = "noop"
    aws_region: str = "us-east-1"
    ses_configuration_set: str | None = None
    ses_sending_domain: str | None = None
    ses_from_address: str | None = None
    ses_managed_pool_name: str | None = None
    sns_topic_arn_ses_events: str | None = None
    sns_verify_signatures: bool = True

    # Unsub
    unsub_signing_key: str
    unsub_base_url: str = "http://localhost:8000"

    # Rate limiting
    send_rate_limit_per_second: int = 14
    send_daily_quota: int = 50000

    # Workflow engine
    workflow_tick_interval_seconds: int = 10

    # AI
    ai_backend: str = "mock"
    anthropic_api_key: str | None = None
    anthropic_generation_model: str = "claude-sonnet-4-5"
    anthropic_classification_model: str = "claude-haiku-4-5"

    # MAB
    mab_min_explore_sends: int = 200
    mab_min_retire_sends: int = 1000
    stats_refresh_interval_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
