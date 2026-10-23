import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CADENCE_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator("alert_tenants", "apple_proxy_ip_ranges", "known_scanner_uas", mode="before")
    @classmethod
    def _parse_csv_list(cls, v: Any) -> list[str]:
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
    sqs_event_queue_url: str | None = None

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
    alert_tenants: str | list[str] = ["otto"]
    reputation_complaint_rate_warn: float = 0.001
    reputation_complaint_rate_critical: float = 0.003
    reputation_cold_pool_net_negative_runs: int = 3
    gpt_api_credentials_json: str | None = None

    # Scanner / MPP classification
    scanner_confirm_window_s: int = 30
    apple_proxy_ip_ranges: str | list[str] = ["17.0.0.0/8", "17.58.0.0/16"]
    known_scanner_uas: str | list[str] = ["SafeLinks", "Proofpoint", "Barracuda", "DLP-"]
    attribution_window_days: int = 30

    # Google Postmaster Tools (v2). `postmaster_backend`: "google" | "mock".
    # Leaving postmaster_credentials_path unset disables the sync entirely.
    postmaster_backend: str = "google"
    postmaster_credentials_path: str | None = None
    postmaster_domain: str | None = None
    postmaster_lookback_days: int = 7
    postmaster_sync_interval_seconds: int = 21600  # 6h; the API updates daily
    # Gmail reports a ratio: 0.001 == 0.1% (its recommended target) and
    # 0.003 == 0.3% (the hard ceiling above which mitigation is unavailable).
    postmaster_spam_rate_warn: float = 0.001
    postmaster_spam_rate_critical: float = 0.003



@lru_cache
def get_settings() -> Settings:
    return Settings()


_DEFAULT_ENGAGEMENT_CONFIG_PATH = (
    Path(__file__).parent.parent.parent / "config" / "engagement.yaml"
)


class EngagementThresholds(BaseModel):
    active_min_clicks_long: int
    active_min_converts_long: int
    engaged_min_opens_short: int
    passive_min_opens_long: int
    dormant_min_sends_long: int
    cold_min_sends_lifetime: int
    cold_min_tenure_days: int
    new_max_sends_lifetime: int


class EngagementScore(BaseModel):
    weight_click: float
    weight_open: float
    weight_convert: float
    halflife_click_days: float
    halflife_open_days: float
    halflife_convert_days: float
    penalty_complaint: float
    penalty_bounce: float


class EngagementGuardrail(BaseModel):
    max_tier_shift_pct: float = 0.10


class EngagementColdExclusion(BaseModel):
    enabled: bool = True


class EngagementOverridesByProvider(BaseModel):
    model_config = ConfigDict(extra="allow")


class EngagementOverrides(BaseModel):
    by_mailbox_provider: EngagementOverridesByProvider = EngagementOverridesByProvider()


class EngagementConfig(BaseModel):
    window_short_days: int
    window_long_days: int
    thresholds: EngagementThresholds
    score: EngagementScore
    guardrail: EngagementGuardrail = EngagementGuardrail()
    cold_exclusion: EngagementColdExclusion = EngagementColdExclusion()
    overrides: EngagementOverrides = EngagementOverrides()
    tier_config_version: str = ""


def load_engagement_config(path: str | Path | None = None) -> EngagementConfig:
    resolved = Path(path) if path is not None else _DEFAULT_ENGAGEMENT_CONFIG_PATH
    raw_bytes = resolved.read_bytes()
    data = yaml.safe_load(raw_bytes)
    engagement_block = data["engagement"]
    version_hash = hashlib.sha256(
        json.dumps(engagement_block, sort_keys=True).encode()
    ).hexdigest()
    config = EngagementConfig(**engagement_block)
    config = config.model_copy(update={"tier_config_version": version_hash})
    return config


@lru_cache(maxsize=1)
def get_engagement_config() -> EngagementConfig:
    return load_engagement_config()
