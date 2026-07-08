"""Shared AsyncAnthropic client."""
from functools import lru_cache

from anthropic import AsyncAnthropic

from app.config import get_settings


@lru_cache
def get_client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=get_settings().anthropic_api_key)


def get_model() -> str:
    return get_settings().anthropic_model
