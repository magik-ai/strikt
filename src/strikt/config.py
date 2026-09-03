"""Runtime settings.

Every variable is read from the environment (or a local ``.env``) by pydantic-settings and is
documented, with its default, in ``.env.example``. Nothing here is user-specific: per-user
preferences live in the ``profiles`` table and are changed by conversation, never by env vars.
"""

from __future__ import annotations

import re
from datetime import time
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]
TelegramMode = Literal["polling", "webhook"]
LogFormat = Literal["json", "pretty"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://strikt:strikt@postgres:5432/strikt"


class ModelPrice(BaseModel):
    """USD per one million tokens for one model id."""

    input: float
    output: float
    cache_read: float
    cache_write: float


DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-5": ModelPrice(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50),
}


def _split_ids(value: object) -> list[int]:
    """Parse ``"1, 2,3"`` / ``[1, 2]`` / ``""`` into a list of ints."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [int(part) for part in re.split(r"[,\s]+", value.strip()) if part]
    if isinstance(value, list | tuple | set):
        return [int(part) for part in value]
    raise TypeError(f"cannot parse telegram id list from {type(value).__name__}")


class Settings(BaseSettings):
    """All process-level configuration. See ``.env.example`` for the documented list."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---------------------------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    allowed_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    admin_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_mode: TelegramMode = "polling"
    telegram_webhook_secret: SecretStr | None = None

    # --- Anthropic ----------------------------------------------------------------------------
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = DEFAULT_MODEL
    effort_turn: Effort = "medium"
    effort_verify: Effort = "low"
    effort_proactive: Effort = "low"
    effort_summary: Effort = "low"
    effort_research: Effort = "medium"
    max_tokens_turn: int = 8192
    max_tokens_verify: int = 2048
    max_tokens_proactive: int = 1024
    max_tokens_summary: int = 4096
    max_tokens_research: int = 8192
    max_tool_rounds: int = 12
    context_max_turns: int = 30
    context_max_tokens: int = 40_000
    llm_timeout_s: float = 120.0

    # --- OpenAI (voice transcription only) ----------------------------------------------------
    openai_api_key: SecretStr | None = None
    openai_transcription_model: str = "gpt-transcribe"
    openai_transcription_fallback_model: str = "whisper-1"

    # --- Storage ------------------------------------------------------------------------------
    database_url: str = DEFAULT_DATABASE_URL
    token_encryption_key: SecretStr = SecretStr("")

    # --- Web (OAuth callbacks, webhooks, optional Telegram webhook) ---------------------------
    public_base_url: str = "http://localhost:8080"
    web_port: int = 8080

    # --- Integrations -------------------------------------------------------------------------
    whoop_client_id: str | None = None
    whoop_client_secret: SecretStr | None = None
    withings_client_id: str | None = None
    withings_client_secret: SecretStr | None = None
    usda_api_key: SecretStr | None = None
    off_user_agent: str = "Strikt/0.1 (https://github.com/magik-ai/bomiso)"

    # --- Logging ------------------------------------------------------------------------------
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "pretty"

    # --- Cost tracking ------------------------------------------------------------------------
    price_table: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_PRICES))

    # --- Proactive engine ---------------------------------------------------------------------
    proactive_daily_cap: int = 5
    proactive_daily_cap_drill_sergeant: int = 8
    proactive_followup_minutes: int = 45
    quiet_start: time = time(0, 0)
    quiet_end: time = time(7, 30)

    # --- Nutrition ----------------------------------------------------------------------------
    loose_food_buffer: float = 0.25

    @field_validator("allowed_telegram_ids", "admin_telegram_ids", mode="before")
    @classmethod
    def _parse_id_lists(cls, value: object) -> list[int]:
        return _split_ids(value)

    @field_validator("loose_food_buffer")
    @classmethod
    def _buffer_range(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("LOOSE_FOOD_BUFFER must be between 0 and 1 (0.25 = +25%)")
        return value

    # --- Derived helpers ----------------------------------------------------------------------
    @property
    def model(self) -> str:
        """The Claude model id used for every LLM call (PLAN §6.1 calls it ``settings.model``)."""
        return self.anthropic_model

    def price_for(self, model: str | None = None) -> ModelPrice | None:
        """Price row for ``model`` (exact id, then longest prefix match), or None if unknown."""
        target = model or self.anthropic_model
        if target in self.price_table:
            return self.price_table[target]
        candidates = [key for key in self.price_table if target.startswith(key)]
        if not candidates:
            return None
        return self.price_table[max(candidates, key=len)]

    def daily_cap_for(self, intensity: str) -> int:
        return (
            self.proactive_daily_cap_drill_sergeant
            if intensity == "drill_sergeant"
            else self.proactive_daily_cap
        )

    def is_allowed(self, telegram_id: int) -> bool:
        return telegram_id in self.allowed_telegram_ids or telegram_id in self.admin_telegram_ids

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_telegram_ids

    def missing_for_runtime(self) -> list[str]:
        """Names of settings that must be present to run the real bot (not needed for tests)."""
        missing: list[str] = []
        if not self.telegram_bot_token.get_secret_value():
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.anthropic_api_key.get_secret_value():
            missing.append("ANTHROPIC_API_KEY")
        if not self.token_encryption_key.get_secret_value():
            missing.append("TOKEN_ENCRYPTION_KEY")
        if self.telegram_mode == "webhook" and not (
            self.telegram_webhook_secret and self.telegram_webhook_secret.get_secret_value()
        ):
            missing.append("TELEGRAM_WEBHOOK_SECRET")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (tests build ``Settings()`` directly instead)."""
    return Settings()
