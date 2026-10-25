from pathlib import Path

from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.db import migration_files
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.models import DocumentKind
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import ReferenceLosFeed, statement_document
from commercial_credit.store import SqliteCreditStore


def test_sqlite_store_round_trip(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    pipeline = CreditPipeline(store=store, documents=LocalDocumentStore())
    borrower, facility = ReferenceLosFeed().fetch_application("LOS-2026-0117")
    docs = [{
        "kind": DocumentKind.FINANCIAL_STATEMENT.value,
        "filename": "hartline-fy2024-statements.json",
        "data_hex": statement_document(2024).hex(),
        "period_label": "FY2024",
    }]
    credit_file = pipeline.open_application(borrower, facility, documents=docs)
    reopened = SqliteCreditStore(tmp_path / "credit.db")
    loaded = reopened.get_credit_file(credit_file.credit_file_id)
    assert loaded is not None
    assert loaded.borrower.legal_name == "Hartline Manufacturing, Inc."
    assert len(reopened.get_extractions(credit_file.credit_file_id)) == 19
    assert reopened.list_audit(credit_file.credit_file_id)
    assert reopened.ping() is True


def test_migrations_are_numbered_and_present():
    files = migration_files(MIGRATIONS_DIR)
    assert files, "no migration files found"
    assert files[0].name.startswith("0001_")
    names = [f.name for f in files]
    assert names == sorted(names)
    sql = Path(files[0]).read_text()
    assert "credit_files" in sql and "extractions" in sql and "audit_events" in sql
