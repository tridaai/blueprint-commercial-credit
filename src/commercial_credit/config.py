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
# Round 15: the model gateway's governed data — profiles and the
# price table — ships as files beside the code, loaded root-relative
# (never CWD-relative) by the wiring. A missing or unreadable file
# is a configuration error at startup, not a fallback to built-ins.
MODEL_PROFILES_PATH = REPO_ROOT / "data" / "model_profiles.json"
MODEL_PRICES_PATH = REPO_ROOT / "data" / "model_prices.json"


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    sqlite_path: str
    s3_endpoint_url: str | None
    s3_bucket: str | None
    s3_region: str
    # Round 15: the model gateway. `model_profile` names the
    # deployment's own drafting profile; it is validated against
    # the gateway at wiring time (fail-closed — an unknown or
    # unconfigured profile stops the stack at startup, not on
    # its first call). `extraction_profile`, when set, puts the
    # model-assisted extractor behind the extraction port under
    # that live profile; unset keeps the offline reference
    # extractor.
    model_profile: str = "offline"
    extraction_profile: str | None = None

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
        model_profile=os.environ.get("CREDIT_MODEL_PROFILE") or "offline",
        extraction_profile=os.environ.get("CREDIT_EXTRACTION_PROFILE") or None,
    )
