"""Round 5: memo drafting and the figure verifier, graded hard.

The centrepiece is law 1 enforced at the memo: a draft is stored
only when EVERY figure in its rendered text traces to the pinned
artefacts. The tamper cases are the point of the round — a figure
altered by one cent, a ratio transposed, a figure invented out of
thin air, and a policy exception quietly dropped must each be
refused, with the offender named. Hand-decided anchors:

  Hartline FY2024 — current ratio 2.2590, working capital
    2,202,000, total debt 3,120,000 (hand-checked in round 3).
    EBITDA / DSCR / FCCR are not computable (no cash-flow page),
    so the v1 report carries five non-pass outcomes: min_dscr,
    min_global_dscr, min_fixed_charge_coverage (all
    not_evaluable), required_documents_complete (fail — 7 of 9
    items outstanding after three statements are filed), and
    appraisal_age (not_evaluable; a term loan carries no
    appraisal requirement). All five must appear in the memo's
    exceptions, by test id.
  Coverage borrower — DSCR exactly 1.3000, FCCR 1.1333, leverage
    0.8000 (round 4 hand arithmetic), passing v1's floors.
  Leverage borrower — 5,000,000 / 1,000,000 = 5.0000 against a
    2.00 blocker ceiling: the memo must carry the FAIL, figures
    and all, never soften it.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit import memo as memo_module
from commercial_credit.checklist import generate_checklist
from commercial_credit.computations import compute_entity_pack, compute_global_pack
from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.db import migration_files
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.errors import (
    MemoVerificationError,
    NotFoundError,
    ValidationError,
)
from commercial_credit.memo import (
    MemoInputs,
    build_trace_universe,
    compose_memo,
    extract_figures,
    get_composer,
    render_memo,
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
    Guarantor,
    PeriodSource,
    Provenance,
    SourceDocument,
    Spread,
    SpreadLine,
)
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.policy import evaluate_policy
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
    """A Spread + extractions from raw figures, as in rounds 2–4."""
    periods = sorted(figures)
    lines: list[SpreadLine] = []
    extractions: list[ExtractedValue] = []
    counters: dict[str, int] = {}
    for template in TEMPLATE_LINES:
        counters[template.section] = counters.get(template.section, 0) + 1
        raw: dict[int, Decimal | None] = {}
        for year in periods:
            value = figures[year].get(template.field)
            raw[year] = Decimal(value) if value is not None else None
            if value is not None:
                extractions.append(ExtractedValue(
                    field=template.field,
                    value=Decimal(value),
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
        spread_id=f"spread-test-{doc_prefix}",
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
    credit_file_id="cf-memo-fixture",
) -> CreditFile:
    facility = FacilityRequest(
        facility_type=facility_type,
        amount=D(amount),
        purpose="Working capital and equipment",
        term_months=60,
        collateral_description="All business assets",
        guarantors=[
            Guarantor(name=name, ownership_pct=D("100")) for name in guarantors
        ],
    )
    credit_file = CreditFile(
        credit_file_id=credit_file_id,
        borrower=Borrower(
            legal_name="Fixture Borrower, LLC",
            ein_last4="1234",
            segment=segment,
            state_of_formation="OH",
            years_in_business=9,
            primary_contact_name="Pat Fixture",
            primary_contact_email="pat@fixture.example",
        ),
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

STRONG_GUARANTOR = {
    2023: {"current_portion_ltd": "40000"},
    2024: {
        "ebit": "150000",
        "depreciation_and_amortization": "20000",
        "interest_expense": "30000",
    },
}


def coverage_inputs(credit_file_id="cf-memo-fixture", **file_kwargs):
    credit_file = make_file(credit_file_id=credit_file_id, **file_kwargs)
    spread, extractions = build_fixture_spread(
        credit_file_id, "covdoc", COVERAGE_FIGURES
    )
    pack = compute_entity_pack(
        credit_file_id=credit_file_id,
        entity="Fixture Borrower, LLC",
        spread=spread,
        extractions=extractions,
    )
    report = evaluate_policy(
        credit_file, pack, policy_version="v1", as_of=AS_OF
    )
    return MemoInputs(
        credit_file=credit_file, spread=spread, pack=pack,
        policy_report=report,
    )


def hartline_pipeline() -> CreditPipeline:
    return CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )


def open_hartline(pipeline: CreditPipeline) -> CreditFile:
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
    return pipeline.open_application(borrower, facility, documents=docs)


def hartline_memo(pipeline: CreditPipeline):
    credit_file = open_hartline(pipeline)
    report = pipeline.evaluate_policy(
        credit_file.credit_file_id, pack_version="v1", as_of=AS_OF
    )
    return pipeline.compose_memo(
        credit_file.credit_file_id, report_id=report.report_id
    )


# ---------------------------------------------------------------------
# The composed memo: pins, sections, status
# ---------------------------------------------------------------------


def test_composed_memo_pins_its_artefacts_and_ends_in_draft():
    pipeline = hartline_pipeline()
    memo = hartline_memo(pipeline)
    spread = pipeline.get_spread(memo.credit_file_id)
    report = pipeline.store.get_policy_report(memo.policy_report_id)
    assert memo.status == "draft"
    assert memo.borrower_name == "Hartline Manufacturing, Inc."
    assert memo.facility_type == "term_loan"
    assert memo.spread_id == spread.spread_id
    assert memo.computation_pack_id == report.computation_pack_id
    assert memo.policy_pack_id == "aldergrove-policy"
    assert memo.policy_version == "v1"
    assert memo.as_of == AS_OF
    assert memo.composer_profile == "offline"
    assert [s.key for s in memo.sections] == [
        "request_summary",
        "borrower_overview",
        "financial_analysis",
        "strengths",
        "risks_mitigants",
        "policy_exceptions",
        "recommendation",
        "conditions",
    ]
    # Stored behind the port, listed on the file.
    assert pipeline.get_memo(memo.credit_file_id, memo.memo_id).memo_id == memo.memo_id
    assert memo.memo_id in [
        m.memo_id for m in pipeline.list_memos(memo.credit_file_id)
    ]


def test_intake_run_composes_a_draft_memo():
    pipeline = hartline_pipeline()
    credit_file = open_hartline(pipeline)
    memos = pipeline.list_memos(credit_file.credit_file_id)
    assert len(memos) == 1
    assert memos[0].status == "draft"
    assert memos[0].verification.verified is True
    actions = [e.action for e in pipeline.store.list_audit(credit_file.credit_file_id)]
    assert "memo_composed" in actions


def test_recommendation_and_conditions_are_placeholders():
    memo = hartline_memo(hartline_pipeline())
    recommendation = memo.section("recommendation")
    assert recommendation is not None
    text = " ".join(recommendation.paragraphs)
    assert "Reserved for the credit officer" in text
    assert "makes no recommendation" in text
    conditions = memo.section("conditions")
    assert conditions is not None
    assert "none are recorded" in " ".join(conditions.paragraphs)


# ---------------------------------------------------------------------
# The verifier: completeness on clean memos
# ---------------------------------------------------------------------


def test_every_rendered_figure_carries_a_trace():
    memo = hartline_memo(hartline_pipeline())
    rendered = render_memo(memo)
    extracted = extract_figures(rendered)
    record = memo.verification
    assert record.verified is True
    assert record.figure_count == len(extracted)
    assert record.figure_count > 50  # a real memo, not a stub
    assert [t.text for t in record.figures] == [f.text for f in extracted]
    assert all(t.source for t in record.figures)


def test_a_known_figure_traces_to_its_computation():
    memo = hartline_memo(hartline_pipeline())
    traces = [t for t in memo.verification.figures if t.text == "2.2590×"]
    assert traces, "FY2024 current ratio missing from the memo"
    assert "current_ratio" in traces[0].source
    assert "computation pack" in traces[0].source
    money = [t for t in memo.verification.figures if t.text == "$2,202,000.00"]
    assert money and "working_capital" in money[0].source


def test_offline_composer_figure_discipline_across_fixtures():
    """Property check: every number the composer emits exists in
    its inputs — across the seed borrower and both fixtures."""
    inputs_list = [coverage_inputs()]
    lev_spread, lev_extractions = build_fixture_spread(
        "cf-memo-fixture", "levdoc",
        {2024: {"total_liabilities": "5000000", "total_equity": "1000000"}},
    )
    lev_file = make_file()
    lev_pack = compute_entity_pack(
        credit_file_id="cf-memo-fixture", entity="Fixture Borrower, LLC",
        spread=lev_spread, extractions=lev_extractions,
    )
    inputs_list.append(MemoInputs(
        credit_file=lev_file, spread=lev_spread, pack=lev_pack,
        policy_report=evaluate_policy(
            lev_file, lev_pack, policy_version="v1", as_of=AS_OF
        ),
    ))
    pipeline = hartline_pipeline()
    credit_file = open_hartline(pipeline)
    report = pipeline.evaluate_policy(
        credit_file.credit_file_id, pack_version="v1", as_of=AS_OF
    )
    hartline_memo_inputs = MemoInputs(
        credit_file=pipeline.store.get_credit_file(credit_file.credit_file_id),
        spread=pipeline.get_spread(credit_file.credit_file_id),
        pack=pipeline.store.get_computation_pack(report.computation_pack_id),
        policy_report=report,
    )
    inputs_list.append(hartline_memo_inputs)
    for inputs in inputs_list:
        memo = compose_memo(
            inputs.credit_file, inputs.spread, inputs.pack,
            inputs.policy_report, global_pack=inputs.global_pack,
        )
        universe = build_trace_universe(inputs)
        for figure in extract_figures(render_memo(memo)):
            assert figure.value in universe, (
                f"{figure.text} not in inputs for "
                f"{inputs.credit_file.credit_file_id}"
            )


# ---------------------------------------------------------------------
# The verifier: tamper cases are refused, offenders named
# ---------------------------------------------------------------------


def _tamper(memo, section_key, old, new):
    tampered = memo.model_copy(deep=True)
    section = tampered.section(section_key)
    assert section is not None
    for idx, paragraph in enumerate(section.paragraphs):
        if old in paragraph:
            section.paragraphs[idx] = paragraph.replace(old, new, 1)
            return tampered
    for table in section.tables:
        for r, row in enumerate(table.rows):
            for c, cell in enumerate(row):
                if old in cell:
                    table.rows[r][c] = cell.replace(old, new, 1)
                    return tampered
    raise AssertionError(f"{old!r} not found in section {section_key!r}")


def test_a_figure_altered_by_one_cent_is_refused():
    pipeline = hartline_pipeline()
    memo = hartline_memo(pipeline)
    tampered = _tamper(
        memo, "financial_analysis", "$2,202,000.00", "$2,202,000.01"
    )
    inputs = _inputs_for(pipeline, memo)
    with pytest.raises(MemoVerificationError) as caught:
        verify_memo(tampered, inputs)
    assert "$2,202,000.01" in caught.value.untraceable[0]
    assert "$2,202,000.01" in str(caught.value)


def test_a_transposed_ratio_is_refused():
    pipeline = hartline_pipeline()
    memo = hartline_memo(pipeline)
    tampered = _tamper(memo, "financial_analysis", "2.2590×", "2.2950×")
    with pytest.raises(MemoVerificationError) as caught:
        verify_memo(tampered, _inputs_for(pipeline, memo))
    assert any("2.2950×" in item for item in caught.value.untraceable)


def test_an_invented_figure_is_refused():
    pipeline = hartline_pipeline()
    memo = hartline_memo(pipeline)
    tampered = memo.model_copy(deep=True)
    tampered.section("strengths").paragraphs.append(
        "The borrower also holds $9,999,999.99 in off-statement reserves."
    )
    with pytest.raises(MemoVerificationError) as caught:
        verify_memo(tampered, _inputs_for(pipeline, memo))
    assert any("$9,999,999.99" in item for item in caught.value.untraceable)


def _inputs_for(pipeline, memo) -> MemoInputs:
    report = pipeline.store.get_policy_report(memo.policy_report_id)
    return MemoInputs(
        credit_file=pipeline.store.get_credit_file(memo.credit_file_id),
        spread=next(
            s for s in pipeline.store.list_spreads(memo.credit_file_id)
            if s.spread_id == memo.spread_id
        ),
        pack=pipeline.store.get_computation_pack(memo.computation_pack_id),
        policy_report=report,
        global_pack=(
            pipeline.store.get_computation_pack(memo.global_pack_id)
            if memo.global_pack_id else None
        ),
    )


# ---------------------------------------------------------------------
# Exceptions integrity
# ---------------------------------------------------------------------


def test_blocker_failure_is_carried_into_the_exceptions():
    spread, extractions = build_fixture_spread(
        "cf-memo-fixture", "levdoc",
        {2024: {"total_liabilities": "5000000", "total_equity": "1000000"}},
    )
    pack = compute_entity_pack(
        credit_file_id="cf-memo-fixture", entity="Fixture Borrower, LLC",
        spread=spread, extractions=extractions,
    )
    credit_file = make_file()
    report = evaluate_policy(credit_file, pack, policy_version="v1", as_of=AS_OF)
    memo = compose_memo(credit_file, spread, pack, report)
    exceptions = memo.section("policy_exceptions")
    assert "max_leverage_by_segment" in exceptions.exception_test_ids
    text = " ".join(exceptions.paragraphs)
    assert "FAIL" in text and "5.0000×" in text and "2.00×" in text
    assert memo.verification.exceptions_listed == [
        o.test_id for o in report.non_pass_outcomes()
    ]


def test_dropping_an_exception_is_refused_and_named():
    pipeline = hartline_pipeline()
    memo = hartline_memo(pipeline)
    tampered = memo.model_copy(deep=True)
    section = tampered.section("policy_exceptions")
    section.exception_test_ids.remove("min_dscr")
    section.paragraphs = [
        p for p in section.paragraphs
        if not p.startswith("Minimum debt service coverage (borrower)")
    ]
    with pytest.raises(MemoVerificationError) as caught:
        verify_memo(tampered, _inputs_for(pipeline, memo))
    assert caught.value.missing_exceptions == ["min_dscr"]
    assert "min_dscr" in str(caught.value)


def test_softening_a_not_evaluable_into_a_pass_is_refused():
    pipeline = hartline_pipeline()
    memo = hartline_memo(pipeline)
    tampered = memo.model_copy(deep=True)
    section = tampered.section("policy_exceptions")
    # Rewrite the DSCR exception as though it had passed — its
    # figures still trace, so only the structural check can catch it.
    section.paragraphs = [
        p.replace("NOT EVALUABLE", "PASS") if "debt service coverage (borrower)" in p else p
        for p in section.paragraphs
    ]
    section.exception_test_ids.remove("min_dscr")
    with pytest.raises(MemoVerificationError):
        verify_memo(tampered, _inputs_for(pipeline, memo))


def test_a_fully_clean_report_leaves_no_exceptions():
    credit_file = make_file(
        facility_type=FacilityType.CRE_LOAN,
        guarantors=("Eval Guarantor",),
        documents=[SourceDocument(
            document_id="doc-appraisal-1",
            credit_file_id="cf-memo-fixture",
            kind=DocumentKind.APPRAISAL,
            filename="appraisal.json",
            sha256="ab" * 32,
            byte_size=128,
            storage_key="docs/doc-appraisal-1",
            received_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )],
    )
    spread, extractions = build_fixture_spread(
        "cf-memo-fixture", "covdoc", COVERAGE_FIGURES
    )
    pack = compute_entity_pack(
        credit_file_id="cf-memo-fixture", entity="Fixture Borrower, LLC",
        spread=spread, extractions=extractions,
    )
    g_spread, g_extractions = build_fixture_spread(
        "cf-memo-fixture", "guardoc", STRONG_GUARANTOR
    )
    g_pack = compute_entity_pack(
        credit_file_id="cf-memo-fixture", entity="Eval Guarantor",
        spread=g_spread, extractions=g_extractions,
    )
    global_pack = compute_global_pack(
        credit_file_id="cf-memo-fixture",
        borrower_pack=pack,
        entity_packs={"Eval Guarantor": g_pack},
        expected_guarantors=["Eval Guarantor"],
    )
    report = evaluate_policy(
        credit_file, pack, policy_version="v1",
        global_pack=global_pack, as_of=AS_OF,
    )
    assert report.non_pass_outcomes() == []
    memo = compose_memo(
        credit_file, spread, pack, report, global_pack=global_pack
    )
    exceptions = memo.section("policy_exceptions")
    assert exceptions.exception_test_ids == []
    assert "records no exceptions" in " ".join(exceptions.paragraphs)
    assert memo.verification.exceptions_expected == []
    assert memo.global_pack_id == global_pack.pack_id


# ---------------------------------------------------------------------
# Refusal at the pipeline: nothing unverified is ever stored
# ---------------------------------------------------------------------


class _RogueComposer:
    name = "rogue-test"

    def compose(self, inputs):
        sections = get_composer("offline").compose(inputs)
        sections[0].paragraphs.append(
            "The facility is comfortably covered by $4,242,424.24 "
            "of unencumbered collateral."
        )
        return sections


def test_a_rogue_composer_is_refused_and_nothing_is_stored():
    pipeline = hartline_pipeline()
    credit_file = open_hartline(pipeline)
    before = pipeline.list_memos(credit_file.credit_file_id)
    memo_module.COMPOSER_PROFILES["rogue-test"] = _RogueComposer
    try:
        with pytest.raises(MemoVerificationError) as caught:
            pipeline.compose_memo(
                credit_file.credit_file_id, profile="rogue-test"
            )
        assert "$4,242,424.24" in str(caught.value)
    finally:
        del memo_module.COMPOSER_PROFILES["rogue-test"]
    assert pipeline.list_memos(credit_file.credit_file_id) == before


def test_live_profile_is_defined_but_not_configured():
    with pytest.raises(ValidationError, match="not"):
        get_composer("live").compose(coverage_inputs())
    with pytest.raises(ValidationError):
        get_composer("no-such-profile")
    pipeline = hartline_pipeline()
    credit_file = open_hartline(pipeline)
    with pytest.raises(ValidationError):
        pipeline.compose_memo(credit_file.credit_file_id, profile="live")


# ---------------------------------------------------------------------
# Determinism + storage
# ---------------------------------------------------------------------


def test_composition_is_deterministic():
    inputs = coverage_inputs()
    first = compose_memo(
        inputs.credit_file, inputs.spread, inputs.pack, inputs.policy_report
    )
    second = compose_memo(
        inputs.credit_file, inputs.spread, inputs.pack, inputs.policy_report
    )
    assert first.memo_id == second.memo_id
    assert render_memo(first) == render_memo(second)
    assert first.verification == second.verification


def test_pipeline_compose_is_idempotent_and_audited_once():
    pipeline = hartline_pipeline()
    memo = hartline_memo(pipeline)
    again = pipeline.compose_memo(
        memo.credit_file_id, report_id=memo.policy_report_id
    )
    assert again.memo_id == memo.memo_id
    audits = [
        e for e in pipeline.store.list_audit(memo.credit_file_id)
        if e.action == "memo_composed"
        and memo.memo_id in e.detail
    ]
    assert len(audits) == 1


def test_sqlite_store_round_trips_a_memo(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    pipeline = CreditPipeline(store=store, documents=LocalDocumentStore())
    credit_file = open_hartline(pipeline)
    memo = pipeline.list_memos(credit_file.credit_file_id)[0]
    reopened = SqliteCreditStore(tmp_path / "credit.db")
    loaded = reopened.get_memo(memo.memo_id)
    assert loaded is not None
    assert loaded.model_dump() == memo.model_dump()
    assert [m.memo_id for m in reopened.list_memos(credit_file.credit_file_id)] == [
        memo.memo_id
    ]


def test_migration_0005_is_registered():
    names = [f.name for f in migration_files(MIGRATIONS_DIR)]
    assert "0005_memos.sql" in names
    sql = (MIGRATIONS_DIR / "0005_memos.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS memos" in sql


def test_get_memo_on_the_wrong_file_is_not_found():
    pipeline = hartline_pipeline()
    memo = hartline_memo(pipeline)
    with pytest.raises(NotFoundError):
        pipeline.get_memo("cf-does-not-exist", memo.memo_id)


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


def _open_via_api(client) -> str:
    resp = client.post(
        "/applications/from-los", json={"application_ref": "LOS-2026-0117"}
    )
    assert resp.status_code == 201
    return resp.json()["credit_file_id"]


def test_api_compose_fetch_and_verification(client):
    credit_file_id = _open_via_api(client)
    listed = client.get(f"/applications/{credit_file_id}/memos")
    assert listed.status_code == 200
    assert len(listed.json()) == 1  # composed by the intake run
    memo_id = listed.json()[0]["memo_id"]

    composed = client.post(f"/applications/{credit_file_id}/memos", json={})
    assert composed.status_code == 201
    assert composed.json()["status"] == "draft"
    assert composed.json()["policy_version"] in ("v1", "v2")

    fetched = client.get(f"/applications/{credit_file_id}/memos/{memo_id}")
    assert fetched.status_code == 200
    assert fetched.json()["memo_id"] == memo_id
    assert fetched.json()["verification"]["verified"] is True

    verification = client.get(
        f"/applications/{credit_file_id}/memos/{memo_id}/verification"
    )
    assert verification.status_code == 200
    body = verification.json()
    assert body["figure_count"] == len(body["figures"])
    assert body["figure_count"] > 0
    # The LOS-opened file has no statements filed yet, so its
    # report is thick with not_evaluables — every one carried.
    assert body["exceptions_listed"]
    assert set(body["exceptions_listed"]) == set(body["exceptions_expected"])

    text = client.get(f"/applications/{credit_file_id}/memos/{memo_id}/text")
    assert text.status_code == 200
    assert "CREDIT MEMORANDUM" in text.json()["text"]
    assert "$1,500,000.00" in text.json()["text"]


def test_api_compose_with_a_pinned_pack_version(client):
    credit_file_id = _open_via_api(client)
    composed = client.post(
        f"/applications/{credit_file_id}/memos",
        json={"pack_version": "v1"},
    )
    assert composed.status_code == 201
    assert composed.json()["policy_version"] == "v1"
    listed = client.get(f"/applications/{credit_file_id}/memos").json()
    assert any(m["policy_version"] == "v1" for m in listed)
    assert len({m["memo_id"] for m in listed}) == len(listed)


def test_api_refusals(client):
    credit_file_id = _open_via_api(client)
    live = client.post(
        f"/applications/{credit_file_id}/memos", json={"profile": "live"}
    )
    assert live.status_code == 422
    unknown_profile = client.post(
        f"/applications/{credit_file_id}/memos", json={"profile": "nope"}
    )
    assert unknown_profile.status_code == 422
    missing = client.get(f"/applications/{credit_file_id}/memos/memo-nope")
    assert missing.status_code == 404
    missing_file = client.post("/applications/cf-nope/memos", json={})
    assert missing_file.status_code == 404
