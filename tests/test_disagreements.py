"""Round 8: document disagreements — detection, precedence without
concealment, computation flags, and the memo's structural duty to
carry them (design law 5).

Hand-decided anchors, from the seed fixture's own figures:

  Hartline FY2024, three witnesses.
    Audited:      revenue 12,374,000 · COGS 8,661,800 · gross
                  profit 3,712,200 · EBIT 897,600 · net income
                  484,600 · inventory 1,932,000 · total current
                  assets 3,951,000.
    Tax return:   revenue 11,905,000 · COGS 8,412,000 · gross
                  profit 3,493,000 · EBIT 678,400 · net income
                  402,300; operating expenses and interest agree
                  with the audit exactly.
    Management:   revenue 12,410,000 · inventory 1,945,500 ·
                  total current assets 3,964,500; everything else
                  as audited.
  Under the disagreement policy (zero tolerance audited-vs-tax on
  revenue/EBIT/net income; 0.1% relative tolerance elsewhere;
  material at 2% or $50,000) that is exactly SEVEN disagreements:
  five material — revenue (tax −469,000, −3.79%), COGS (tax
  −249,800, −2.88%), gross profit (−219,200, −5.90%), EBIT
  (−219,200, −24.42%), net income (−82,300, −16.98%) — and two
  immaterial (inventory +13,500, +0.70%; total current assets
  +13,500, +0.34%, both management-vs-audited). The working figure
  on every contested line is the audited one: precedence, never
  an average (the revenue average would be 12,229,666.67 — a
  figure nobody reported).

  With depreciation & amortisation of 412,300 on the FY2024
  spread, EBITDA is 897,600 + 412,300 = 1,309,900 and rests on the
  disagreed EBIT line, so it and DSCR must carry the EBIT
  disagreement's flag; the current ratio rests on lines whose
  only disagreements are immaterial, so it must not.
"""

from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.checklist import generate_checklist
from commercial_credit.computations import (
    compute_entity_pack,
    compute_global_pack,
    ratio,
    verify_trace,
)
from commercial_credit.disagreements import (
    SOURCE_AUDITED,
    SOURCE_MANAGEMENT,
    SOURCE_TAX,
)
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.errors import MemoVerificationError, ValidationError
from commercial_credit.extraction import OfflineReferenceExtractor
from commercial_credit.memo import (
    MemoInputs,
    compose_memo,
    render_disagreement_paragraph,
    verify_memo,
)
from commercial_credit.models import (
    Borrower,
    BorrowerSegment,
    CreditFile,
    DocumentKind,
    ExtractedValue,
    FacilityRequest,
    FacilityType,
    Provenance,
    SourceDocument,
)
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.policy import evaluate_policy
from commercial_credit.seed import (
    HARTLINE_FIGURES,
    HARTLINE_MANAGEMENT_OVERRIDES,
    HARTLINE_TAX_FIGURES,
    ReferenceLosFeed,
    hartline_disagreement_documents,
    management_statement_document,
    statement_document,
    tax_return_document,
)
from commercial_credit.spreading import build_spread
from commercial_credit.store import InMemoryCreditStore

AS_OF = date(2025, 6, 30)
D = Decimal


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _doc(
    document_id: str, kind: DocumentKind, sha: str,
    credit_file_id: str = "cf-r8",
) -> SourceDocument:
    return SourceDocument(
        document_id=document_id,
        credit_file_id=credit_file_id,
        kind=kind,
        filename=f"{document_id}.json",
        sha256=sha,
        byte_size=1024,
        content_type="application/json",
        storage_key=f"mem://{document_id}",
        period_label="FY2024",
    )


def _value(document_id, field, value, year, source_type, *, page=1, line="L1"):
    return ExtractedValue(
        field=field,
        value=D(value),
        period_label=f"FY{year}",
        fiscal_year=year,
        provenance=Provenance(document_id=document_id, page=page, line=line),
        source_type=source_type,
    )


def two_source_file():
    """A credit file whose FY2024 is reported by three sources,
    with hand-built extractions (the pipeline fixture covers the
    rendered-document route). FY2023 is audited-only and complete,
    so FY2024 debt service has its prior-period input."""
    documents = [
        _doc("aud-2023", DocumentKind.FINANCIAL_STATEMENT, "dd" * 32),
        _doc("aud-2024", DocumentKind.FINANCIAL_STATEMENT, "aa" * 32),
        _doc("tax-2024", DocumentKind.TAX_RETURN, "bb" * 32),
        _doc("mgmt-2024", DocumentKind.FINANCIAL_STATEMENT, "cc" * 32),
    ]
    facility = FacilityRequest(
        facility_type=FacilityType.TERM_LOAN,
        amount=D("1500000"),
        purpose="Refinance existing term debt",
        term_months=60,
    )
    credit_file = CreditFile(
        credit_file_id="cf-r8",
        borrower=Borrower(
            legal_name="Fixture Borrower, LLC",
            ein_last4="1234",
            segment=BorrowerSegment.MANUFACTURING,
            state_of_formation="OH",
            years_in_business=9,
            primary_contact_name="Pat Fixture",
            primary_contact_email="pat@fixture.example",
        ),
        facility=facility,
        documents=documents,
    )
    credit_file.checklist = generate_checklist(facility)

    values: list[ExtractedValue] = []
    for field, value in HARTLINE_FIGURES[2023].items():
        values.append(_value("aud-2023", field, value, 2023, SOURCE_AUDITED))
    for field, value in HARTLINE_FIGURES[2024].items():
        values.append(_value("aud-2024", field, value, 2024, SOURCE_AUDITED))
    values.append(_value(
        "aud-2024", "depreciation_and_amortization", "412300", 2024,
        SOURCE_AUDITED, page=3,
    ))
    for field, value in HARTLINE_TAX_FIGURES[2024].items():
        values.append(_value("tax-2024", field, value, 2024, SOURCE_TAX))
    for field, value in HARTLINE_MANAGEMENT_OVERRIDES[2024].items():
        values.append(_value("mgmt-2024", field, value, 2024, SOURCE_MANAGEMENT))
    return credit_file, values


def hartline_pipeline(with_disagreement_docs: bool = True) -> CreditPipeline:
    pipeline = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
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
    if with_disagreement_docs:
        docs += hartline_disagreement_documents()
    credit_file = pipeline.open_application(borrower, facility, documents=docs)
    return pipeline, credit_file


def hartline_disagreements(pipeline, credit_file):
    spread = pipeline.get_spread(credit_file.credit_file_id)
    return spread, {d.line_key: d for d in spread.disagreements}


# ---------------------------------------------------------------------
# Extraction: the sources are stamped, the returns are read
# ---------------------------------------------------------------------


def test_extractor_reads_tax_returns_and_stamps_source_types():
    extractor = OfflineReferenceExtractor()
    audited_doc = _doc("aud-2024", DocumentKind.FINANCIAL_STATEMENT, "aa" * 32)
    tax_doc = _doc("tax-2024", DocumentKind.TAX_RETURN, "bb" * 32)
    mgmt_doc = _doc("mgmt-2024", DocumentKind.FINANCIAL_STATEMENT, "cc" * 32)

    audited = extractor.extract(audited_doc, statement_document(2024))
    assert audited and all(v.source_type == SOURCE_AUDITED for v in audited)

    taxed = extractor.extract(tax_doc, tax_return_document(2024))
    assert len(taxed) == len(HARTLINE_TAX_FIGURES[2024]) == 7
    assert all(v.source_type == SOURCE_TAX for v in taxed)
    by_field = {v.field: v.value for v in taxed}
    assert by_field["revenue"] == D("11905000")
    assert by_field["net_income"] == D("402300")

    managed = extractor.extract(mgmt_doc, management_statement_document(2024))
    assert managed and all(v.source_type == SOURCE_MANAGEMENT for v in managed)
    by_field = {v.field: v.value for v in managed}
    assert by_field["revenue"] == D("12410000")
    assert by_field["inventory"] == D("1945500")


# ---------------------------------------------------------------------
# Detection: graded exactly on the fixture
# ---------------------------------------------------------------------


def test_detection_grades_the_fixture_exactly():
    pipeline, credit_file = hartline_pipeline()
    spread, by_line = hartline_disagreements(pipeline, credit_file)

    assert spread.disagreement_policy_version == "v1"
    assert set(by_line) == {
        "revenue", "cost_of_goods_sold", "gross_profit", "ebit",
        "net_income", "inventory", "total_current_assets",
    }
    material = {k for k, d in by_line.items() if d.material}
    assert material == {
        "revenue", "cost_of_goods_sold", "gross_profit", "ebit",
        "net_income",
    }
    assert {k for k, d in by_line.items() if not d.material} == {
        "inventory", "total_current_assets",
    }
    # The witnesses agree on these two lines exactly: no record.
    assert "operating_expenses" not in by_line
    assert "interest_expense" not in by_line
    # FY2022/FY2023 have a single witness: nothing to disagree with.
    assert all(d.fiscal_year == 2024 for d in spread.disagreements)

    revenue = by_line["revenue"]
    assert revenue.working_source_type == SOURCE_AUDITED
    assert revenue.working_value == D("12374000")
    tax = revenue.source(SOURCE_TAX)
    assert tax is not None
    assert tax.value == D("11905000")
    assert tax.delta_abs == D("469000")
    assert tax.delta_rel == D("469000") / D("12374000")
    mgmt = revenue.source(SOURCE_MANAGEMENT)
    assert mgmt is not None and mgmt.value == D("12410000")
    assert mgmt.delta_abs == D("36000")
    # Provenance is carried in full: document id + the SHA on file.
    docs = {d.document_id: d for d in credit_file.documents}
    for source in revenue.sources:
        assert source.document_sha256 == docs[source.document_id].sha256
        assert source.provenance.document_id == source.document_id

    ebit = by_line["ebit"]
    assert ebit.source(SOURCE_TAX).delta_abs == D("219200")
    inventory = by_line["inventory"]
    assert inventory.working_value == D("1932000")
    assert inventory.source(SOURCE_MANAGEMENT).delta_abs == D("13500")


def test_working_values_are_the_audited_figures_never_an_average():
    pipeline, credit_file = hartline_pipeline()
    spread, _ = hartline_disagreements(pipeline, credit_file)
    line = next(l for l in spread.lines if l.key == "revenue")
    assert line.values[2024] == D("12374000")
    assert line.adjusted_values[2024] == D("12374000")
    average = (D("12374000") + D("11905000") + D("12410000")) / 3
    assert line.values[2024] != average


def test_period_sources_record_each_type_and_its_version():
    pipeline, credit_file = hartline_pipeline()
    spread, _ = hartline_disagreements(pipeline, credit_file)
    fy2024 = {
        ps.source_type: ps for ps in spread.period_sources
        if ps.fiscal_year == 2024
    }
    assert set(fy2024) == {SOURCE_AUDITED, SOURCE_TAX, SOURCE_MANAGEMENT}
    assert all(ps.version == 1 for ps in fy2024.values())
    fy2023 = [ps for ps in spread.period_sources if ps.fiscal_year == 2023]
    assert len(fy2023) == 1 and fy2023[0].source_type == SOURCE_AUDITED


# ---------------------------------------------------------------------
# Precedence and the restatement boundary
# ---------------------------------------------------------------------


def test_precedence_does_not_depend_on_registration_order():
    credit_file, values = two_source_file()
    # Tax return registered BEFORE the audited statements.
    credit_file.documents = [
        credit_file.documents[2], credit_file.documents[3],
        credit_file.documents[0], credit_file.documents[1],
    ]
    spread = build_spread(credit_file, list(reversed(values)), [])
    assert next(l for l in spread.lines if l.key == "revenue").values[2024] == D("12374000")
    record = next(d for d in spread.disagreements if d.line_key == "revenue")
    assert record.working_source_type == SOURCE_AUDITED


def test_a_tax_return_cannot_restate_audited_figures_away():
    documents = [
        _doc("aud-v1", DocumentKind.FINANCIAL_STATEMENT, "a1" * 32),
        _doc("tax-2024", DocumentKind.TAX_RETURN, "bb" * 32),
        _doc("aud-v2", DocumentKind.FINANCIAL_STATEMENT, "a2" * 32),
    ]
    facility = FacilityRequest(
        facility_type=FacilityType.TERM_LOAN, amount=D("1000000"),
        purpose="Equipment", term_months=60,
    )
    credit_file = CreditFile(
        credit_file_id="cf-r8-restate",
        borrower=Borrower(
            legal_name="Restate Borrower, LLC", ein_last4="9999",
            segment=BorrowerSegment.MANUFACTURING, state_of_formation="OH",
            years_in_business=5, primary_contact_name="Rae Restate",
            primary_contact_email="rae@example.com",
        ),
        facility=facility,
        documents=documents,
    )
    values = [
        _value("aud-v1", "revenue", "12000000", 2024, SOURCE_AUDITED),
        _value("tax-2024", "revenue", "11000000", 2024, SOURCE_TAX),
        _value("aud-v2", "revenue", "12300000", 2024, SOURCE_AUDITED),
    ]
    spread = build_spread(credit_file, values, [])

    # The audited restatement supersedes within its own type...
    assert next(l for l in spread.lines if l.key == "revenue").values[2024] == D("12300000")
    audited = next(
        ps for ps in spread.period_sources
        if ps.fiscal_year == 2024 and ps.source_type == SOURCE_AUDITED
    )
    assert audited.document_id == "aud-v2"
    assert audited.version == 2
    assert audited.superseded_document_ids == ["aud-v1"]
    # ...and the tax return still stands beside it, disagreeing
    # with the RESTATED figure. It restated nothing away.
    (record,) = spread.disagreements
    assert record.line_key == "revenue"
    assert record.working_value == D("12300000")
    assert record.source(SOURCE_TAX).value == D("11000000")
    assert record.material


def test_spread_rebuild_is_deterministic_disagreements_included():
    credit_file, values = two_source_file()
    first = build_spread(credit_file, values, [])
    second = build_spread(credit_file, list(values), [])
    assert first.spread_id == second.spread_id
    assert [d.disagreement_id for d in first.disagreements] == [
        d.disagreement_id for d in second.disagreements
    ]
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


# ---------------------------------------------------------------------
# Computation flags
# ---------------------------------------------------------------------


def _two_source_pack():
    credit_file, values = two_source_file()
    spread = build_spread(credit_file, values, [])
    pack = compute_entity_pack(
        credit_file_id=credit_file.credit_file_id,
        entity="Fixture Borrower, LLC",
        spread=spread,
        extractions=values,
    )
    return credit_file, spread, values, pack


def _figure(pack, key, year=2024):
    return next(
        f for f in pack.figures if f.key == key and f.fiscal_year == year
    )


def test_figures_over_materially_disagreed_lines_are_flagged():
    credit_file, spread, values, pack = _two_source_pack()
    by_line = {d.line_key: d for d in spread.disagreements}

    ebitda = _figure(pack, "ebitda")
    assert ebitda.status == "computed"
    assert ebitda.value == D("1309900")  # 897,600 + 412,300
    assert ebitda.disagreement_ids == [by_line["ebit"].disagreement_id]

    dscr = _figure(pack, "dscr")
    assert dscr.status == "computed"
    expected = ratio(
        D("1309900")
        / (D(HARTLINE_FIGURES[2023]["current_portion_ltd"]) + D("251700"))
    )
    assert dscr.value == expected
    assert dscr.disagreement_ids == [by_line["ebit"].disagreement_id]

    gross_margin = _figure(pack, "gross_margin")
    assert set(gross_margin.disagreement_ids) == {
        by_line["revenue"].disagreement_id,
        by_line["gross_profit"].disagreement_id,
    }

    # The discriminator: lines whose only disagreements are
    # immaterial flag nothing.
    assert _figure(pack, "current_ratio").disagreement_ids == []
    assert _figure(pack, "working_capital").disagreement_ids == []
    # And a period with a single witness flags nothing either.
    assert _figure(pack, "current_ratio", 2023).disagreement_ids == []

    # The trace still re-performs end to end, flags and all.
    report = verify_trace(pack, spread=spread)
    assert report.figures_checked > 0
    assert report.completeness == 1.0, report.gaps


def test_global_pack_inherits_the_borrowers_flags():
    credit_file, spread, values, pack = _two_source_pack()
    by_line = {d.line_key: d for d in spread.disagreements}
    audited_only = [v for v in values if v.source_type == SOURCE_AUDITED]
    clean_file = credit_file.model_copy(
        update={"credit_file_id": "cf-r8-guarantor"}
    )
    clean_spread = build_spread(clean_file, audited_only, [])
    assert clean_spread.disagreements == []
    guarantor_pack = compute_entity_pack(
        credit_file_id=clean_file.credit_file_id,
        entity="Guarantor, LLC",
        spread=clean_spread,
        extractions=audited_only,
    )
    global_pack = compute_global_pack(
        credit_file_id=credit_file.credit_file_id,
        borrower_pack=pack,
        entity_packs={"Guarantor, LLC": guarantor_pack},
    )
    global_ebitda = _figure(global_pack, "global_cash_flow")
    assert global_ebitda.status == "computed"
    assert global_ebitda.disagreement_ids == [
        by_line["ebit"].disagreement_id
    ]
    global_dscr = _figure(global_pack, "global_dscr")
    assert global_dscr.disagreement_ids == [by_line["ebit"].disagreement_id]


# ---------------------------------------------------------------------
# The memo: carries them, or is refused
# ---------------------------------------------------------------------


def _fixture_memo_inputs():
    credit_file, spread, values, pack = _two_source_pack()
    report = evaluate_policy(credit_file, pack, policy_version="v1", as_of=AS_OF)
    return MemoInputs(
        credit_file=credit_file, spread=spread, pack=pack,
        policy_report=report,
    )


def test_memo_carries_every_material_disagreement():
    pipeline, credit_file = hartline_pipeline()
    spread, by_line = hartline_disagreements(pipeline, credit_file)
    report = pipeline.evaluate_policy(
        credit_file.credit_file_id, pack_version="v1", as_of=AS_OF
    )
    memo = pipeline.compose_memo(
        credit_file.credit_file_id, report_id=report.report_id
    )
    keys = [s.key for s in memo.sections]
    assert keys.index("document_disagreements") == (
        keys.index("financial_analysis") + 1
    )
    section = memo.section("document_disagreements")
    material_ids = sorted(
        d.disagreement_id for d in spread.disagreements if d.material
    )
    assert sorted(section.disagreement_ids) == material_ids
    assert sorted(memo.verification.disagreements_expected) == material_ids
    assert sorted(memo.verification.disagreements_listed) == material_ids

    from commercial_credit.memo import render_memo
    text = render_memo(memo)
    # The tax return's figures are stated, not smoothed over.
    assert "$11,905,000.00" in text
    assert "$469,000.00" in text
    # The immaterial two are counted, in the open.
    assert "The remaining 2 disagreement(s) fall below" in text
    # Flagged figures trace AS flagged, never as clean figures.
    traces = [
        t for t in memo.verification.figures
        if t.section_key == "financial_analysis"
        and "document disagreement" in t.source
    ]
    assert traces, "expected flagged-figure traces in financial analysis"


def test_memo_refused_when_the_section_is_dropped():
    inputs = _fixture_memo_inputs()
    memo = compose_memo(
        inputs.credit_file, inputs.spread, inputs.pack,
        inputs.policy_report,
    )
    tampered = memo.model_copy(update={
        "sections": [
            s for s in memo.sections if s.key != "document_disagreements"
        ]
    })
    with pytest.raises(MemoVerificationError) as excinfo:
        verify_memo(tampered, inputs)
    assert "disagreements section missing" in str(excinfo.value)


def test_memo_refused_when_a_material_disagreement_is_omitted():
    inputs = _fixture_memo_inputs()
    memo = compose_memo(
        inputs.credit_file, inputs.spread, inputs.pack,
        inputs.policy_report,
    )
    section = memo.section("document_disagreements")
    dropped = next(
        d for d in inputs.spread.disagreements
        if d.material and d.line_key == "ebit"
    )
    canonical = render_disagreement_paragraph(dropped)
    edited = section.model_copy(update={
        "paragraphs": [p for p in section.paragraphs if p != canonical],
        "disagreement_ids": [
            i for i in section.disagreement_ids
            if i != dropped.disagreement_id
        ],
    })
    tampered = memo.model_copy(update={
        "sections": [
            edited if s.key == "document_disagreements" else s
            for s in memo.sections
        ]
    })
    with pytest.raises(MemoVerificationError) as excinfo:
        verify_memo(tampered, inputs)
    assert dropped.disagreement_id in str(excinfo.value)


def test_memo_refused_when_a_material_disagreement_is_reworded():
    inputs = _fixture_memo_inputs()
    memo = compose_memo(
        inputs.credit_file, inputs.spread, inputs.pack,
        inputs.policy_report,
    )
    section = memo.section("document_disagreements")
    target = next(
        d for d in inputs.spread.disagreements
        if d.material and d.line_key == "revenue"
    )
    canonical = render_disagreement_paragraph(target)
    softened = (
        "Revenue (fiscal 2024): the audited statements report "
        "$12,374,000.00, which the spread carries; the tax return "
        "reports a broadly similar figure and the difference is "
        "not significant."
    )
    edited = section.model_copy(update={
        "paragraphs": [
            softened if p == canonical else p for p in section.paragraphs
        ],
    })
    tampered = memo.model_copy(update={
        "sections": [
            edited if s.key == "document_disagreements" else s
            for s in memo.sections
        ]
    })
    with pytest.raises(MemoVerificationError) as excinfo:
        verify_memo(tampered, inputs)
    assert "not stated in their recorded terms" in str(excinfo.value)


def test_memo_refused_when_an_immaterial_one_is_presented_as_material():
    inputs = _fixture_memo_inputs()
    memo = compose_memo(
        inputs.credit_file, inputs.spread, inputs.pack,
        inputs.policy_report,
    )
    section = memo.section("document_disagreements")
    immaterial = next(
        d for d in inputs.spread.disagreements if not d.material
    )
    edited = section.model_copy(update={
        "disagreement_ids": [
            *section.disagreement_ids, immaterial.disagreement_id
        ],
    })
    tampered = memo.model_copy(update={
        "sections": [
            edited if s.key == "document_disagreements" else s
            for s in memo.sections
        ]
    })
    with pytest.raises(MemoVerificationError) as excinfo:
        verify_memo(tampered, inputs)
    assert "does not record as material" in str(excinfo.value)


def test_immaterial_only_disagreements_still_get_a_section():
    credit_file, values = two_source_file()
    audited_and_mgmt = [
        v for v in values if v.source_type != SOURCE_TAX
    ]
    spread = build_spread(credit_file, audited_and_mgmt, [])
    assert spread.disagreements
    assert all(not d.material for d in spread.disagreements)
    pack = compute_entity_pack(
        credit_file_id=credit_file.credit_file_id,
        entity="Fixture Borrower, LLC",
        spread=spread,
        extractions=audited_and_mgmt,
    )
    report = evaluate_policy(credit_file, pack, policy_version="v1", as_of=AS_OF)
    memo = compose_memo(credit_file, spread, pack, report)
    section = memo.section("document_disagreements")
    assert section is not None
    assert section.disagreement_ids == []
    assert memo.verification.disagreements_expected == []
    assert any(
        "fall below the disagreement policy's materiality line"
        in p for p in section.paragraphs
    )


def test_no_disagreements_no_section():
    pipeline, credit_file = hartline_pipeline(with_disagreement_docs=False)
    spread = pipeline.get_spread(credit_file.credit_file_id)
    assert spread.disagreements == []
    report = pipeline.evaluate_policy(
        credit_file.credit_file_id, pack_version="v1", as_of=AS_OF
    )
    memo = pipeline.compose_memo(
        credit_file.credit_file_id, report_id=report.report_id
    )
    assert memo.section("document_disagreements") is None


# ---------------------------------------------------------------------
# The officer gate: the section is locked there too
# ---------------------------------------------------------------------


def test_officer_cannot_edit_the_disagreements_section():
    pipeline, credit_file = hartline_pipeline()
    report = pipeline.evaluate_policy(
        credit_file.credit_file_id, pack_version="v1", as_of=AS_OF
    )
    memo = pipeline.compose_memo(
        credit_file.credit_file_id, report_id=report.report_id
    )
    pipeline.submit_memo_for_review(
        credit_file.credit_file_id, memo.memo_id,
        actor="pat.preparer", actor_role="relationship_manager",
    )
    with pytest.raises(ValidationError) as excinfo:
        pipeline.approve_memo(
            credit_file.credit_file_id, memo.memo_id,
            officer_id="officer.okafor", officer_role="credit_officer",
            edits={"document_disagreements": ["The sources agree."]},
        )
    assert "not editable" in str(excinfo.value)
    # Unedited, the same memo approves clean and keeps its section.
    final = pipeline.approve_memo(
        credit_file.credit_file_id, memo.memo_id,
        officer_id="officer.okafor", officer_role="credit_officer",
    )
    assert final.section("document_disagreements") is not None
    assert final.verification.disagreements_expected == (
        memo.verification.disagreements_expected
    )


# ---------------------------------------------------------------------
# API: list, detail, filters, and the spread's own flags
# ---------------------------------------------------------------------


@pytest.fixture()
def client():
    pipeline = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app)


def _upload(client, fid, kind, filename, data, period_label="FY2024"):
    resp = client.post(
        f"/applications/{fid}/documents",
        json={
            "kind": kind,
            "filename": filename,
            "content_b64": base64.b64encode(data).decode("ascii"),
            "period_label": period_label,
        },
    )
    assert resp.status_code == 201
    return resp.json()


def test_api_disagreements_list_detail_and_spread_flags(client):
    opened = client.post(
        "/applications/from-los", json={"application_ref": "LOS-2026-0117"}
    )
    assert opened.status_code == 201
    fid = opened.json()["credit_file_id"]
    for year in (2022, 2023, 2024):
        _upload(
            client, fid, "financial_statement",
            f"hartline-fy{year}-statements.json", statement_document(year),
            period_label=f"FY{year}",
        )
    _upload(client, fid, "tax_return", "hartline-fy2024-tax-return.json",
            tax_return_document(2024))
    _upload(client, fid, "financial_statement",
            "hartline-fy2024-management-statements.json",
            management_statement_document(2024))
    assert client.post(f"/applications/{fid}/extract").status_code == 200

    listed = client.get(f"/applications/{fid}/disagreements")
    assert listed.status_code == 200
    records = listed.json()
    assert len(records) == 7
    assert sum(1 for r in records if r["material"]) == 5

    material_only = client.get(
        f"/applications/{fid}/disagreements", params={"material": "true"}
    )
    assert len(material_only.json()) == 5
    immaterial_only = client.get(
        f"/applications/{fid}/disagreements", params={"material": "false"}
    )
    assert {r["line_key"] for r in immaterial_only.json()} == {
        "inventory", "total_current_assets",
    }

    revenue = next(r for r in records if r["line_key"] == "revenue")
    detail = client.get(
        f"/applications/{fid}/disagreements/{revenue['disagreement_id']}"
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["working_value"] == "12374000"
    sources = {s["source_type"]: s for s in body["sources"]}
    assert sources["tax_return"]["value"] == "11905000"
    assert sources["tax_return"]["delta_abs"] == "469000"
    assert client.get(
        f"/applications/{fid}/disagreements/dis-nope"
    ).status_code == 404

    # The spread view carries the records; the pack carries flags.
    spread = client.get(f"/applications/{fid}/spread").json()
    assert len(spread["disagreements"]) == 7
    assert spread["disagreement_policy_version"] == "v1"
    assert {
        ps["source_type"] for ps in spread["period_sources"]
        if ps["fiscal_year"] == 2024
    } == {"audited_statement", "tax_return", "management_statement"}

    computed = client.post(f"/applications/{fid}/computations", json={})
    assert computed.status_code == 201
    figures = {
        (f["key"], f["fiscal_year"]): f
        for f in computed.json()["figures"]
    }
    assert figures[("gross_margin", 2024)]["disagreement_ids"]
    assert figures[("current_ratio", 2024)]["disagreement_ids"] == []
