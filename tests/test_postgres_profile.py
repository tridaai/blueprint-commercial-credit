"""The Postgres production profile, graded directly (R16).

These tests are profile-conditional by design: without
``CREDIT_TEST_DATABASE_URL`` they skip (and the skip is visible in
the suite output — never silent). With it, they grade the two
things parity mode alone does not:

- the migration runner on a real, empty Postgres database —
  every numbered migration applies, in order, and a second run
  applies nothing (idempotent startup);
- the deployment store itself — a credit file written through one
  ``PostgresCreditStore`` instance is read back, whole, through a
  second instance over a separate connection pool path, the way a
  restarted process meets the same database.

In parity mode itself these tests are excluded from the
cross-profile outcome comparison in the eval gate: they are the
profile-conditional leg, not shared behaviour.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

import pg_parity
from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.db import migration_files, run_migrations
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import ReferenceLosFeed, statement_document
from commercial_credit.models import DocumentKind
from commercial_credit.store import PostgresCreditStore

pytestmark = pytest.mark.skipif(
    not pg_parity.parity_url(),
    reason="CREDIT_TEST_DATABASE_URL not set — Postgres profile not graded",
)


def _fresh_database() -> str:
    """A new empty database (UTF8, template0) on the parity server."""
    base = pg_parity.parity_url()
    name = f"credit_profile_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(
            f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING \'UTF8\''
        )
    return base.rsplit("/", 1)[0] + f"/{name}"


def _drop_database(url: str) -> None:
    base = pg_parity.parity_url()
    name = url.rsplit("/", 1)[1].split("?")[0]
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_migrations_apply_from_empty_and_rerun_is_a_noop():
    url = _fresh_database()
    try:
        expected = [p.name for p in migration_files(MIGRATIONS_DIR)]
        assert len(expected) == 13
        first = run_migrations(url, MIGRATIONS_DIR)
        assert first == expected
        second = run_migrations(url, MIGRATIONS_DIR)
        assert second == []
        with psycopg.connect(url) as conn:
            recorded = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM schema_migrations ORDER BY name"
                )
            ]
        assert recorded == sorted(expected)
    finally:
        _drop_database(url)


def test_postgres_store_persists_a_credit_file_across_instances():
    url = _fresh_database()
    try:
        run_migrations(url, MIGRATIONS_DIR)
        store = PostgresCreditStore(url)
        pipeline = CreditPipeline(store=store, documents=LocalDocumentStore())
        borrower, facility = ReferenceLosFeed().fetch_application(
            "LOS-2026-0117"
        )
        docs = [{
            "kind": DocumentKind.FINANCIAL_STATEMENT.value,
            "filename": "hartline-fy2024-statements.json",
            "data_hex": statement_document(2024).hex(),
            "period_label": "FY2024",
        }]
        credit_file = pipeline.open_application(
            borrower, facility, documents=docs
        )

        # A second store instance — a restarted process meeting
        # the same database — reads the file back whole.
        reopened = PostgresCreditStore(url)
        loaded = reopened.get_credit_file(credit_file.credit_file_id)
        assert loaded is not None
        assert loaded.borrower.legal_name == "Hartline Manufacturing, Inc."
        assert len(reopened.get_extractions(credit_file.credit_file_id)) == 19
        assert reopened.list_audit(credit_file.credit_file_id)
        assert reopened.ping() is True
    finally:
        _drop_database(url)
