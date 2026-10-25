"""Round 13 — the risk-rating scorecard.

The bank's rating model is the bank's; this blueprint ships a
coded stand-in and the discipline around it. These tests pin:

  the definition  a versioned scorecard held as data — weights
                  summing to exactly 1, a missing-data band on
                  every figure factor, grade bands covering
                  1–10 once; a broken definition is refused at
                  registration and a registered version is never
                  silently replaced
  the grading     Calloway, hand-computed factor by factor:
                  leverage 5.0000 → 8, DSCR 1.3125 → 5, current
                  ratio 1.7143 → 4, EBITDA-margin trend −0.0040
                  → 5, no test history → 5, no breaches → 2,
                  audited and uncontested → 2; composite 4.85,
                  grade 5 (watch). The breach year re-grades to
                  7.05, grade 7 (special mention)
  missing data    Hartline has no cash-flow page, so its EBITDA
                  and DSCR cannot compute: the factors land in
                  their named missing-data bands with the missing
                  input named — never zero-filled, never skipped
  the override    at most one grade from the ASSIGNED grade,
                  coded reason, a countersigning second officer;
                  both grades stay visible; the assignment
                  artefact is byte-identical after the move
  not a decision  the gates neither import nor consult the
                  rating (source scan + signatures), and a file
                  with a strong rating and no gate action is
                  exactly as undecided as one without
  the wiring      the memo states the rating in its header
                  facts; the annual review pack carries it with
                  its factor breakdown and states it in the
                  locked record; history keeps both versions and
                  labels the comparison cross-version
  the surfaces    the API, the SQLite round-trip, migration
                  0012, and no in-place rating mutation anywhere
"""

from __future__ import annotations

import inspect
import json
from datetime import date
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit import committee as committee_module
from commercial_credit import officer_gate as officer_gate_module
from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.db import migration_files
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.errors import NotFoundError, ValidationError
from commercial_credit.memo import render_memo
from commercial_credit.models import (
    Borrower,
    DocumentKind,
    FacilityRequest,
    FacilityType,
)
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.scorecard import (
    SCORECARD_ID,
    get_scorecard,
    register_scorecard,
    validate_scorecard_definition,
)
from commercial_credit.spreading import TEMPLATE_BY_FIELD
from commercial_credit.store import InMemoryCreditStore, SqliteCreditStore

from test_annual_reviews import breach_year_pipeline
from test_committee_gate import (
    CHAIR,
    CHAIR_ROLE,
    OFFICER,
    OFFICER_ROLE,
    RM,
    RM_ROLE,
    convened_final,
    decide,
    hartline_pipeline,
    open_hartline,
)
from test_covenants import approved_calloway

SECOND_OFFICER = "officer-mateo-reyes"
SECOND_OFFICER_ROLE = "senior_credit_officer"
AS_OF = date(2026, 6, 30)


def factor(assignment, factor_id):
    return next(
        f for f in assignment.factors if f.factor_id == factor_id
    )


# ---------------------------------------------------------------------
# The definition: data, validated whole
# ---------------------------------------------------------------------


def test_the_shipped_scorecard_is_valid_and_fixed():
    definition = get_scorecard("1")
    assert definition.scorecard_id == SCORECARD_ID
    assert sum(
        (f.weight for f in definition.factors), Decimal("0")
    ) == Decimal("1")
    assert [f.factor_id for f in definition.factors] == [
        "leverage", "dscr", "liquidity", "profitability_trend",
        "covenant_compliance", "breach_history", "statement_quality",
    ]
    for f in definition.factors:
        if f.kind == "figure":
            assert sum(1 for b in f.bands if b.is_missing_data) == 1
    covered = sorted(
        g
        for band in definition.grade_bands
        for g in range(band.grade_from, band.grade_to + 1)
    )
    assert covered == list(range(1, 11))
    with pytest.raises(ValidationError, match="unknown scorecard"):
        get_scorecard("99")


def test_a_broken_definition_is_refused_at_registration():
    good = get_scorecard("1")
    broken = good.model_copy(deep=True)
    broken.version = "90"
    broken.factors[0].weight = Decimal("0.50")  # weights now sum > 1
    with pytest.raises(ValidationError, match="weights sum"):
        register_scorecard(broken)
    broken2 = good.model_copy(deep=True)
    broken2.version = "91"
    broken2.factors[1].bands = [
        b for b in broken2.factors[1].bands if not b.is_missing_data
    ]
    with pytest.raises(ValidationError, match="missing-data band"):
        register_scorecard(broken2)
    # And a registered version is never quietly replaced.
    changed = good.model_copy(update={"note": "a different note"})
    with pytest.raises(ValidationError, match="already registered"):
        register_scorecard(changed)
    validate_scorecard_definition(good)  # the shipped one stands


# ---------------------------------------------------------------------
# The graded fixture: Calloway, factor by factor
# ---------------------------------------------------------------------


def test_calloway_assignment_grades_factor_by_factor():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(
        pipeline
    )
    cf = credit_file.credit_file_id
    assignment = pipeline.assign_rating(cf, as_of=AS_OF)
    assert assignment.scorecard_id == SCORECARD_ID
    assert assignment.scorecard_version == "1"
    assert assignment.latest_period == 2025
    pack = pipeline.store.get_computation_pack(
        assignment.computation_pack_id
    )
    assert pack is not None and pack.pack_id == assignment.computation_pack_id
    assert assignment.spread_id == pack.spread_id

    lev = factor(assignment, "leverage")
    assert lev.input_value == Decimal("5.0000")
    assert lev.band_id == "leverage_at_most_5_00" and lev.score == 8
    assert lev.weight == Decimal("0.20")
    assert lev.weighted_contribution == Decimal("1.60")
    assert lev.figure_key == "debt_to_tangible_net_worth"
    assert lev.fiscal_year == 2025

    dscr = factor(assignment, "dscr")
    assert dscr.input_value == Decimal("1.3125")
    assert dscr.band_id == "dscr_at_least_1_25" and dscr.score == 5
    assert dscr.weighted_contribution == Decimal("1.25")

    liq = factor(assignment, "liquidity")
    assert liq.input_value == Decimal("1.7143")
    assert liq.band_id == "current_ratio_at_least_1_50"
    assert liq.score == 4
    assert liq.weighted_contribution == Decimal("0.60")

    trend = factor(assignment, "profitability_trend")
    assert trend.input_value == Decimal("-0.0040")
    assert trend.band_id == "margin_trend_at_least_-0_0050"
    assert trend.score == 5
    assert trend.weighted_contribution == Decimal("0.50")

    comp = factor(assignment, "covenant_compliance")
    assert comp.input_category == "no_test_history" and comp.score == 5
    breach = factor(assignment, "breach_history")
    assert breach.input_category == "no_breach_history"
    assert breach.score == 2
    quality = factor(assignment, "statement_quality")
    assert quality.input_category == "audited_statements_clean"
    assert quality.score == 2

    assert assignment.composite_score == Decimal("4.85")
    assert assignment.assigned_grade == 5
    assert assignment.assigned_band == "watch"

    # Every figure factor's trace walks home: the pack figure it
    # names exists, and its recorded derivation steps are that
    # figure's own steps in the pack (both periods' steps, for
    # the trend factor).
    for f in assignment.factors:
        if f.kind != "figure" or f.input_value is None:
            continue
        assert f.derivation_steps  # the walk exists
        assert pack.pack_id in f.trace
        if f.factor_id == "profitability_trend":
            step_keys = {
                s.step_key
                for g in pack.figures if g.key == f.figure_key
                for s in g.steps
            }
            assert set(f.derivation_steps) <= step_keys
            assert len(f.derivation_steps) >= 2
        else:
            figure = next(
                g for g in pack.figures
                if g.key == f.figure_key
                and g.fiscal_year == f.fiscal_year
            )
            assert f.derivation_steps == [
                s.step_key for s in figure.steps
            ]

    # Content-addressed: assigning again over the same artefacts
    # at the same date returns the stored artefact, once.
    again = pipeline.assign_rating(cf, as_of=AS_OF)
    assert again.assignment_id == assignment.assignment_id
    assert len(pipeline.list_rating_assignments(cf)) == 1


def test_breach_year_regrades_with_the_record():
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    assignment = pipeline.assign_rating(cf, as_of=date(2027, 3, 1))
    assert assignment.latest_period == 2026
    assert factor(assignment, "leverage").input_value == Decimal("4.4348")
    assert factor(assignment, "leverage").score == 8
    assert factor(assignment, "dscr").input_value == Decimal("0.9143")
    assert factor(assignment, "dscr").band_id == "dscr_at_least_0_90"
    assert factor(assignment, "dscr").score == 9
    assert factor(assignment, "liquidity").input_value == Decimal("1.7310")
    assert factor(assignment, "profitability_trend").input_value == (
        Decimal("-0.0135")
    )
    assert factor(assignment, "profitability_trend").score == 7
    assert factor(assignment, "covenant_compliance").input_category == (
        "covenant_test_failed"
    )
    assert factor(assignment, "covenant_compliance").score == 8
    assert factor(assignment, "breach_history").input_category == (
        "open_breach"
    )
    assert factor(assignment, "breach_history").score == 9
    assert factor(assignment, "statement_quality").score == 2
    assert assignment.composite_score == Decimal("7.05")
    assert assignment.assigned_grade == 7
    assert assignment.assigned_band == "special mention"


# ---------------------------------------------------------------------
# Missing data: named bands, never zeros
# ---------------------------------------------------------------------


def test_hartline_missing_figures_land_in_named_bands():
    pipeline = hartline_pipeline()
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    assignment = pipeline.assign_rating(cf, as_of=AS_OF)
    dscr = factor(assignment, "dscr")
    assert dscr.input_value is None
    assert dscr.band_id == "dscr_insufficient_data"
    assert dscr.score == 8
    assert any(
        "depreciation_and_amortization" in m for m in dscr.missing_inputs
    )
    trend = factor(assignment, "profitability_trend")
    assert trend.input_value is None
    assert trend.band_id == "profitability_trend_insufficient_data"
    # Nothing was zero-filled anywhere: every figure factor either
    # carries its computed value or stands in its missing band.
    for f in assignment.factors:
        if f.kind == "figure":
            assert f.input_value is not None or (
                f.band_id.endswith("_insufficient_data")
                and f.missing_inputs
            )
    assert factor(assignment, "leverage").input_value == Decimal("1.5337")
    assert assignment.composite_score == Decimal("4.60")
    assert assignment.assigned_grade == 5


# ---------------------------------------------------------------------
# The strong borrower: every financial factor at its best band
# ---------------------------------------------------------------------

_BS = (
    "cash_and_equivalents", "accounts_receivable", "inventory",
    "total_current_assets", "net_fixed_assets", "total_assets",
    "accounts_payable", "current_portion_ltd",
    "total_current_liabilities", "long_term_debt",
    "total_liabilities", "total_equity",
)
_IS = (
    "revenue", "cost_of_goods_sold", "gross_profit",
    "operating_expenses", "ebit", "interest_expense", "net_income",
)
_CF = (
    "depreciation_and_amortization", "change_in_receivables",
    "change_in_inventory", "change_in_payables",
    "net_cash_from_operations", "capital_expenditures",
    "net_cash_from_investing", "debt_proceeds", "debt_repayments",
    "net_cash_from_financing", "net_change_in_cash",
)

# Sterling Fabrication: FY2025 leverage 2,000,000 / 3,200,000 =
# 0.6250, DSCR 1,104,000 / 470,000 = 2.3489, current ratio
# 3.0000, EBITDA margin 0.1000 → 0.1200 across the spread.
STERLING_FIGURES: dict[int, dict[str, int]] = {
    2023: dict(zip(_BS, (
        500000, 900000, 1100000, 2500000, 2000000, 4500000,
        600000, 300000, 1000000, 1400000, 2400000, 2100000,
    )), **dict(zip(_IS, (
        8000000, 5600000, 2400000, 1760000, 640000, 120000, 380000,
    ))), **dict(zip(_CF, (
        160000, -50000, -60000, 40000, 700000, 200000, -200000,
        100000, -250000, -150000, 350000,
    )))),
    2024: dict(zip(_BS, (
        600000, 950000, 1150000, 2700000, 2100000, 4800000,
        620000, 320000, 1050000, 1280000, 2330000, 2470000,
    )), **dict(zip(_IS, (
        8600000, 6020000, 2580000, 1806000, 774000, 115000, 470000,
    ))), **dict(zip(_CF, (
        172000, -50000, -50000, 20000, 800000, 220000, -220000,
        80000, -300000, -220000, 360000,
    )))),
    2025: dict(zip(_BS, (
        800000, 1000000, 1200000, 3000000, 2200000, 5200000,
        650000, 350000, 1000000, 1000000, 2000000, 3200000,
    )), **dict(zip(_IS, (
        9200000, 6440000, 2760000, 1840000, 920000, 150000, 560000,
    ))), **dict(zip(_CF, (
        184000, -50000, -50000, 30000, 950000, 250000, -250000,
        50000, -280000, -230000, 470000,
    )))),
}


def _sterling_document(year: int) -> bytes:
    figures = STERLING_FIGURES[year]

    def page(number: int, title: str, fields) -> dict:
        return {
            "page": number,
            "title": title,
            "lines": [
                {
                    "line": idx,
                    "label": TEMPLATE_BY_FIELD[field].label,
                    "value": figures[field],
                }
                for idx, field in enumerate(fields, start=1)
            ],
        }

    payload = {
        "document_type": "financial_statement",
        "borrower": "Sterling Fabrication, Inc.",
        "fiscal_year": year,
        "period_label": f"FY{year}",
        "basis": "audited",
        "pages": [
            page(1, "Balance Sheet", _BS),
            page(2, "Income Statement", _IS),
            page(3, "Cash Flow Statement", _CF),
        ],
    }
    return json.dumps(payload, indent=2).encode()


def sterling_pipeline():
    pipeline = hartline_pipeline()
    borrower = Borrower(
        legal_name="Sterling Fabrication, Inc.",
        ein_last4="9021",
        segment="manufacturing",
        state_of_formation="NC",
        years_in_business=21,
        primary_contact_name="Priya Sterling",
        primary_contact_email="priya@sterlingfab.example",
    )
    facility = FacilityRequest(
        facility_type=FacilityType.TERM_LOAN,
        amount=Decimal("1200000"),
        purpose="Second machining line",
        term_months=60,
        collateral_description="Equipment and receivables",
        guarantors=[],
    )
    docs = [
        {
            "kind": DocumentKind.FINANCIAL_STATEMENT.value,
            "filename": f"sterling-fy{year}-statements.json",
            "data_hex": _sterling_document(year).hex(),
            "period_label": f"FY{year}",
        }
        for year in (2023, 2024, 2025)
    ]
    credit_file = pipeline.open_application(
        borrower, facility, documents=docs
    )
    return pipeline, credit_file


def test_strong_borrower_grades_at_the_top_bands():
    pipeline, credit_file = sterling_pipeline()
    cf = credit_file.credit_file_id
    assignment = pipeline.assign_rating(cf, as_of=AS_OF)
    assert factor(assignment, "leverage").input_value == Decimal("0.6250")
    assert factor(assignment, "leverage").score == 1
    assert factor(assignment, "dscr").input_value == Decimal("2.3489")
    assert factor(assignment, "dscr").score == 1
    assert factor(assignment, "liquidity").input_value == Decimal("3.0000")
    assert factor(assignment, "liquidity").score == 1
    trend = factor(assignment, "profitability_trend")
    assert trend.input_value == Decimal("0.0200") and trend.score == 1
    assert assignment.composite_score == Decimal("1.60")
    assert assignment.assigned_grade == 2
    assert assignment.assigned_band == "pass"


# ---------------------------------------------------------------------
# The override: bounded, coded, countersigned, beside the artefact
# ---------------------------------------------------------------------


def _calloway_assignment():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(
        pipeline
    )
    cf = credit_file.credit_file_id
    assignment = pipeline.assign_rating(cf, as_of=AS_OF)
    return pipeline, cf, assignment


def test_override_refusals():
    pipeline, cf, assignment = _calloway_assignment()
    aid = assignment.assignment_id

    def attempt(**overrides):
        kwargs = dict(
            officer_id=OFFICER, officer_role=OFFICER_ROLE,
            new_grade=4, reason_code="collateral_strength",
            countersigner_id=SECOND_OFFICER,
            countersigner_role=SECOND_OFFICER_ROLE,
        )
        kwargs.update(overrides)
        return lambda: pipeline.override_rating(cf, aid, **kwargs)

    with pytest.raises(ValidationError, match="at most one grade"):
        attempt(new_grade=7)()
    with pytest.raises(ValidationError, match="at most one grade"):
        attempt(new_grade=3)()
    with pytest.raises(ValidationError, match="changes nothing"):
        attempt(new_grade=5)()
    with pytest.raises(ValidationError, match="must stand on the"):
        attempt(new_grade=11)()
    with pytest.raises(ValidationError, match="unknown rating override"):
        attempt(reason_code="gut_feel")()
    with pytest.raises(ValidationError, match="requires a note"):
        attempt(reason_code="other", note="")()
    with pytest.raises(ValidationError, match="countersigning"):
        attempt(countersigner_id=None)()
    with pytest.raises(ValidationError, match="different officer"):
        attempt(countersigner_id=OFFICER)()
    with pytest.raises(ValidationError, match="countersign"):
        attempt(countersigner_role=RM_ROLE)()
    with pytest.raises(ValidationError):
        attempt(officer_id="")()
    with pytest.raises(ValidationError):
        attempt(officer_role=RM_ROLE)()
    assert pipeline.list_rating_overrides(cf) == []


def test_override_lands_and_both_grades_stay_visible():
    pipeline, cf, assignment = _calloway_assignment()
    aid = assignment.assignment_id
    before = assignment.model_dump_json()
    override = pipeline.override_rating(
        cf, aid,
        officer_id=OFFICER, officer_role=OFFICER_ROLE,
        new_grade=4, reason_code="collateral_strength",
        countersigner_id=SECOND_OFFICER,
        countersigner_role=SECOND_OFFICER_ROLE,
    )
    assert override.assigned_grade == 5 and override.new_grade == 4
    assert override.previous_grade == 5
    assert override.reason_vocabulary == "rating-override-reasons-v1"
    # The assignment artefact is untouched by the move.
    stored = pipeline.get_rating_assignment(cf, aid)
    assert stored.model_dump_json() == before
    assert stored.assigned_grade == 5
    view = pipeline.rating_view(stored)
    assert view["assigned_grade"] == 5 and view["final_grade"] == 4
    assert view["final_band"] == "pass"
    assert view["override"].override_id == override.override_id
    # Back to 5 is lawful (one grade from assigned); from there,
    # 3 would stand two grades from the assigned grade — the
    # chain may not walk the bound away.
    back = pipeline.override_rating(
        cf, aid,
        officer_id=SECOND_OFFICER, officer_role=SECOND_OFFICER_ROLE,
        new_grade=5, reason_code="documented_adverse_information",
        countersigner_id=OFFICER, countersigner_role=OFFICER_ROLE,
    )
    assert back.previous_grade == 4
    assert back.supersedes_override_id == override.override_id
    with pytest.raises(ValidationError, match="at most one grade"):
        pipeline.override_rating(
            cf, aid,
            officer_id=OFFICER, officer_role=OFFICER_ROLE,
            new_grade=3, reason_code="collateral_strength",
            countersigner_id=SECOND_OFFICER,
            countersigner_role=SECOND_OFFICER_ROLE,
        )
    audit = pipeline.store.list_audit(cf)
    assert any(e.action == "risk_rating_assigned" for e in audit)
    assert any(e.action == "risk_rating_overridden" for e in audit)


# ---------------------------------------------------------------------
# The rating is an input, never a decision
# ---------------------------------------------------------------------


def test_the_gates_neither_import_nor_consult_the_rating():
    for module in (committee_module, officer_gate_module):
        source = inspect.getsource(module).lower()
        assert "scorecard" not in source
        assert "ratingassignment" not in source.replace("_", "")
    for fn in (
        CreditPipeline.decide_committee,
        CreditPipeline.convene_committee,
        CreditPipeline.approve_memo,
    ):
        assert not any(
            "rating" in name
            for name in inspect.signature(fn).parameters
        )


def test_a_rating_alone_decides_nothing():
    # Twin files: one rated before its committee, one never
    # rated. The committee's decision is identical in outcome —
    # the rating was in the room and changed nothing.
    rated = hartline_pipeline()
    credit_file, draft, final = convened_final(rated)
    cf = credit_file.credit_file_id
    rated.assign_rating(cf, as_of=AS_OF)
    rated_decision = decide(rated, cf, final.memo_id)
    plain = hartline_pipeline()
    credit_file2, draft2, final2 = convened_final(plain)
    cf2 = credit_file2.credit_file_id
    plain_decision = decide(plain, cf2, final2.memo_id)
    assert rated_decision.outcome == plain_decision.outcome
    # And the strong borrower's grade-2 rating, with no gate
    # action at all, leaves its memo a draft and its file
    # undecided: a rating is not an approval wearing a number.
    pipeline, sterling = sterling_pipeline()
    scf = sterling.credit_file_id
    pipeline.assign_rating(scf, as_of=AS_OF)
    memo = pipeline.compose_memo(scf)
    assert pipeline.memo_status(scf, memo.memo_id) == "draft"
    assert pipeline.store.list_committee_decisions(scf) == []


# ---------------------------------------------------------------------
# Wiring: the memo header and the review pack
# ---------------------------------------------------------------------


def test_the_memo_states_the_current_rating_in_its_header():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(
        pipeline
    )
    cf = credit_file.credit_file_id
    before = pipeline.compose_memo(cf)
    assert before.rating_assignment_id is None
    assert "Risk rating" not in render_memo(before)

    assignment = pipeline.assign_rating(cf, as_of=AS_OF)
    memo = pipeline.compose_memo(cf)
    assert memo.memo_id != before.memo_id
    assert memo.rating_assignment_id == assignment.assignment_id
    assert memo.rating_assigned_grade == 5
    assert memo.rating_grade == 5 and memo.rating_band == "watch"
    assert memo.rating_override_id is None
    header = render_memo(memo)
    assert "Risk rating (assigned): grade `5` (watch)" in header
    assert f"assignment `{assignment.assignment_id}`" in header

    pipeline.override_rating(
        cf, assignment.assignment_id,
        officer_id=OFFICER, officer_role=OFFICER_ROLE,
        new_grade=4, reason_code="collateral_strength",
        countersigner_id=SECOND_OFFICER,
        countersigner_role=SECOND_OFFICER_ROLE,
    )
    moved = pipeline.compose_memo(cf)
    assert moved.rating_grade == 4 and moved.rating_band == "pass"
    assert moved.rating_override_id is not None
    assert "Risk rating (final, after override" in render_memo(moved)


def test_the_review_pack_carries_the_rating_and_its_breakdown():
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    unrated = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )
    assert unrated.rating_assignment_id is None
    assert unrated.rating_factors == []
    assert not any(
        p.startswith("Risk rating")
        for p in unrated.summary.locked_facts
    )

    assignment = pipeline.assign_rating(cf, as_of=date(2027, 3, 1))
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )
    assert pack.pack_id != unrated.pack_id
    assert pack.rating_assignment_id == assignment.assignment_id
    assert pack.rating_grade == 7
    assert pack.rating_band == "special mention"
    assert [f.factor_id for f in pack.rating_factors] == [
        f.factor_id for f in assignment.factors
    ]
    locked = [
        p for p in pack.summary.locked_facts
        if p.startswith("Risk rating")
    ]
    assert len(locked) == 1
    assert "it decides nothing" in locked[0]
    # The stored pack's summary re-verifies against its pinned
    # inputs — the rating paragraph recomputes identically.
    verified = pipeline.check_review_summary(
        cf, pack.pack_id,
        paragraphs=pack.summary.paragraphs,
        locked_facts=pack.summary.locked_facts,
    )
    assert verified.verified


# ---------------------------------------------------------------------
# History: versions append, never rewrite
# ---------------------------------------------------------------------


def test_history_keeps_versions_and_labels_the_comparison():
    pipeline, cf, assignment = _calloway_assignment()
    before = pipeline.get_rating_assignment(
        cf, assignment.assignment_id
    ).model_dump_json()
    v2 = get_scorecard("1").model_copy(update={
        "version": "77",
        "note": "A second registered version for the history "
                "test — same factors, new version.",
    })
    register_scorecard(v2)
    second = pipeline.assign_rating(
        cf, as_of=AS_OF, scorecard_version="77"
    )
    assert second.assignment_id != assignment.assignment_id
    assert second.scorecard_version == "77"
    assert second.assigned_grade == assignment.assigned_grade
    # The v1 artefact stands byte-identical under its own version.
    after = pipeline.get_rating_assignment(
        cf, assignment.assignment_id
    )
    assert after.model_dump_json() == before
    history = pipeline.rating_history(cf)
    assert len(history) == 2
    assert history[0]["cross_version"] is False
    assert history[1]["cross_version"] is True
    assert "not directly comparable" in history[1]["comparison_basis"]
    assert history[0]["assignment"].scorecard_version == "1"


# ---------------------------------------------------------------------
# Surfaces: API, SQLite round-trip, migration, no in-place writes
# ---------------------------------------------------------------------


@pytest.fixture()
def client():
    pipeline = hartline_pipeline()
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app), pipeline


def test_api_rating_surfaces(client):
    test_client, pipeline = client
    credit_file, final, decision, covenant_set = approved_calloway(
        pipeline
    )
    cf = credit_file.credit_file_id
    created = test_client.post(
        f"/applications/{cf}/risk-rating",
        json={"as_of": "2026-06-30"},
    )
    assert created.status_code == 201, created.text
    view = created.json()
    assert view["assigned_grade"] == 5
    assert view["final_grade"] == 5
    assert Decimal(
        str(view["assignment"]["composite_score"])
    ) == Decimal("4.85")
    aid = view["assignment"]["assignment_id"]

    listed = test_client.get(f"/applications/{cf}/risk-ratings")
    assert listed.status_code == 200
    assert [v["assignment"]["assignment_id"] for v in listed.json()] == [aid]

    refused = test_client.post(
        f"/applications/{cf}/risk-ratings/{aid}/override",
        json={
            "officer_id": OFFICER, "officer_role": OFFICER_ROLE,
            "new_grade": 7, "reason_code": "collateral_strength",
            "countersigner_id": SECOND_OFFICER,
            "countersigner_role": SECOND_OFFICER_ROLE,
        },
    )
    assert refused.status_code == 422, refused.text

    landed = test_client.post(
        f"/applications/{cf}/risk-ratings/{aid}/override",
        json={
            "officer_id": OFFICER, "officer_role": OFFICER_ROLE,
            "new_grade": 4, "reason_code": "collateral_strength",
            "countersigner_id": SECOND_OFFICER,
            "countersigner_role": SECOND_OFFICER_ROLE,
        },
    )
    assert landed.status_code == 201, landed.text
    detail = test_client.get(f"/applications/{cf}/risk-ratings/{aid}")
    assert detail.json()["final_grade"] == 4
    assert detail.json()["assigned_grade"] == 5
    history = test_client.get(
        f"/applications/{cf}/risk-rating-history"
    )
    assert history.status_code == 200 and len(history.json()) == 1
    missing = test_client.get(
        f"/applications/{cf}/risk-ratings/rating-nope"
    )
    assert missing.status_code == 404


def test_sqlite_round_trip_and_migration_0012(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    pipeline = CreditPipeline(
        store=store, documents=LocalDocumentStore()
    )
    credit_file, final, decision, covenant_set = approved_calloway(
        pipeline
    )
    cf = credit_file.credit_file_id
    assignment = pipeline.assign_rating(cf, as_of=AS_OF)
    override = pipeline.override_rating(
        cf, assignment.assignment_id,
        officer_id=OFFICER, officer_role=OFFICER_ROLE,
        new_grade=4, reason_code="collateral_strength",
        countersigner_id=SECOND_OFFICER,
        countersigner_role=SECOND_OFFICER_ROLE,
    )
    reread = SqliteCreditStore(tmp_path / "credit.db")
    assert reread.get_rating_assignment(
        assignment.assignment_id
    ) == assignment
    assert reread.list_rating_assignments(cf) == [assignment]
    assert reread.get_rating_override(override.override_id) == override
    assert reread.list_rating_overrides(cf) == [override]

    names = [f.name for f in migration_files(MIGRATIONS_DIR)]
    assert "0012_risk_ratings.sql" in names
    sql = (MIGRATIONS_DIR / "0012_risk_ratings.sql").read_text()
    assert "rating_assignments" in sql and "rating_overrides" in sql


def test_no_in_place_rating_mutation_surface():
    from commercial_credit.store import (
        InMemoryCreditStore,
        PostgresCreditStore,
        SqliteCreditStore,
    )

    for cls in (InMemoryCreditStore, SqliteCreditStore, PostgresCreditStore):
        names = [n for n in dir(cls) if "rating" in n]
        assert set(names) == {
            "save_rating_assignment", "get_rating_assignment",
            "list_rating_assignments", "save_rating_override",
            "get_rating_override", "list_rating_overrides",
        }, (cls.__name__, names)
    with pytest.raises(NotFoundError, match="not found"):
        hartline_pipeline().get_rating_assignment(
            "cf-nope", "rating-nope"
        )
