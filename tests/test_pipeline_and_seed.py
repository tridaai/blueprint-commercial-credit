from decimal import Decimal

from commercial_credit.documents import LocalDocumentStore
from commercial_credit.models import DocumentKind
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import (
    HARTLINE_FIGURES,
    ReferenceLosFeed,
    ground_truth,
    statement_document,
)
from commercial_credit.store import InMemoryCreditStore


def _run_seed_pipeline() -> CreditPipeline:
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    borrower, facility = ReferenceLosFeed().fetch_application("LOS-2026-0117")
    docs = [
        {
            "kind": DocumentKind.FINANCIAL_STATEMENT.value,
            "filename": f"hartline-fy{year}-statements.json",
            "data_hex": statement_document(year).hex(),
            "period_label": f"FY{year}",
        }
        for year in (2022, 2023, 2024)
    ]
    pipeline.open_application(borrower, facility, documents=docs)
    return pipeline


def test_seed_statements_balance_and_foot():
    for year, f in HARTLINE_FIGURES.items():
        d = {k: Decimal(v) for k, v in f.items()}
        assert d["total_current_assets"] + d["net_fixed_assets"] == d["total_assets"], year
        assert d["total_liabilities"] + d["total_equity"] == d["total_assets"], year
        assert d["revenue"] - d["cost_of_goods_sold"] == d["gross_profit"], year
        assert d["gross_profit"] - d["operating_expenses"] == d["ebit"], year


def test_pipeline_extracts_every_statement_line_for_three_years():
    pipeline = _run_seed_pipeline()
    files = pipeline.store.list_credit_files()
    assert len(files) == 1
    values = pipeline.store.get_extractions(files[0].credit_file_id)
    # 19 statement fields × 3 fiscal years.
    assert len(values) == 57
    truth = ground_truth()
    for v in values:
        assert truth[(v.fiscal_year, v.field)] == v.value


def test_pipeline_records_audit_trail():
    pipeline = _run_seed_pipeline()
    files = pipeline.store.list_credit_files()
    actions = [e.action for e in pipeline.store.list_audit(files[0].credit_file_id)]
    assert "application_intake" in actions
    assert "checklist_generated" in actions
    assert actions.count("document_registered") == 3
    assert actions.count("extraction_completed") == 3


def test_los_feed_rejects_unknown_reference():
    import pytest

    from commercial_credit.errors import NotFoundError

    with pytest.raises(NotFoundError):
        ReferenceLosFeed().fetch_application("LOS-0000-0000")
