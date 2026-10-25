"""Round 4: the policy pack, graded against hand-decided outcomes.

Every expected outcome below was decided by hand from the fixture
figures (the arithmetic is shown in comments) — never by running
the evaluator and writing down what it said. The fixtures:

  Coverage borrower — EBITDA 390,000, debt service 300,000, so
    DSCR is exactly 1.3000: a pass under v1 (floor 1.25) and a fail
    under v2 (floor 1.35). FCCR (390,000 − 50,000 capex) / 300,000
    = 1.1333. Leverage 800,000 / 1,000,000 = 0.8000.
  Leverage borrower — liabilities 5,000,000 over tangible net
    worth 1,000,000 = 5.0000, over every segment ceiling.
  Hospitality borrower — leverage 1.9000: passes v1's 2.00 ceiling,
    fails v2's tightened 1.75.
  Hartline (the seed) — no cash-flow page, so EBITDA and DSCR are
    not computable and the DSCR test must say not_evaluable, naming
    depreciation & amortisation. Its leverage is hand-checked:
    4,509,000 / 2,940,000 = 1.5337 (FY2024).
"""

import base64
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.checklist import generate_checklist
from commercial_credit.computations import compute_entity_pack, compute_global_pack
from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.db import migration_files
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.errors import CreditError, NotFoundError
from commercial_credit.models import (
    Borrower,
    BorrowerSegment,
    CreditFile,
    DocumentKind,
    FacilityRequest,
    FacilityType,
    Guarantor,
    PeriodSource,
    PolicyTest,
    Provenance,
    ExtractedValue,
    SourceDocument,
    Spread,
    SpreadLine,
)
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.policy import (
    CURRENT_POLICY_VERSION,
    evaluate_policy,
    get_policy_pack,
    list_policy_packs,
)
from commercial_credit.seed import ReferenceLosFeed, statement_document
from commercial_credit.spreading import TEMPLATE_LINES
from commercial_credit.store import InMemoryCreditStore, SqliteCreditStore

D = Decimal

AS_OF = date(2025, 6, 30)

SECTION_PAGE = {
    "balance_sheet": 1,
    "income_statement": 2,
    "cash_flow_statement": 3,
}


def build_fixture_spread(credit_file_id, doc_prefix, figures):
    """A Spread + extractions from raw figures, as in rounds 2–3."""
    periods = sorted(figures)
    lines: list[SpreadLine] = []
    extractions: list[ExtractedValue] = []
    counters: dict[str, int] = {}
    for template in TEMPLATE_LINES:
        counters[template.section] = counters.get(template.section, 0) + 1
        raw: dict[int, Decimal | None] = {}
        for year in periods:
            value = figures[year].get(template.field)
            raw[year] = D(value) if value is not None else None
            if value is not None:
                extractions.append(ExtractedValue(
                    field=template.field,
                    value=D(value),
                    period_label=f"FY{year}",
                    fiscal_year=year,
                    provenance=Provenance(
                        document_id=f"{doc_prefix}-{year}",
                        page=SECTION_PAGE[template.section],
                        line=f"L{counters[template.section]}",
                    ),
                ))
        lines.append(SpreadLine(
            key=template.key, label=template.label, section=template.section,
            values=raw, adjusted_values=dict(raw),
            changes={year: None for year in periods},
        ))
    spread = Spread(
        spread_id=f"spread-policy-{doc_prefix}",
        credit_file_id=credit_file_id,
        template_version="aldergrove-spread-v1",
        periods=periods,
        period_sources=[
            PeriodSource(
                fiscal_year=year, period_label=f"FY{year}",
                document_id=f"{doc_prefix}-{year}",
                document_sha256="cd" * 32, version=1,
            )
            for year in periods
        ],
        lines=lines,
    )
    return spread, extractions


def make_file(
    *,
    segment=BorrowerSegment.MANUFACTURING,
    facility_type=FacilityType.TERM_LOAN,
    amount="2000000",
    guarantors=(),
    checklist_satisfied=True,
    documents=(),
    credit_file_id="cf-policy-fixture",
) -> CreditFile:
    borrower = Borrower(
        legal_name="Fixture Borrower, LLC",
        ein_last4="1234",
        segment=segment,
        state_of_formation="OH",
        years_in_business=9,
        primary_contact_name="Pat Fixture",
        primary_contact_email="pat@fixture.example",
    )
    facility = FacilityRequest(
        facility_type=facility_type,
        amount=D(amount),
        purpose="Working capital and equipment",
        term_months=60,
        guarantors=[
            Guarantor(name=name, ownership_pct=D("100")) for name in guarantors
        ],
    )
    credit_file = CreditFile(
        credit_file_id=credit_file_id,
        borrower=borrower,
        facility=facility,
        documents=list(documents),
    )
    credit_file.checklist = generate_checklist(facility)
    if checklist_satisfied:
        for item in credit_file.checklist:
            item.satisfied_by_document_id = "doc-fixture"
    return credit_file


COVERAGE_FIGURES = {
    2023: {"current_portion_ltd": "200000"},
    2024: {
        "ebit": "270000",
        "depreciation_and_amortization": "120000",
        "interest_expense": "100000",
        "current_portion_ltd": "220000",
        "capital_expenditures": "50000",
        "total_liabilities": "800000",
        "total_equity": "1000000",
    },
}


def coverage_pack(credit_file_id="cf-policy-fixture"):
    spread, extractions = build_fixture_spread(
        credit_file_id, "covdoc", COVERAGE_FIGURES
    )
    return compute_entity_pack(
        credit_file_id=credit_file_id,
        entity="Fixture Borrower, LLC",
        spread=spread,
        extractions=extractions,
    )


def guarantor_pack(credit_file_id, figures, doc_prefix):
    spread, extractions = build_fixture_spread(credit_file_id, doc_prefix, figures)
    return compute_entity_pack(
        credit_file_id=credit_file_id,
        entity="Eval Guarantor",
        spread=spread,
        extractions=extractions,
    )


# Guarantor with EBITDA 20,000 (10,000 + 10,000) and debt service
# 500,000 (prior CPLTD 400,000 + interest 100,000).
WEAK_GUARANTOR = {
    2023: {"current_portion_ltd": "400000"},
    2024: {
        "ebit": "10000",
        "depreciation_and_amortization": "10000",
        "interest_expense": "100000",
    },
}

# Guarantor with EBITDA 170,000 (150,000 + 20,000) and debt service
# 70,000 (prior CPLTD 40,000 + interest 30,000).
STRONG_GUARANTOR = {
    2023: {"current_portion_ltd": "40000"},
    2024: {
        "ebit": "150000",
        "depreciation_and_amortization": "20000",
        "interest_expense": "30000",
    },
}


# ---------------------------------------------------------------------
# The packs themselves
# ---------------------------------------------------------------------


def test_two_pack_versions_ship_with_a_tightening():
    packs = list_policy_packs()
    assert [p.version for p in packs] == ["v1", "v2"]
    v1, v2 = packs
    assert v1.pack_id == v2.pack_id == "aldergrove-policy"
    assert v1.effective_to == date(2026, 3, 31)
    assert v2.effective_from == date(2026, 4, 1)
    assert v2.effective_to is None
    assert CURRENT_POLICY_VERSION == "v2"

    def threshold(pack, test_id):
        test = next(t for t in pack.tests if t.test_id == test_id)
        return test.threshold if test.threshold is not None else test.limit

    # The v2 tightening, stated in its issuing note.
    assert threshold(v2, "min_dscr") > threshold(v1, "min_dscr")
    assert threshold(v2, "financial_statement_age") < threshold(
        v1, "financial_statement_age"
    )
    lev1 = next(
        t for t in v1.tests if t.test_id == "max_leverage_by_segment"
    )
    lev2 = next(
        t for t in v2.tests if t.test_id == "max_leverage_by_segment"
    )
    assert (
        lev2.segment_thresholds["hospitality"]
        < lev1.segment_thresholds["hospitality"]
    )
    # Everything else is carried across unchanged.
    for test_id in (
        "min_global_dscr",
        "min_fixed_charge_coverage",
        "max_single_borrower_concentration",
        "appraisal_age",
    ):
        assert threshold(v1, test_id) == threshold(v2, test_id)
    assert v1.capital_base == v2.capital_base


def test_every_test_is_well_formed_and_names_real_figures():
    pack = coverage_pack()
    engine_keys = {f.key for f in pack.figures} | {"global_dscr"}
    for version in ("v1", "v2"):
        p = get_policy_pack(version)
        ids = [t.test_id for t in p.tests]
        assert len(ids) == len(set(ids)) == 8
        for test in p.tests:
            assert test.citation.strip()
            assert test.severity in ("blocker", "warning")
            if test.kind == "figure":
                assert test.figure_key in engine_keys, test.test_id
            if test.segment_thresholds is not None:
                assert set(test.segment_thresholds) == {
                    s.value for s in BorrowerSegment
                }


def test_unknown_pack_version_is_a_named_refusal():
    with pytest.raises(NotFoundError):
        get_policy_pack("v9")


def test_policy_test_validation_refuses_unnamed_inputs():
    with pytest.raises(CreditError):
        PolicyTest(
            test_id="bad", name="Bad", kind="figure",
            severity="blocker", citation="policy text",
        )
    with pytest.raises(CreditError):
        PolicyTest(
            test_id="bad", name="Bad", kind="file_fact",
            severity="blocker", citation="policy text",
        )


# ---------------------------------------------------------------------
# Figure tests: pass, fail, and the honest third state
# ---------------------------------------------------------------------


def test_coverage_borrower_passes_v1_with_hand_decided_outcomes():
    credit_file = make_file()
    report = evaluate_policy(
        credit_file, coverage_pack(), policy_version="v1", as_of=AS_OF
    )
    expected = {
        "max_leverage_by_segment": "pass",
        "min_dscr": "pass",
        "min_global_dscr": "not_evaluable",
        "min_fixed_charge_coverage": "pass",
        "max_single_borrower_concentration": "pass",
        "required_documents_complete": "pass",
        "financial_statement_age": "pass",
        "appraisal_age": "not_evaluable",
    }
    assert {o.test_id: o.outcome for o in report.outcomes} == expected
    assert report.summary == {
        "pass": 6,
        "fail": 0,
        "not_evaluable": 2,
        "blocker_failures": 0,
        "blocker_not_evaluable": 1,
    }
    dscr = report.outcome("min_dscr")
    assert dscr.threshold == D("1.25")
    assert dscr.fiscal_year == 2024
    assert dscr.values_read[-1].value == "1.3000"
    leverage = report.outcome("max_leverage_by_segment")
    assert leverage.threshold == D("2.00")
    assert leverage.values_read[-1].value == "0.8000"
    # The two not-evaluable outcomes name why, exactly.
    glob = report.outcome("min_global_dscr")
    assert any("no guarantors" in m for m in glob.missing_inputs)
    appraisal = report.outcome("appraisal_age")
    assert any("term_loan" in m for m in appraisal.missing_inputs)


def test_the_same_borrower_fails_v2_on_the_tightened_dscr_floor():
    credit_file = make_file()
    report = evaluate_policy(
        credit_file, coverage_pack(), policy_version="v2", as_of=AS_OF
    )
    dscr = report.outcome("min_dscr")
    assert dscr.outcome == "fail"
    assert dscr.threshold == D("1.35")
    assert dscr.severity == "blocker"
    assert report.summary["blocker_failures"] == 1
    # Nothing else flips: the tightening is surgical.
    assert report.outcome("max_leverage_by_segment").outcome == "pass"
    assert report.outcome("financial_statement_age").outcome == "pass"


def test_leverage_failure_is_a_blocker_with_the_value_on_record():
    spread, extractions = build_fixture_spread(
        "cf-policy-fixture", "levdoc",
        {2024: {"total_liabilities": "5000000", "total_equity": "1000000"}},
    )
    pack = compute_entity_pack(
        credit_file_id="cf-policy-fixture", entity="Fixture Borrower, LLC",
        spread=spread, extractions=extractions,
    )
    report = evaluate_policy(
        make_file(), pack, policy_version="v1", as_of=AS_OF
    )
    leverage = report.outcome("max_leverage_by_segment")
    assert leverage.outcome == "fail"
    assert leverage.severity == "blocker"
    assert leverage.threshold == D("2.00")
    assert leverage.values_read[-1].value == "5.0000"
    assert leverage.values_read[0].value == "manufacturing"


def test_hospitality_leverage_flips_with_the_v2_ceiling():
    figures = {
        2024: {"total_liabilities": "1900000", "total_equity": "1000000"}
    }
    spread, extractions = build_fixture_spread(
        "cf-policy-fixture", "hospdoc", figures
    )
    pack = compute_entity_pack(
        credit_file_id="cf-policy-fixture", entity="Fixture Borrower, LLC",
        spread=spread, extractions=extractions,
    )
    credit_file = make_file(segment=BorrowerSegment.HOSPITALITY)
    v1 = evaluate_policy(credit_file, pack, policy_version="v1", as_of=AS_OF)
    v2 = evaluate_policy(credit_file, pack, policy_version="v2", as_of=AS_OF)
    assert v1.outcome("max_leverage_by_segment").outcome == "pass"
    outcome = v2.outcome("max_leverage_by_segment")
    assert outcome.outcome == "fail"
    assert outcome.threshold == D("1.75")


def test_a_figure_absent_from_the_pack_is_not_evaluable_never_a_pass():
    pack = coverage_pack()
    stripped = pack.model_copy(update={
        "figures": [f for f in pack.figures if f.key != "dscr"],
    })
    report = evaluate_policy(
        make_file(), stripped, policy_version="v1", as_of=AS_OF
    )
    dscr = report.outcome("min_dscr")
    assert dscr.outcome == "not_evaluable"
    assert any("not present" in m for m in dscr.missing_inputs)


# ---------------------------------------------------------------------
# Global DSCR: applicability is a file fact
# ---------------------------------------------------------------------


def _global_report(guarantor_figures, doc_prefix):
    credit_file = make_file(guarantors=("Eval Guarantor",))
    borrower_pack = coverage_pack()
    g_pack = guarantor_pack(
        credit_file.credit_file_id, guarantor_figures, doc_prefix
    )
    global_pack = compute_global_pack(
        credit_file_id=credit_file.credit_file_id,
        borrower_pack=borrower_pack,
        entity_packs={"Eval Guarantor": g_pack},
        expected_guarantors=["Eval Guarantor"],
    )
    return evaluate_policy(
        credit_file, borrower_pack, policy_version="v1",
        global_pack=global_pack, as_of=AS_OF,
    )


def test_global_dscr_passes_when_the_combined_view_covers():
    # Combined cash flow 390,000 + 170,000 = 560,000; combined debt
    # service 300,000 + 70,000 = 370,000; 560,000 / 370,000 = 1.5135.
    report = _global_report(STRONG_GUARANTOR, "gstrong")
    outcome = report.outcome("min_global_dscr")
    assert outcome.outcome == "pass"
    assert outcome.values_read[-1].value == "1.5135"
    assert report.global_pack_id is not None


def test_global_dscr_fails_when_the_guarantor_drags_it_under():
    # Combined cash flow 390,000 + 20,000 = 410,000; combined debt
    # service 300,000 + 500,000 = 800,000; 410,000 / 800,000 = 0.5125.
    report = _global_report(WEAK_GUARANTOR, "gweak")
    outcome = report.outcome("min_global_dscr")
    assert outcome.outcome == "fail"
    assert outcome.values_read[-1].value == "0.5125"


def test_guarantors_named_but_no_global_pack_is_not_evaluable():
    credit_file = make_file(guarantors=("Eval Guarantor",))
    report = evaluate_policy(
        credit_file, coverage_pack(), policy_version="v1", as_of=AS_OF
    )
    outcome = report.outcome("min_global_dscr")
    assert outcome.outcome == "not_evaluable"
    assert any("global computation pack" in m for m in outcome.missing_inputs)


# ---------------------------------------------------------------------
# File-fact tests
# ---------------------------------------------------------------------


def test_missing_documents_fail_and_are_named():
    credit_file = make_file(checklist_satisfied=False)
    report = evaluate_policy(
        credit_file, coverage_pack(), policy_version="v1", as_of=AS_OF
    )
    outcome = report.outcome("required_documents_complete")
    assert outcome.outcome == "fail"
    assert outcome.severity == "blocker"
    missing = next(
        v for v in outcome.values_read
        if v.input == "fact:missing_required_items"
    )
    assert "chk-01" in missing.value
    assert "chk-01" in outcome.detail


def test_no_checklist_yet_is_not_evaluable():
    credit_file = make_file(checklist_satisfied=False)
    credit_file.checklist = []
    report = evaluate_policy(
        credit_file, coverage_pack(), policy_version="v1", as_of=AS_OF
    )
    outcome = report.outcome("required_documents_complete")
    assert outcome.outcome == "not_evaluable"
    assert any("checklist" in m for m in outcome.missing_inputs)


def test_stale_statements_fail_the_age_limit():
    # FY2024 year-end to 2027-06-30 is 29 complete calendar months.
    report = evaluate_policy(
        make_file(), coverage_pack(), policy_version="v1",
        as_of=date(2027, 6, 30),
    )
    outcome = report.outcome("financial_statement_age")
    assert outcome.outcome == "fail"
    assert outcome.threshold == D("18")
    age = next(
        v for v in outcome.values_read if v.input == "fact:age_months"
    )
    assert age.value == "29"


def test_concentration_over_the_capital_share_fails():
    credit_file = make_file(amount="30000000")
    report = evaluate_policy(
        credit_file, coverage_pack(), policy_version="v1", as_of=AS_OF
    )
    outcome = report.outcome("max_single_borrower_concentration")
    assert outcome.outcome == "fail"
    share = next(
        v for v in outcome.values_read
        if v.input == "fact:concentration_share"
    )
    assert share.value == "0.1200"


def _appraisal_doc(received: datetime) -> SourceDocument:
    return SourceDocument(
        document_id="doc-appraisal-1",
        credit_file_id="cf-policy-fixture",
        kind=DocumentKind.APPRAISAL,
        filename="appraisal.json",
        sha256="ab" * 32,
        byte_size=128,
        storage_key="docs/doc-appraisal-1",
        received_at=received,
    )


def test_appraisal_age_for_cre_files():
    fresh = make_file(
        facility_type=FacilityType.CRE_LOAN,
        documents=[_appraisal_doc(datetime(2025, 1, 15, tzinfo=timezone.utc))],
    )
    report = evaluate_policy(
        fresh, coverage_pack(), policy_version="v1", as_of=AS_OF
    )
    assert report.outcome("appraisal_age").outcome == "pass"

    stale = make_file(
        facility_type=FacilityType.CRE_LOAN,
        documents=[_appraisal_doc(datetime(2024, 1, 15, tzinfo=timezone.utc))],
    )
    report = evaluate_policy(
        stale, coverage_pack(), policy_version="v1", as_of=AS_OF
    )
    outcome = report.outcome("appraisal_age")
    assert outcome.outcome == "fail"
    assert outcome.severity == "warning"

    absent = make_file(facility_type=FacilityType.CRE_LOAN)
    report = evaluate_policy(
        absent, coverage_pack(), policy_version="v1", as_of=AS_OF
    )
    outcome = report.outcome("appraisal_age")
    assert outcome.outcome == "not_evaluable"
    assert any("appraisal document" in m for m in outcome.missing_inputs)


# ---------------------------------------------------------------------
# The seed borrower, through the real pipeline
# ---------------------------------------------------------------------


def _hartline_pipeline() -> CreditPipeline:
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
    pipeline.open_application(borrower, facility, documents=docs)
    return pipeline


def test_hartline_dscr_is_not_evaluable_and_names_the_gap():
    pipeline = _hartline_pipeline()
    credit_file = pipeline.store.list_credit_files()[0]
    report = pipeline.evaluate_policy(
        credit_file.credit_file_id, pack_version="v1", as_of=AS_OF
    )
    dscr = report.outcome("min_dscr")
    assert dscr.outcome == "not_evaluable"
    assert any(
        "depreciation_and_amortization" in m for m in dscr.missing_inputs
    )
    assert report.outcome("min_fixed_charge_coverage").outcome == (
        "not_evaluable"
    )
    leverage = report.outcome("max_leverage_by_segment")
    assert leverage.outcome == "pass"
    assert leverage.values_read[-1].value == "1.5337"
    assert leverage.fiscal_year == 2024
    assert report.outcome("required_documents_complete").outcome == "fail"
    assert report.outcome("financial_statement_age").outcome == "pass"
    assert report.outcome(
        "max_single_borrower_concentration"
    ).outcome == "pass"
    glob = report.outcome("min_global_dscr")
    assert glob.outcome == "not_evaluable"
    assert any("global computation pack" in m for m in glob.missing_inputs)


def test_policy_history_records_each_pinned_version_once():
    pipeline = _hartline_pipeline()
    credit_file = pipeline.store.list_credit_files()[0]
    fid = credit_file.credit_file_id
    # The intake pipeline already evaluated under the current (v2)
    # version; pinning v1 appends its own entry.
    v1 = pipeline.evaluate_policy(fid, pack_version="v1", as_of=AS_OF)
    again = pipeline.evaluate_policy(fid, pack_version="v1", as_of=AS_OF)
    assert again.report_id == v1.report_id
    assert again.model_dump_json() == v1.model_dump_json()
    history = pipeline.store.get_credit_file(fid).policy_history
    versions = [h.policy_version for h in history]
    assert versions.count("v1") == 1
    assert "v2" in versions
    entry = next(h for h in history if h.policy_version == "v1")
    assert entry.report_id == v1.report_id
    assert entry.as_of == AS_OF


def test_v1_report_survives_the_v2_evaluation_untouched():
    store = InMemoryCreditStore()
    credit_file = make_file()
    pack = coverage_pack()
    v1 = evaluate_policy(credit_file, pack, policy_version="v1", as_of=AS_OF)
    store.save_policy_report(v1)
    v2 = evaluate_policy(credit_file, pack, policy_version="v2", as_of=AS_OF)
    store.save_policy_report(v2)
    assert v1.report_id != v2.report_id
    kept = store.get_policy_report(v1.report_id)
    assert kept is not None
    assert kept.model_dump_json() == v1.model_dump_json()
    assert kept.outcome("min_dscr").outcome == "pass"
    assert v2.outcome("min_dscr").outcome == "fail"
    assert len(store.list_policy_reports(credit_file.credit_file_id)) == 2


def test_evaluation_is_deterministic_in_id_and_bytes():
    credit_file = make_file()
    pack = coverage_pack()
    first = evaluate_policy(credit_file, pack, policy_version="v1", as_of=AS_OF)
    second = evaluate_policy(
        credit_file, pack, policy_version="v1", as_of=AS_OF
    )
    assert first.report_id == second.report_id
    assert first.model_dump_json() == second.model_dump_json()
    # A moved input — a newly satisfied document — moves the id.
    changed = make_file(checklist_satisfied=False)
    moved = evaluate_policy(changed, pack, policy_version="v1", as_of=AS_OF)
    assert moved.report_id != first.report_id


# ---------------------------------------------------------------------
# Stores and migrations
# ---------------------------------------------------------------------


def test_sqlite_store_round_trips_policy_reports(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    report = evaluate_policy(
        make_file(), coverage_pack(), policy_version="v1", as_of=AS_OF
    )
    store.save_policy_report(report)
    store.save_policy_report(report)  # content-addressed: recorded once
    assert store.get_policy_report(report.report_id) == report
    assert len(store.list_policy_reports(report.credit_file_id)) == 1
    v2 = evaluate_policy(
        make_file(), coverage_pack(), policy_version="v2", as_of=AS_OF
    )
    store.save_policy_report(v2)
    assert len(store.list_policy_reports(report.credit_file_id)) == 2
    reopened = SqliteCreditStore(tmp_path / "credit.db")
    assert reopened.get_policy_report(report.report_id) == report


def test_migration_0004_creates_the_policy_reports_table():
    files = migration_files(MIGRATIONS_DIR)
    names = [f.name for f in files]
    assert any(n.startswith("0004_") for n in names)
    sql = next(
        f for f in files if f.name.startswith("0004_")
    ).read_text()
    assert "policy_reports" in sql


# ---------------------------------------------------------------------
# API
# ---------------------------------------------------------------------


@pytest.fixture()
def client():
    pipeline = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app)


def test_api_lists_packs_and_refuses_unknown_versions(client):
    packs = client.get("/policy-packs")
    assert packs.status_code == 200
    assert [p["version"] for p in packs.json()] == ["v1", "v2"]
    one = client.get("/policy-packs/v1")
    assert one.status_code == 200
    assert one.json()["capital_base"] == "250000000"
    assert client.get("/policy-packs/v9").status_code == 404


def test_api_evaluates_under_a_pinned_version_and_serves_reports(client):
    opened = client.post(
        "/applications/from-los", json={"application_ref": "LOS-2026-0117"}
    )
    assert opened.status_code == 201
    fid = opened.json()["credit_file_id"]
    for year in (2022, 2023, 2024):
        uploaded = client.post(
            f"/applications/{fid}/documents",
            json={
                "kind": "financial_statement",
                "filename": f"hartline-fy{year}-statements.json",
                "content_b64": base64.b64encode(
                    statement_document(year)
                ).decode("ascii"),
                "period_label": f"FY{year}",
            },
        )
        assert uploaded.status_code == 201
    assert client.post(f"/applications/{fid}/extract").status_code == 200
    resp = client.post(
        f"/applications/{fid}/policy-evaluations",
        json={"pack_version": "v1", "as_of": "2025-06-30"},
    )
    assert resp.status_code == 201
    report = resp.json()
    assert report["policy_version"] == "v1"
    outcomes = {o["test_id"]: o["outcome"] for o in report["outcomes"]}
    assert outcomes["min_dscr"] == "not_evaluable"
    assert outcomes["max_leverage_by_segment"] == "pass"

    listed = client.get(f"/applications/{fid}/policy-reports")
    assert listed.status_code == 200
    assert report["report_id"] in [r["report_id"] for r in listed.json()]
    fetched = client.get(
        f"/applications/{fid}/policy-reports/{report['report_id']}"
    )
    assert fetched.status_code == 200
    assert fetched.json()["report_id"] == report["report_id"]
    assert client.get(
        f"/applications/{fid}/policy-reports/policy-nope"
    ).status_code == 404

    detail = client.get(f"/applications/{fid}")
    history = detail.json()["policy_history"]
    assert {h["policy_version"] for h in history} >= {"v1", "v2"}
    bad = client.post(
        f"/applications/{fid}/policy-evaluations",
        json={"pack_version": "v9"},
    )
    assert bad.status_code == 404
