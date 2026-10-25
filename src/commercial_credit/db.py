"""PostgreSQL migrations: numbered SQL files + a startup runner.

`migrations/` at the repo root is the single source of schema truth
for the Postgres profile. The runner applies each file once, in
numeric order, recording it in `schema_migrations`; it runs at API
startup so a fresh database reaches the current schema with no
manual step. The SQLite/in-memory stores are test doubles and keep
their own minimal schemas in store.py — they never substitute for
this DDL in a deployment.
"""

from __future__ import annotations

import re
from pathlib import Path

from .errors import StoreUnavailableError

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def migration_files(migrations_dir: Path) -> list[Path]:
    files = [p for p in migrations_dir.glob("*.sql") if _MIGRATION_RE.match(p.name)]
    return sorted(files, key=lambda p: p.name)


def run_migrations(database_url: str, migrations_dir: Path) -> list[str]:
    """Apply pending migrations; return the names applied this run."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise StoreUnavailableError("psycopg is not installed") from exc
    applied: list[str] = []
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {
                row[0]
                for row in conn.execute("SELECT name FROM schema_migrations")
            }
            for path in migration_files(migrations_dir):
                if path.name in done:
                    continue
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
                )
                applied.append(path.name)
    except Exception as exc:
        raise StoreUnavailableError(f"migration run failed: {exc}") from exc
    return applied
