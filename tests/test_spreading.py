"""Round 2: the spreading engine.

Ground truth is the seed borrower's generated figures; the spread is
graded against them exactly (Decimal equality), because a spread that
almost matches the statements is a wrong spread.
"""

import base64
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from commercial_credit.documents import (
    LocalDocumentStore,
    read_document_verified,
    register_document,
)
from commercial_credit.errors import ValidationError
from commercial_credit.extraction import receive_extraction
from commercial_credit.models import (
    AdjustmentReasonCode,
    DocumentKind,
    Provenance,
    SpreadAdjustment,
)
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import ReferenceLosFeed, ground_truth, statement_document
from commercial_credit.spreading import build_spread
from commercial_credit.store import InMemoryCreditStore, SqliteCreditStore


def _open_seed_file(pipeline: CreditPipeline, years=(2022, 2023, 2024)):
    borrower, facility = ReferenceLosFeed().fetch_application("LOS-2026-0117")
    docs = [
        {
            "kind": DocumentKind.FINANCIAL_STATEMENT.value,
            "filename": f"hartline-fy{year}-statements.json",
            "data_hex": statement_document(year).hex(),
            "period_label": f"FY{year}",
        }
        for year in years
    ]
    return pipeline.open_application(borrower, facility, documents=docs)


def _modified_statement(year: int, *, extra_bs_lines=(), cf_lines=(), overrides=None) -> bytes:
    """The seed statement for a year, with lines added or values
    overridden — how a restated or richer statement arrives."""
    payload = json.loads(statement_document(year))
    bs_page = payload["pages"][0]
    next_line = max(line["line"] for line in bs_page["lines"]) + 1
    for label, value in extra_bs_lines:
        bs_page["lines"].append({"line": next_line, "label": label, "value": value})
        next_line += 1
    for label, value in (overrides or {}).items():
        for page in payload["pages"]:
            for line in page["lines"]:
                if line["label"] == label:
                    line["value"] = value
    if cf_lines:
        payload["pages"].append(
            {
                "page": 3,
                "title": "Cash Flow Statement",
                "lines": [
                    {"line": idx, "label": label, "value": value}
                    for idx, (label, value) in enumerate(cf_lines, start=1)
                ],
            }
        )
    return json.dumps(payload, indent=2).encode("utf-8")


def _register_and_extract(pipeline: CreditPipeline, credit_file_id: str, data: bytes,
                          filename: str, period_label: str):
    credit_file = pipeline.store.get_credit_file(credit_file_id)
    doc = register_document(
        pipeline.store,
        pipeline.documents,
        credit_file,
        kind=DocumentKind.FINANCIAL_STATEMENT,
        filename=filename,
        data=data,
        period_label=period_label,
    )
    content = read_document_verified(pipeline.documents, doc)
    values = receive_extraction(pipeline.extractor.extract(doc, content))
    pipeline.store.save_extractions(credit_file_id, values)
    return doc


def _line(spread, key):
    return next(line for line in spread.lines if line.key == key)


# -- the spread against ground truth -----------------------------------


def test_spread_matches_ground_truth_for_all_periods():
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    credit_file = _open_seed_file(pipeline)
    spread = pipeline.get_spread(credit_file.credit_file_id)

    assert spread.periods == [2022, 2023, 2024]
    assert spread.template_version == "aldergrove-spread-v1"
    truth = ground_truth()
    for (year, field), expected in truth.items():
        line = _line(spread, field)
        assert line.values[year] == expected, f"{field} FY{year}"
        # No adjustments yet: the adjusted view is the unadjusted view.
        assert line.adjusted_values[year] == expected
    # Period-over-period presentation data.
    revenue = _line(spread, "revenue")
    assert revenue.changes[2022] is None
    assert revenue.changes[2023] == Decimal("1137000")
    assert revenue.changes[2024] == Decimal("1392000")
    # Every period rests on its own filed document, version 1.
    doc_ids = {d.period_label: d.document_id for d in credit_file.documents}
    for source in spread.period_sources:
        assert source.version == 1
        assert source.superseded_document_ids == []
        assert source.document_id == doc_ids[source.period_label]
        assert source.document_sha256
    assert spread.unmapped == []


def test_spread_build_is_deterministic_and_order_independent():
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    credit_file = _open_seed_file(pipeline)
    first = pipeline.build_spread(credit_file.credit_file_id)
    second = pipeline.build_spread(credit_file.credit_file_id)
    assert first.model_dump_json() == second.model_dump_json()
    assert first.spread_id == second.spread_id
    assert first.spread_id.startswith("spread-")

    extractions = pipeline.store.get_extractions(credit_file.credit_file_id)
    shuffled = build_spread(credit_file, list(reversed(extractions)), [])
    assert shuffled.model_dump_json() == first.model_dump_json()


def test_cash_flow_section_maps_from_extraction():
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    credit_file = _open_seed_file(pipeline, years=(2024,))
    data = _modified_statement(
        2024,
        cf_lines=[
            ("Depreciation and amortization", "212000"),
            ("Net cash provided by operating activities", "884000"),
            ("Capital expenditures", "402000"),
        ],
    )
    _register_and_extract(
        pipeline, credit_file.credit_file_id, data,
        "hartline-fy2024-with-cashflow.json", "FY2024",
    )
    spread = pipeline.build_spread(credit_file.credit_file_id)
    da = _line(spread, "depreciation_and_amortization")
    assert da.section == "cash_flow_statement"
    assert da.values[2024] == Decimal("212000")
    assert _line(spread, "net_cash_from_operations").values[2024] == Decimal("884000")
    assert _line(spread, "capital_expenditures").values[2024] == Decimal("402000")


# -- unmapped lines ------------------------------------------------------


def test_unmapped_lines_are_carried_named_never_dropped():
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    credit_file = _open_seed_file(pipeline, years=(2024,))
    data = _modified_statement(
        2024,
        extra_bs_lines=[("Goodwill", "250000"), ("Other assets", "61500")],
    )
    doc = _register_and_extract(
        pipeline, credit_file.credit_file_id, data,
        "hartline-fy2024-restated.json", "FY2024",
    )
    spread = pipeline.build_spread(credit_file.credit_file_id)

    assert [entry.name for entry in spread.unmapped] == ["Goodwill", "Other assets"]
    goodwill = next(e for e in spread.unmapped if e.name == "Goodwill")
    assert goodwill.values[2024] == Decimal("250000")
    assert goodwill.sources[2024].document_id == doc.document_id
    assert goodwill.sources[2024].page == 1
    # The unmapped figures were not folded into any template line:
    # total assets still reads exactly what the statement stated.
    assert _line(spread, "total_assets").values[2024] == Decimal("7449000")


# -- adjustments ----------------------------------------------------------


def _adjustment(credit_file, **overrides) -> SpreadAdjustment:
    doc = credit_file.documents[0]
    base = dict(
        credit_file_id=credit_file.credit_file_id,
        line_key="ebit",
        fiscal_year=2024,
        amount=Decimal("125000"),
        reason_code=AdjustmentReasonCode.NON_RECURRING_EXPENSE,
        reason="One-time plant relocation cost inside operating expenses",
        source=Provenance(document_id=doc.document_id, page=2, line="L5"),
        proposed_by="credit-officer:j.lee",
    )
    base.update(overrides)
    return SpreadAdjustment(**base)


def test_adjustment_adjusted_and_unadjusted_views():
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    credit_file = _open_seed_file(pipeline)
    pipeline.add_adjustment(credit_file.credit_file_id, _adjustment(credit_file))
    spread = pipeline.add_adjustment(
        credit_file.credit_file_id,
        _adjustment(credit_file, amount=Decimal("25000"),
                    reason="Second add-back: severance from the same relocation"),
    )
    ebit = _line(spread, "ebit")
    assert ebit.values[2024] == Decimal("897600")  # unadjusted view untouched
    assert ebit.adjusted_values[2024] == Decimal("897600") + Decimal("150000")
    assert ebit.values[2023] == ebit.adjusted_values[2023]  # other periods untouched
    assert len(spread.adjustments) == 2
    actions = [e.action for e in pipeline.store.list_audit(credit_file.credit_file_id)]
    assert actions.count("adjustment_recorded") == 2


def test_adjustment_without_provenance_or_reason_is_rejected():
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    credit_file = _open_seed_file(pipeline)
    good = _adjustment(credit_file)

    smuggled_no_source = SpreadAdjustment.model_construct(
        **{**good.model_dump(), "source": None}
    )
    with pytest.raises(ValidationError):
        pipeline.add_adjustment(credit_file.credit_file_id, smuggled_no_source)

    smuggled_no_reason = SpreadAdjustment.model_construct(
        adjustment_id=good.adjustment_id,
        credit_file_id=good.credit_file_id,
        line_key=good.line_key,
        fiscal_year=good.fiscal_year,
        amount=good.amount,
        reason_code=good.reason_code,
        reason="   ",
        source=good.source,
        proposed_by=good.proposed_by,
    )
    with pytest.raises(ValidationError):
        pipeline.add_adjustment(credit_file.credit_file_id, smuggled_no_reason)

    with pytest.raises(ValidationError):
        pipeline.add_adjustment(
            credit_file.credit_file_id,
            _adjustment(credit_file, line_key="not_a_spread_line"),
        )

    with pytest.raises((ValidationError, PydanticValidationError)):
        _adjustment(credit_file, reason="   ")

    # Nothing was recorded and the spread is the unadjusted spread.
    assert pipeline.store.list_adjustments(credit_file.credit_file_id) == []
    spread = pipeline.get_spread(credit_file.credit_file_id)
    assert spread.adjustments == []


# -- restatement -----------------------------------------------------------


def test_restatement_supersedes_period_and_prior_version_stays_retrievable():
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    credit_file = _open_seed_file(pipeline)
    v1_doc = next(d for d in credit_file.documents if d.period_label == "FY2024")

    corrected = _modified_statement(2024, overrides={"Revenue": "11980000"})
    v2_doc = _register_and_extract(
        pipeline, credit_file.credit_file_id, corrected,
        "hartline-fy2024-corrected.json", "FY2024",
    )
    spread = pipeline.build_spread(credit_file.credit_file_id)

    assert _line(spread, "revenue").values[2024] == Decimal("11980000")
    source = next(s for s in spread.period_sources if s.fiscal_year == 2024)
    assert source.version == 2
    assert source.document_id == v2_doc.document_id
    assert source.document_sha256 == v2_doc.sha256
    assert source.superseded_document_ids == [v1_doc.document_id]
    # Other periods still rest on their first versions.
    for other in spread.period_sources:
        if other.fiscal_year != 2024:
            assert other.version == 1
    # The superseded extraction is still in the store, retrievable.
    prior = [
        v for v in pipeline.store.get_extractions(credit_file.credit_file_id)
        if v.field == "revenue" and v.fiscal_year == 2024
        and v.provenance.document_id == v1_doc.document_id
    ]
    assert [v.value for v in prior] == [Decimal("12374000")]


# -- stores + API ------------------------------------------------------------


def test_sqlite_store_persists_spread_and_adjustments(tmp_path):
    path = tmp_path / "credit.db"
    pipeline = CreditPipeline(store=SqliteCreditStore(path), documents=LocalDocumentStore())
    credit_file = _open_seed_file(pipeline)
    pipeline.add_adjustment(credit_file.credit_file_id, _adjustment(credit_file))
    built = pipeline.get_spread(credit_file.credit_file_id)

    reopened = SqliteCreditStore(path)
    loaded = reopened.get_latest_spread(credit_file.credit_file_id)
    assert loaded is not None
    assert loaded.model_dump_json() == built.model_dump_json()
    assert len(reopened.list_adjustments(credit_file.credit_file_id)) == 1
    assert reopened.list_spreads(credit_file.credit_file_id)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from commercial_credit import api as api_module

    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app)


def test_spread_and_adjustments_via_api(client):
    resp = client.post("/applications/from-los", json={"application_ref": "LOS-2026-0117"})
    assert resp.status_code == 201
    credit_file_id = resp.json()["credit_file_id"]
    for year in (2022, 2023, 2024):
        body = {
            "kind": "financial_statement",
            "filename": f"hartline-fy{year}-statements.json",
            "content_b64": base64.b64encode(statement_document(year)).decode("ascii"),
            "period_label": f"FY{year}",
        }
        assert client.post(f"/applications/{credit_file_id}/documents", json=body).status_code == 201
    assert client.post(f"/applications/{credit_file_id}/extract").status_code == 200

    spread = client.get(f"/applications/{credit_file_id}/spread").json()
    assert spread["periods"] == [2022, 2023, 2024]
    revenue = next(line for line in spread["lines"] if line["key"] == "revenue")
    assert Decimal(revenue["values"]["2024"]) == Decimal("12374000")
    doc_id = next(
        s["document_id"] for s in spread["period_sources"] if s["fiscal_year"] == 2024
    )

    posted = client.post(
        f"/applications/{credit_file_id}/adjustments",
        json={
            "line_key": "ebit",
            "fiscal_year": 2024,
            "amount": "125000",
            "reason_code": "non_recurring_expense",
            "reason": "One-time plant relocation cost inside operating expenses",
            "source": {"document_id": doc_id, "page": 2, "line": "L5"},
            "proposed_by": "credit-officer:j.lee",
        },
    )
    assert posted.status_code == 201
    ebit = next(
        line for line in posted.json()["spread"]["lines"] if line["key"] == "ebit"
    )
    assert Decimal(ebit["values"]["2024"]) == Decimal("897600")
    assert Decimal(ebit["adjusted_values"]["2024"]) == Decimal("897600") + Decimal("125000")

    rejected = client.post(
        f"/applications/{credit_file_id}/adjustments",
        json={
            "line_key": "ebit",
            "fiscal_year": 2024,
            "amount": "1",
            "reason_code": "other",
            "reason": "   ",
            "source": {"document_id": doc_id, "page": 2, "line": "L5"},
            "proposed_by": "credit-officer:j.lee",
        },
    )
    assert rejected.status_code == 422
    listed = client.get(f"/applications/{credit_file_id}/adjustments").json()
    assert len(listed) == 1
