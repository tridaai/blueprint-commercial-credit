"""Postgres parity mode for the test suite (R16).

The suite's default profile runs store-backed tests on the
in-memory double, plus SQLite replays where a test wants a file
(the store contract, exercised per adapter). That samples the SQL
adapters; it does not prove the whole suite behaves identically
on the production store. Parity mode closes that gap without
rewriting a single test:

    CREDIT_TEST_DATABASE_URL=<redacted>
        uv run pytest -q

When the variable is set, conftest activates this module after
collection (every test module already imported):

- every module attribute bound to the original
  ``InMemoryCreditStore`` class is replaced by a
  ``PostgresCreditStore`` subclass whose instances each get a
  fresh, fully migrated Postgres schema — one schema per store
  instance, so instances are as isolated as the in-memory
  double is (a subclass, not a closure, so the suite's
  surface-introspection tests see the adapter's real methods);
- ``SqliteCreditStore(path)`` is replaced the same way, keyed by
  the resolved path: the same path maps to the same schema, so
  the reopen/persistence tests verify the same property against
  Postgres that they verify against a file;
- ``commercial_credit.wiring``'s ``SqliteCreditStore`` is swapped
  the same way, so a stack built by the wiring with no
  DATABASE_URL (the gateway wiring tests) also lands on Postgres.

Schemas created during a run are dropped at session end (best
effort). The mode is all-or-nothing and loud: if the variable is
set but Postgres is unreachable, activation raises rather than
silently falling back to the default profile.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import quote

import psycopg

from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.db import run_migrations
from commercial_credit.store import (
    InMemoryCreditStore,
    PostgresCreditStore,
    SqliteCreditStore,
)

ENV_VAR = "CREDIT_TEST_DATABASE_URL"

_lock = threading.Lock()
_created_schemas: list[str] = []
_activated = False


def parity_url() -> str | None:
    return os.environ.get(ENV_VAR) or None


def _schema_url(base: str, schema: str) -> str:
    options = quote(f"-c search_path={schema}", safe="")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}options={options}"


def _admin(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True)


def _fresh_factory(base: str) -> type[PostgresCreditStore]:
    """A PostgresCreditStore subclass standing in for the
    in-memory double: each instance gets a fresh migrated schema.
    A class (not a closure) so the suite's surface-introspection
    tests — which assert the exact per-feature method sets of
    every adapter — see the Postgres adapter's real surface."""

    class FreshSchemaStore(PostgresCreditStore):
        def __init__(self) -> None:
            schema = f"t_{uuid.uuid4().hex[:20]}"
            with _admin(base) as conn:
                conn.execute(f'CREATE SCHEMA "{schema}"')
            with _lock:
                _created_schemas.append(schema)
            url = _schema_url(base, schema)
            run_migrations(url, MIGRATIONS_DIR)
            super().__init__(url)

    return FreshSchemaStore


def _path_factory(base: str) -> type[PostgresCreditStore]:
    """A PostgresCreditStore subclass standing in for the SQLite
    file store: instances over the same resolved path share one
    schema, so reopen/persistence semantics carry over."""

    class PathSchemaStore(PostgresCreditStore):
        def __init__(self, path: str | Path) -> None:
            key = str(Path(path).resolve())
            digest = hashlib.sha256(key.encode()).hexdigest()[:20]
            schema = f"p_{digest}"
            with _lock:
                if schema not in _created_schemas:
                    with _admin(base) as conn:
                        conn.execute(
                            f'CREATE SCHEMA IF NOT EXISTS "{schema}"'
                        )
                    _created_schemas.append(schema)
                    url = _schema_url(base, schema)
                    run_migrations(url, MIGRATIONS_DIR)
                else:
                    url = _schema_url(base, schema)
            super().__init__(url)

    return PathSchemaStore


def activate() -> bool:
    """Swap the test doubles for Postgres stores. True if active."""
    global _activated
    if _activated:
        return True
    base = parity_url()
    if not base:
        return False
    # Loud, not a fallback: a named-but-unreachable server fails
    # the run here rather than quietly testing the doubles.
    try:
        with _admin(base) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        raise RuntimeError(
            f"{ENV_VAR} is set but Postgres is unreachable: {exc}"
        ) from exc
    fresh = _fresh_factory(base)
    by_path = _path_factory(base)
    for module in list(sys.modules.values()):
        if module is None:
            continue
        for name, original, replacement in (
            ("InMemoryCreditStore", InMemoryCreditStore, fresh),
            ("SqliteCreditStore", SqliteCreditStore, by_path),
        ):
            try:
                if getattr(module, name, None) is original:
                    setattr(module, name, replacement)
            except Exception:
                continue
    _activated = True
    return True


def cleanup() -> None:
    """Drop the run's schemas (best effort, at session end)."""
    base = parity_url()
    if not base or not _created_schemas:
        return
    try:
        with _admin(base) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND pid <> pg_backend_pid()"
            )
            for schema in _created_schemas:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    except Exception:
        pass
    _created_schemas.clear()
