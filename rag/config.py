from __future__ import annotations

import redis as redis_lib
from langchain_groq import ChatGroq
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sentence_transformers import SentenceTransformer


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    groq_api_key: SecretStr
    redis_url: str = "redis://localhost:6379"
    groq_model: str = "openai/gpt-oss-120b"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    chunk_size: int = 500
    chunk_overlap: int = 50
    cache_threshold: float = 0.85
    rate_limit_minute: int = 10
    rate_limit_hour: int = 100


_settings: Settings | None = None
_redis: redis_lib.Redis | None = None
_embedder: SentenceTransformer | None = None
_llm: ChatGroq | None = None


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


def get_embedder() -> SentenceTransformer:
    """Local HuggingFace model — downloaded once, then runs on this machine (no API key)."""
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(get_settings().embedding_model)
    return _embedder


def get_llm() -> ChatGroq:
    global _llm
    if _llm is None:
        s = get_settings()
        _llm = ChatGroq(
            model=s.groq_model,
            api_key=s.groq_api_key,
            max_tokens=512,
        )
    return _llm
