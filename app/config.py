"""Application configuration loaded from environment variables.

Every secret / deployment-specific value comes from the environment so that
nothing sensitive is ever committed to version control.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram -----------------------------------------------------------
    telegram_bot_token: str = ""
    webhook_secret_token: str = ""          # checked against X-Telegram-Bot-Api-Secret-Token
    webhook_url: str = ""                    # public HTTPS URL of this app, e.g. https://app.onrender.com

    # --- LLM provider (OpenAI-compatible chat completions) ------------------
    llm_api_base: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2                 # cheap retry budget; free tiers are NOT unlimited
    llm_temperature: float = 0.85
    llm_max_tokens: int = 500

    # --- PostgreSQL ----------------------------------------------------------
    database_url: str = "postgres://localhost:5432/companion_bot"

    # --- Conversation limits --------------------------------------------------
    history_window: int = 20                 # messages sent to the model per request
    max_message_length: int = 6000           # reject oversized inbound messages
    memory_limit_per_user: int = 200         # cap long-term memories per user

    # --- Rate limiting (per user) ---------------------------------------------
    rate_limit_messages: int = 12            # messages ...
    rate_limit_window_seconds: int = 60      # ... per window

    # --- Misc -------------------------------------------------------------------
    log_level: str = "INFO"

    @property
    def telegram_api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.telegram_bot_token}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
