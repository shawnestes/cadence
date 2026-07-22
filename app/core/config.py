from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CADENCE_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator("alert_tenants", mode="before")
    @classmethod
    def _parse_alert_tenants(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v

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

    # Frequency governance (addendum 002 §1)
    frequency_cap_per_week: int = 3
    frequency_cap_per_month: int = 8
    send_window_start_hour: int = 9
    send_window_end_hour: int = 20
    send_default_timezone: str = "America/Chicago"

    # Engagement decay (addendum 002 §2)
    engagement_engaged_window_days: int = 30
    engagement_cold_after_unopened: int = 5
    engagement_dormant_after_unopened: int = 10

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

    # Reputation monitoring / alerts
    alert_bounce_warn_rate: float = 0.03
    alert_bounce_critical_rate: float = 0.05
    alert_complaint_warn_rate: float = 0.0007
    alert_complaint_critical_rate: float = 0.001
    alert_sustained_minutes: int = 30
    alert_monitor_interval_seconds: int = 300
    alert_eval_window: str = "24h"
    slack_webhook_url: str | None = None
    alert_tenants: list[str] = ["otto"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
