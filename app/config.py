from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+asyncpg://bashculture:local-dev-only@localhost:55432/bashculture"
    )
    admin_api_token: SecretStr = SecretStr("")
    event_timezone: str = "Asia/Yekaterinburg"
    max_import_bytes: int = Field(default=2_097_152, ge=1024, le=10_485_760)
    max_import_rows: int = Field(default=2000, ge=1, le=10000)
    session_secure_cookie: bool = False
    public_base_url: str = "http://localhost:8080"
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_bot_username: str = ""
    notification_interval_seconds: int = Field(default=60, ge=1, le=3600)
    notification_daily_limit: int = Field(default=3, ge=1, le=20)
    notification_quiet_start: int = Field(default=22, ge=0, le=23)
    notification_quiet_end: int = Field(default=9, ge=0, le=23)
    llm_api_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_require_api_key: bool = False
    llm_model: str = ""
    llm_free: bool = False
    llm_timeout_seconds: float = Field(default=5, gt=0, le=30)
    llm_max_output_tokens: int = Field(default=700, ge=100, le=2000)
    llm_input_rub_per_million: float = Field(default=0, ge=0)
    llm_output_rub_per_million: float = Field(default=0, ge=0)
    llm_turn_budget_rub: float = Field(default=1, gt=0, le=100)
    llm_dialogue_budget_rub: float = Field(default=3, gt=0, le=1000)
    llm_daily_budget_rub: float = Field(default=20, gt=0, le=10000)
    route_api_url: str = ""
    retention_days: int = Field(default=30, ge=1, le=365)


@lru_cache
def get_settings() -> Settings:
    return Settings()
