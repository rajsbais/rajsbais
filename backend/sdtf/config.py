from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Everything is overridable through environment variables so the
    same image runs in dev (SQLite), CI and Kubernetes (PostgreSQL)."""

    database_url: str = field(default_factory=lambda: os.getenv("SDTF_DATABASE_URL", "sqlite:///./data/sdtf.db"))
    auth_secret: str = field(default_factory=lambda: os.getenv("SDTF_AUTH_SECRET", "dev-only-secret-change-me"))
    token_ttl_seconds: int = field(default_factory=lambda: int(os.getenv("SDTF_TOKEN_TTL", "28800")))
    dev_users_enabled: bool = field(default_factory=lambda: os.getenv("SDTF_DEV_USERS", "1") == "1")
    extraction_workers: int = field(default_factory=lambda: int(os.getenv("SDTF_EXTRACTION_WORKERS", "4")))
    evidence_dir: str = field(default_factory=lambda: os.getenv("SDTF_EVIDENCE_DIR", "./data/evidence"))
    environment: str = field(default_factory=lambda: os.getenv("SDTF_ENV", "development"))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(o for o in os.getenv("SDTF_CORS_ORIGINS", "http://localhost:5173").split(",") if o)
    )


settings = Settings()
