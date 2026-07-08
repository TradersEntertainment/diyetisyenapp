"""Application configuration loaded from environment variables / .env file."""
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str = ""
    # NoDecode: keep the raw "1,2" string so the validator below splits it
    # (otherwise pydantic-settings tries to JSON-parse the comma list and fails).
    allowed_telegram_ids: Annotated[list[int], NoDecode] = []
    # Shared group chat id. Optional: normally auto-detected from the first
    # group message and persisted in the DB; set to force a specific group.
    group_chat_id: int | None = None

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # Database
    database_url: str = "postgresql+asyncpg://diyetisyen:diyetisyen@localhost:5432/diyetisyen"

    # App
    tz: str = "Europe/Istanbul"
    dashboard_token: str = "change-me"
    log_level: str = "INFO"
    backup_dir: str = "backups"

    @field_validator("allowed_telegram_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
