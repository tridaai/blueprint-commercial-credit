"""Runtime configuration, from the environment only.

Store selection mirrors the portfolio rule: DATABASE_URL set means
the Postgres profile (migrations run at startup); otherwise the
local SQLite store serves single-node runs, and tests inject
in-memory doubles. The readiness payload names which profile is
live, so a deployment never mistakes a double for the real store.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    sqlite_path: str
    s3_endpoint_url: str | None
    s3_bucket: str | None
    s3_region: str

    @property
    def store_profile(self) -> str:
        return "postgres" if self.database_url else "sqlite"


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL") or None,
        sqlite_path=os.environ.get("CREDIT_SQLITE_PATH", "./data/credit.db"),
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
        s3_bucket=os.environ.get("S3_BUCKET") or None,
        s3_region=os.environ.get("S3_REGION", "us-east-1"),
    )
