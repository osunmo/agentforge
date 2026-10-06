from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    database_url: str
    cors_origins: tuple[str, ...]
    ollama_base_url: str
    openrouter_base_url: str
    openrouter_api_key: str | None
    redis_url: str
    master_key: str | None
    oauth_session_ttl_seconds: int
    gmail_api_base_url: str


def load_settings() -> Settings:
    origins = tuple(
        origin.strip()
        for origin in os.getenv(
            "AGENTFORGE_CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
        if origin.strip()
    )
    return Settings(
        environment=os.getenv("AGENTFORGE_ENV", "development"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./agentforge.db"),
        cors_origins=origins,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"),
        openrouter_base_url=os.getenv(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ).rstrip("/"),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        master_key=os.getenv("AGENTFORGE_MASTER_KEY") or None,
        oauth_session_ttl_seconds=max(
            60, min(int(os.getenv("OAUTH_SESSION_TTL_SECONDS", "600")), 600)
        ),
        gmail_api_base_url=os.getenv(
            "GMAIL_API_BASE_URL", "https://gmail.googleapis.com/gmail/v1"
        ).rstrip("/"),
    )


settings = load_settings()
