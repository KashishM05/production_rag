from __future__ import annotations

import redis as redis_lib
from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    openai_api_key: SecretStr
    redis_url: str = "redis://localhost:6379"
    openai_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    chunk_size: int = 500
    chunk_overlap: int = 50
    cache_threshold: float = 0.85
    rate_limit_minute: int = 10
    rate_limit_hour: int = 100


_settings: Settings | None = None
_redis: redis_lib.Redis | None = None
_openai: AsyncOpenAI | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def get_redis() -> redis_lib.Redis:
    global _redis
    if _redis is None:
        _redis = redis_lib.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


def get_openai() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(api_key=get_settings().openai_api_key.get_secret_value())
    return _openai
