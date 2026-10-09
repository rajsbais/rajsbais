from __future__ import annotations

import json
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
    staging_backend: str = field(default_factory=lambda: os.getenv("SDTF_STAGING_BACKEND", "relational"))  # relational | columnar
    staging_dir: str = field(default_factory=lambda: os.getenv("SDTF_STAGING_DIR", "./data/staging"))  # local path, file://, s3://bucket/prefix, gs://bucket/prefix, memory://
    staging_fs_options: dict = field(default_factory=lambda: json.loads(os.getenv("SDTF_STAGING_FS_OPTIONS", "{}") or "{}"))  # fsspec options, e.g. {"endpoint_url": "http://minio:9000"}
    graph_backend: str = field(default_factory=lambda: os.getenv("SDTF_GRAPH_BACKEND", "relational"))  # relational | neo4j
    load_mode: str = field(default_factory=lambda: os.getenv("SDTF_LOAD_MODE", "api"))  # api (released-API loaders over the target transport) | direct (simulated direct loader)
    neo4j_uri: str = field(default_factory=lambda: os.getenv("SDTF_NEO4J_URI", ""))
    neo4j_user: str = field(default_factory=lambda: os.getenv("SDTF_NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: os.getenv("SDTF_NEO4J_PASSWORD", ""))
    neo4j_database: str = field(default_factory=lambda: os.getenv("SDTF_NEO4J_DATABASE", ""))
    job_lease_seconds: int = field(default_factory=lambda: int(os.getenv("SDTF_JOB_LEASE_SECONDS", "300")))
    worker_poll_seconds: float = field(default_factory=lambda: float(os.getenv("SDTF_WORKER_POLL_SECONDS", "1.0")))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(o for o in os.getenv("SDTF_CORS_ORIGINS", "http://localhost:5173").split(",") if o)
    )


settings = Settings()
