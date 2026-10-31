"""Round 11 — covenant test execution and breach cases.

Round 10 derived the schedule; this round runs the tests on it.
These tests pin:

  execution    a due test re-spreads the period and recomputes the
               covenant's metric through the existing engines —
               the recorded figure is the engine's figure (hand-
               computed here to the fourth decimal), compared
               against the covenant's threshold in a content-
               addressed result artefact; re-running records
               nothing twice
  honesty      awaiting_inputs when the statements have not
               arrived (recorded, naming what is missing);
               not_evaluable when they arrived without the lines
               the metric needs — named, and never read as a pass
  breaches     a failure opens its breach case in the same act;
               resolutions are coded acts of named officers —
               cured only by a later passing re-test of the same
               covenant linked by id, waived only on a committee
               decision's authority, escalated and defaulted under
               coded reasons; terminal states refuse everything;
               nothing deletes or silently closes a breach
  restatement  a restated period flags the affected results
               superseded_by_restatement and schedules a re-test;
               the original record stands
  the store    SQLite round-trip; migration 0010 registered

Fixtures ride the committee-ready Calloway file (FY2023–2025
spread; FY2025 DSCR 1.3125, leverage 5.0000). The FY2026
statements delivered here are hand-built to fail: EBITDA
$320,000 (EBIT $200,000 + D&A $120,000) over debt service
$350,000 (FY2025 CPLTD $240,000 + interest $110,000) gives DSCR
0.9143 against the 1.25 floor; leverage 5,100,000 / 1,150,000 =
4.4348 against the 2.50 ceiling; the current ratio 2,510,000 /
1,450,000 = 1.7310 passes its 1.20 floor. FY2027 recovers: DSCR
540,000 / 360,000 = 1.5000 — the cure.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.db import migration_files
from commercial_credit.documents import LocalDocumentStore, register_document
from commercial_credit.errors import NotFoundError, ValidationError
from commercial_credit.models import DocumentKind
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.store import InMemoryCreditStore, SqliteCreditStore

from test_committee_gate import (
    _BS_FIELDS,
    _CF_FIELDS,
    _IS_FIELDS,
    CHAIR,
    CHAIR_ROLE,
    INFO_REASON,
    LEVERAGE_REASON,
    OFFICER,
    OFFICER_ROLE,
    convened_final,
    decide,
    hartline_pipeline,
)
from test_covenants import approved_calloway

# ---------------------------------------------------------------------
# Hand-built statements for the years after the fixture's spread.
# ---------------------------------------------------------------------

FY2026_FIGURES = dict(zip(_BS_FIELDS, (
    260000, 950000, 1300000, 2510000, 3740000, 6250000,
    850000, 260000, 1450000, 3650000, 5100000, 1150000,
)), **dict(zip(_IS_FIELDS, (
    8600000, 6020000, 2580000, 2380000, 200000, 110000, 60000,
))), **dict(zip(_CF_FIELDS, (
    120000, -50000, -100000, 50000, 80000, 260000, -260000,
    270000, -200000, 70000, -110000,
))))

FY2027_FIGURES = dict(zip(_BS_FIELDS, (
    300000, 1000000, 1400000, 2700000, 3900000, 6600000,
    900000, 280000, 1500000, 3600000, 5100000, 1500000,
)), **dict(zip(_IS_FIELDS, (
    9800000, 6860000, 2940000, 2540000, 400000, 100000, 210000,
))), **dict(zip(_CF_FIELDS, (
    140000, -50000, -100000, 50000, 450000, 300000, -300000,
    100000, -210000, -110000, 40000,
))))

# The restated FY2026: EBIT corrected to $320,000 — EBITDA
# $440,000 over the same $350,000 debt service gives DSCR
# 1.2571, which passes the 1.25 floor.
FY2026_RESTATED_FIGURES = dict(
    FY2026_FIGURES,
    ebit=320000, operating_expenses=2260000, net_income=180000,
)


def statement_document(year, figures, *, with_cash_flow=True):
    from commercial_credit.spreading import TEMPLATE_BY_FIELD

    def page(number, title, fields):
        return {
            "page": number, "title": title,
            "lines": [
                {
                    "line": i,
                    "label": TEMPLATE_BY_FIELD[f].label,
                    "value": figures[f],
                }
                for i, f in enumerate(fields, start=1)
            ],
        }

    pages = [
        page(1, "Balance Sheet", _BS_FIELDS),
        page(2, "Income Statement", _IS_FIELDS),
    ]
    if with_cash_flow:
        pages.append(page(3, "Cash Flow Statement", _CF_FIELDS))
    return json.dumps({
        "document_type": "financial_statement",
        "borrower": "Calloway Precision Manufacturing, Inc.",
        "fiscal_year": year, "period_label": f"FY{year}",
        "basis": "audited", "pages": pages,
    }).encode()


def deliver_statements(pipeline, cf, year, figures, **kwargs):
    stored = pipeline.store.get_credit_file(cf)
    doc = register_document(
        pipeline.store, pipeline.documents, stored,
        kind=DocumentKind.FINANCIAL_STATEMENT,
        filename=f"calloway-fy{year}-statements.json",
        data=statement_document(year, figures, **kwargs),
        period_label=f"FY{year}",
    )
    pipeline.run_extraction(cf)
    return doc


def find_instance(pipeline, cf, *, metric_key, fiscal_year, as_of):
    schedule = pipeline.covenant_schedule(cf, as_of=as_of)
    for view in schedule["sets"]:
        for instance in view["instances"]:
            if (
                instance.kind == "covenant_test"
                and instance.metric_key == metric_key
                and instance.fiscal_year == fiscal_year
            ):
                return instance
    raise AssertionError(
        f"no {metric_key} instance for FY{fiscal_year} at {as_of}"
    )


def results_for(pipeline, cf, *, metric_key=None, fiscal_year=None):
    out = []
    for view in pipeline.covenant_test_results(cf):
        if metric_key is not None and view["metric_key"] != metric_key:
            continue
        if fiscal_year is not None and view["fiscal_year"] != fiscal_year:
            continue
        out.append(view)
    return out


def dscr_breach(pipeline, cf):
    for breach in pipeline.list_breach_cases(
        as_of=date(2027, 9, 1), credit_file_id=cf
    ):
        if breach["metric_key"] == "dscr":
            return breach
    raise AssertionError("no DSCR breach on the file")


def approved_with_fy2026(*, figures=FY2026_FIGURES, **kwargs):
    """An approved Calloway file whose FY2026 statements have
    arrived and been extracted. Returns (pipeline, cf)."""
    pipeline = hartline_pipeline()
    credit_file, _final, _decision, _set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    deliver_statements(pipeline, cf, 2026, figures, **kwargs)
    return pipeline, cf


# ---------------------------------------------------------------------
# Execution: the graded math
# ---------------------------------------------------------------------


def test_a_due_test_records_the_engines_figure_exactly():
    pipeline, cf = approved_with_fy2026()
    instance = find_instance(
        pipeline, cf, metric_key="dscr", fiscal_year=2026,
        as_of=date(2027, 8, 1),
    )
    assert instance.state in ("due", "overdue")
    result = pipeline.run_covenant_test(
        cf, instance.instance_id, as_of=date(2027, 8, 1)
    )
    assert result.outcome == "fail"
    # Hand-computed: EBITDA 320,000 / debt service 350,000.
    assert result.figure_value == Decimal("0.9143")
    assert result.threshold == Decimal("1.25")
    assert result.operator == ">="
    assert result.fiscal_year == 2026
    assert result.computation_pack_id and result.spread_id
    # The figure is the pinned pack's own covenant result — the
    # test compared; it did not calculate.
    pack = pipeline.store.get_computation_pack(result.computation_pack_id)
    engine = next(
        r for r in pack.covenant_results if r.metric_key == "dscr"
    )
    assert engine.value == result.figure_value
    # Period sources are pinned: the FY2026 statement document.
    assert {s.fiscal_year for s in result.period_sources} == {2026}


def test_a_passing_test_records_clean_and_opens_no_breach():
    pipeline, cf = approved_with_fy2026()
    schedule = pipeline.covenant_schedule(cf, as_of=date(2026, 8, 1))
    candidates = [
        instance
        for view in schedule["sets"]
        for instance in view["instances"]
        if instance.kind == "covenant_test"
        and instance.metric_key == "current_ratio"
        and instance.fiscal_year == 2025
    ]
    instance = min(candidates, key=lambda i: i.test_date)
    as_of = instance.test_date + timedelta(days=1)
    result = pipeline.run_covenant_test(
        cf, instance.instance_id, as_of=as_of
    )
    assert result.outcome == "pass"
    assert result.figure_value == Decimal("1.7143")  # 2,400,000/1,400,000
    breaches = pipeline.list_breach_cases(
        as_of=as_of, credit_file_id=cf
    )
    assert all(
        b["test_result_id"] != result.result_id for b in breaches
    )


def test_quarterly_leverage_fails_on_the_fy2026_figures():
    pipeline, cf = approved_with_fy2026()
    ran = pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    leverage = [
        e["result"] for e in ran["results"]
        if e["result"].metric_key == "debt_to_tangible_net_worth"
        and e["result"].fiscal_year == 2026
    ]
    assert leverage, "the FY2026 leverage tests ran"
    for result in leverage:
        assert result.outcome == "fail"
        # 5,100,000 / 1,150,000, to the engine's four places.
        assert result.figure_value == Decimal("4.4348")


def test_rerunning_records_nothing_twice():
    pipeline, cf = approved_with_fy2026()
    first = pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    assert first["results"]
    second = pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    assert second["results"] == [] and second["breaches"] == []
    tick = pipeline.tick_covenants(
        as_of=date(2027, 8, 1), credit_file_id=cf
    )
    assert tick["tests_recorded"] == []
    stored = pipeline.store.list_covenant_test_results(cf)
    ids = [r.result_id for r in stored]
    assert len(ids) == len(set(ids))


def test_a_test_cannot_run_before_it_is_due():
    pipeline, cf = approved_with_fy2026()
    instance = find_instance(
        pipeline, cf, metric_key="dscr", fiscal_year=2026,
        as_of=date(2026, 8, 1),
    )
    assert instance.state in ("scheduled", "due_soon")
    with pytest.raises(ValidationError, match="not before|stands at"):
        pipeline.run_covenant_test(
            cf, instance.instance_id, as_of=date(2026, 8, 1)
        )


# ---------------------------------------------------------------------
# Honest outcomes: awaiting_inputs and not_evaluable
# ---------------------------------------------------------------------


def test_missing_statements_record_awaiting_inputs():
    pipeline = hartline_pipeline()
    credit_file, _f, _d, _s = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    ran = pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    awaiting = [
        e["result"] for e in ran["results"]
        if e["result"].outcome == "awaiting_inputs"
    ]
    assert awaiting, "the FY2026 DSCR test stands awaiting inputs"
    dscr = next(r for r in awaiting if r.metric_key == "dscr")
    assert dscr.fiscal_year == 2026
    assert any("FY2026" in name for name in dscr.missing_inputs)
    assert dscr.figure_value is None
    # An awaiting test is not a failure and opens no breach.
    assert all(
        b["test_result_id"] != dscr.result_id for b in
        pipeline.list_breach_cases(
            as_of=date(2027, 8, 1), credit_file_id=cf
        )
    )
    # And the wait is recorded once, however often the tick runs.
    again = pipeline.tick_covenants(
        as_of=date(2027, 8, 1), credit_file_id=cf
    )
    assert all(
        e["result"].result_id != dscr.result_id
        for e in again["tests_recorded"]
    )


def test_awaiting_then_arrival_runs_the_real_test_in_its_place():
    pipeline = hartline_pipeline()
    credit_file, _f, _d, _s = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    deliver_statements(pipeline, cf, 2026, FY2026_FIGURES)
    ran = pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 2))
    dscr = next(
        e["result"] for e in ran["results"]
        if e["result"].metric_key == "dscr"
    )
    assert dscr.outcome == "fail" and dscr.is_retest
    awaiting = next(
        v for v in results_for(pipeline, cf, metric_key="dscr")
        if v["outcome"] == "awaiting_inputs"
    )
    assert dscr.supersedes_result_id == awaiting["result_id"]


def test_statements_without_the_cash_flow_page_are_not_evaluable():
    pipeline, cf = approved_with_fy2026(with_cash_flow=False)
    ran = pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    dscr = next(
        e["result"] for e in ran["results"]
        if e["result"].metric_key == "dscr"
    )
    # The figure cannot be computed, the missing line is named,
    # and the outcome is not_evaluable — never a pass, never a
    # silent skip, and no breach: nothing has been measured.
    assert dscr.outcome == "not_evaluable"
    assert dscr.figure_value is None
    assert any(
        "depreciation_and_amortization" in name
        for name in dscr.missing_inputs
    )
    assert all(
        b["test_result_id"] != dscr.result_id for b in
        pipeline.list_breach_cases(
            as_of=date(2027, 8, 1), credit_file_id=cf
        )
    )


# ---------------------------------------------------------------------
# Breach cases: opening, ageing, resolutions
# ---------------------------------------------------------------------


def test_a_failure_opens_its_breach_case_with_a_cure_clock():
    pipeline, cf = approved_with_fy2026()
    pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    breach = dscr_breach(pipeline, cf)
    assert breach["state"] == "open"
    assert Decimal(str(breach["figure_value"])) == Decimal("0.9143")
    assert Decimal(str(breach["threshold"])) == Decimal("1.25")
    assert breach["borrower_name"].startswith("Calloway")
    # Cure deadline: test date + the covenant's 30-day cure
    # period (the committee condition's grace).
    result = next(
        v for v in results_for(pipeline, cf, metric_key="dscr")
        if v["outcome"] == "fail"
    )
    assert breach["test_result_id"] == result["result_id"]
    assert breach["test_date"] == result["test_date"]
    from datetime import timedelta
    test_date = date.fromisoformat(result["test_date"])
    assert date.fromisoformat(breach["cure_deadline"]) == (
        test_date + timedelta(days=30)
    )
    # Ageing is derived at read time.
    view = pipeline.get_breach_case(
        cf, breach["breach_id"], as_of=date(2027, 9, 1)
    )
    assert view["days_in_breach"] == (date(2027, 9, 1) - test_date).days
    assert view["cure_overdue"] is True
    assert view["events"][0]["action"] == "opened"


def test_breach_review_and_escalation_are_coded_named_acts():
    pipeline, cf = approved_with_fy2026()
    pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    breach = dscr_breach(pipeline, cf)
    bid = breach["breach_id"]
    with pytest.raises(ValidationError, match="named actor"):
        pipeline.review_breach(
            cf, bid, actor_id="", actor_role="credit_officer"
        )
    with pytest.raises(ValidationError, match="may not act"):
        pipeline.review_breach(
            cf, bid, actor_id="rm-1", actor_role="relationship_manager"
        )
    with pytest.raises(ValidationError, match="unknown breach"):
        pipeline.escalate_breach(
            cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
            reason_code="feels_risky",
        )
    reviewed = pipeline.review_breach(
        cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
        note="Taking this one.",
    )
    assert reviewed.state == "under_review"
    escalated = pipeline.escalate_breach(
        cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
        reason_code="cure_period_expiring",
    )
    assert escalated.state == "escalated"
    assert [e.action for e in escalated.events] == [
        "opened", "under_review", "escalated",
    ]


def test_cure_requires_a_later_passing_retest_of_the_same_covenant():
    pipeline, cf = approved_with_fy2026()
    pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    breach = dscr_breach(pipeline, cf)
    bid = breach["breach_id"]
    failing_id = breach["test_result_id"]
    # The failing result cannot cure its own breach.
    with pytest.raises(ValidationError, match="cannot cure its own"):
        pipeline.cure_breach(
            cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
            cure_result_id=failing_id,
        )
    # A passing result for a DIFFERENT covenant cannot either.
    cr_pass = next(
        v for v in results_for(pipeline, cf, metric_key="current_ratio")
        if v["outcome"] == "pass"
    )
    with pytest.raises(ValidationError, match="not the"):
        pipeline.cure_breach(
            cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
            cure_result_id=cr_pass["result_id"],
        )
    # FY2027 recovers: DSCR 1.5000. The cure lands, linked by id.
    deliver_statements(pipeline, cf, 2027, FY2027_FIGURES)
    pipeline.run_due_covenant_tests(cf, as_of=date(2028, 8, 1))
    cure = next(
        v for v in results_for(pipeline, cf, metric_key="dscr",
                                fiscal_year=2027)
        if v["outcome"] == "pass"
    )
    assert Decimal(str(cure["figure_value"])) == Decimal("1.5000")
    cured = pipeline.cure_breach(
        cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
        cure_result_id=cure["result_id"],
    )
    assert cured.state == "cured"
    assert cured.events[-1].test_result_id == cure["result_id"]
    # A cured breach is terminal: nothing moves it again.
    with pytest.raises(ValidationError, match="terminal"):
        pipeline.escalate_breach(
            cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
            reason_code="repeat_breach",
        )


def test_waiver_stands_only_on_a_committee_decisions_authority():
    pipeline = hartline_pipeline()
    credit_file, _final, decision, _s = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    deliver_statements(pipeline, cf, 2026, FY2026_FIGURES)
    pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    breach = dscr_breach(pipeline, cf)
    bid = breach["breach_id"]
    # No reference at all: a free-text waiver, refused.
    with pytest.raises(ValidationError, match="free-text waiver"):
        pipeline.waive_breach(
            cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
            decision_id="",
        )
    # An invented reference: refused the same way.
    with pytest.raises(ValidationError, match="free-text waiver"):
        pipeline.waive_breach(
            cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
            decision_id="decision-that-does-not-exist",
        )
    # A decline on ANOTHER file is not authority here.
    o_file, _d, o_final = convened_final(pipeline)
    decline = decide(
        pipeline, o_file.credit_file_id, o_final.memo_id,
        outcome="decline",
        adverse_reasons=[LEVERAGE_REASON, INFO_REASON],
    )
    with pytest.raises(ValidationError, match="belongs to"):
        pipeline.waive_breach(
            cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
            decision_id=decline.decision_id,
        )
    # And a decline is not authority even on its own file: only
    # an approval can waive.
    from commercial_credit.covenant_testing import (
        validate_waiver_decision,
    )

    with pytest.raises(ValidationError, match="only an approval"):
        validate_waiver_decision(
            decision=decline, credit_file_id=o_file.credit_file_id
        )
    # The committee's own approval of this file authorises it.
    waived = pipeline.waive_breach(
        cf, bid, actor_id=CHAIR, actor_role=CHAIR_ROLE,
        decision_id=decision.decision_id,
        note="Waived under the committee's approval.",
    )
    assert waived.state == "waived"
    assert waived.events[-1].decision_id == decision.decision_id


def test_the_default_path_needs_review_or_escalation_and_a_code():
    pipeline, cf = approved_with_fy2026()
    pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    breach = dscr_breach(pipeline, cf)
    bid = breach["breach_id"]
    # Straight from open: refused — the default path is reached
    # through review or escalation, not from the queue.
    with pytest.raises(ValidationError, match="cannot move"):
        pipeline.default_breach(
            cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
            reason_code="cure_period_expired_uncured",
        )
    pipeline.review_breach(
        cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE
    )
    with pytest.raises(ValidationError, match="unknown breach"):
        pipeline.default_breach(
            cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
            reason_code="borrower_was_rude",
        )
    defaulted = pipeline.default_breach(
        cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE,
        reason_code="cure_period_expired_uncured",
    )
    assert defaulted.state == "defaulted_path"
    # Terminal: a defaulted breach cannot be waived afterwards.
    with pytest.raises(ValidationError, match="terminal"):
        pipeline.review_breach(
            cf, bid, actor_id=OFFICER, actor_role=OFFICER_ROLE
        )


def test_no_surface_deletes_or_closes_a_breach_silently():
    import inspect

    from commercial_credit.pipeline import CreditPipeline
    from commercial_credit.store import (
        InMemoryCreditStore,
        PostgresCreditStore,
        SqliteCreditStore,
    )

    for cls in (InMemoryCreditStore, SqliteCreditStore, PostgresCreditStore):
        names = [n for n in dir(cls) if "breach" in n]
        assert set(names) == {
            "save_breach_case", "get_breach_case", "list_breach_cases",
        }, (cls.__name__, names)
        assert not any(
            verb in n for n in names
            for verb in ("delete", "remove", "close", "purge")
        )
    fns = {
        n for n in dir(CreditPipeline)
        if "breach" in n and not n.startswith("_")
    }
    assert fns == {
        "list_breach_cases", "get_breach_case", "review_breach",
        "escalate_breach", "cure_breach", "waive_breach",
        "default_breach",
    }
    assert inspect.signature(
        CreditPipeline.waive_breach
    ).parameters["decision_id"]


# ---------------------------------------------------------------------
# Restated inputs: flag, re-test, original stands
# ---------------------------------------------------------------------


def test_a_restatement_flags_the_result_and_the_retest_supersedes():
    pipeline, cf = approved_with_fy2026()
    pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    breach = dscr_breach(pipeline, cf)
    original = next(
        v for v in results_for(pipeline, cf, metric_key="dscr")
        if v["outcome"] == "fail"
    )
    assert original["standing"] == "current"
    # The borrower restates FY2026: EBIT corrected upward.
    deliver_statements(pipeline, cf, 2026, FY2026_RESTATED_FIGURES)
    views = results_for(pipeline, cf, metric_key="dscr")
    flagged = next(v for v in views if v["result_id"] == original["result_id"])
    assert flagged["standing"] == "superseded_by_restatement"
    # The original record itself is untouched.
    assert Decimal(str(flagged["figure_value"])) == Decimal("0.9143")
    # The re-test runs against the restated figures and passes:
    # 440,000 / 350,000 = 1.2571.
    ran = pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 3))
    retest = next(
        e["result"] for e in ran["results"]
        if e["result"].metric_key == "dscr"
    )
    assert retest.is_retest
    assert retest.supersedes_result_id == original["result_id"]
    assert retest.outcome == "pass"
    assert retest.figure_value == Decimal("1.2571")
    # The re-test's pass can cure the breach the original opened.
    cured = pipeline.cure_breach(
        cf, breach["breach_id"], actor_id=OFFICER,
        actor_role=OFFICER_ROLE, cure_result_id=retest.result_id,
    )
    assert cured.state == "cured"
    # History keeps both results, the original still flagged.
    history = pipeline.covenant_history(
        cf, retest.covenant_id, as_of=date(2027, 8, 3)
    )
    standings = {
        v["result_id"]: v["standing"] for v in history["results"]
    }
    assert standings[original["result_id"]] == (
        "superseded_by_restatement"
    )
    assert standings[retest.result_id] == "current"


# ---------------------------------------------------------------------
# Tick integration, store round-trip, API
# ---------------------------------------------------------------------


def test_the_tick_runs_due_tests_and_opens_breaches():
    pipeline, cf = approved_with_fy2026()
    tick = pipeline.tick_covenants(
        as_of=date(2027, 8, 1), credit_file_id=cf
    )
    outcomes = {e["result"].outcome for e in tick["tests_recorded"]}
    assert "fail" in outcomes
    assert any(
        b["breach"].metric_key == "dscr" for b in tick["breaches_opened"]
    )
    # A second tick at the same date records nothing new.
    tick2 = pipeline.tick_covenants(
        as_of=date(2027, 8, 1), credit_file_id=cf
    )
    assert tick2["tests_recorded"] == []
    assert tick2["breaches_opened"] == []


def test_sqlite_round_trip_and_migration_registered(tmp_path):
    names = [p.name for p in migration_files(MIGRATIONS_DIR)]
    assert "0010_covenant_testing.sql" in names
    assert names == sorted(names)

    store = SqliteCreditStore(tmp_path / "credit.db")
    pipeline2 = CreditPipeline(
        store=store, documents=LocalDocumentStore()
    )
    credit_file, _f, _d, _s = approved_calloway(pipeline2)
    cf2 = credit_file.credit_file_id
    deliver_statements(pipeline2, cf2, 2026, FY2026_FIGURES)
    pipeline2.run_due_covenant_tests(cf2, as_of=date(2027, 8, 1))
    results = store.list_covenant_test_results(cf2)
    assert any(r.outcome == "fail" for r in results)
    breaches = store.list_breach_cases(cf2)
    assert breaches and all(b.events for b in breaches)
    one = store.get_covenant_test_result(results[0].result_id)
    assert one == results[0]
    # Insert-once: saving the same result again changes nothing.
    store.save_covenant_test_result(results[0])
    assert len(store.list_covenant_test_results(cf2)) == len(results)


def test_api_test_and_breach_surfaces(tmp_path, monkeypatch):
    store = InMemoryCreditStore()
    pipeline = CreditPipeline(
        store=store, documents=LocalDocumentStore()
    )
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "pipeline", pipeline)
    credit_file, _f, decision, _s = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    deliver_statements(pipeline, cf, 2026, FY2026_FIGURES)
    client = TestClient(api_module.app)

    ran = client.post(
        f"/applications/{cf}/covenant-tests/run",
        json={"as_of": "2027-08-01"},
    )
    assert ran.status_code == 200
    body = ran.json()
    assert any(r["outcome"] == "fail" for r in body["results"])
    assert body["breaches_opened"]

    results = client.get(f"/applications/{cf}/covenant-test-results")
    assert results.status_code == 200
    assert any(r["standing"] == "current" for r in results.json())

    breaches = client.get(
        "/breach-cases", params={"as_of": "2027-09-01"}
    )
    assert breaches.status_code == 200
    listed = breaches.json()
    assert listed and all("days_in_breach" in b for b in listed)
    bid = next(b for b in listed if b["metric_key"] == "dscr")["breach_id"]

    # A waiver without authority is a 422 over the API too.
    refused = client.post(
        f"/applications/{cf}/breach-cases/{bid}/waive",
        json={
            "actor_id": OFFICER, "actor_role": OFFICER_ROLE,
            "decision_id": "nope",
        },
    )
    assert refused.status_code == 422
    waived = client.post(
        f"/applications/{cf}/breach-cases/{bid}/waive",
        json={
            "actor_id": CHAIR, "actor_role": CHAIR_ROLE,
            "decision_id": decision.decision_id,
        },
    )
    assert waived.status_code == 201
    assert waived.json()["state"] == "waived"

    history = client.get(
        f"/applications/{cf}/covenants/"
        f"{waived.json()['covenant_id']}/history",
        params={"as_of": "2027-09-01"},
    )
    assert history.status_code == 200
    assert history.json()["results"] and history.json()["breaches"]

    missing = client.get(
        "/applications/cf-nope/covenant-test-results"
    )
    assert missing.status_code == 404


# ---------------------------------------------------------------------
# Round 18: the cure clock's edge — a breach is overdue only
# strictly AFTER its cure deadline; the deadline day itself is
# the last day of the cure period.
# ---------------------------------------------------------------------


def test_cure_clock_overdue_only_strictly_after_the_deadline():
    pipeline, cf = approved_with_fy2026()
    pipeline.run_due_covenant_tests(cf, as_of=date(2027, 8, 1))
    breach = dscr_breach(pipeline, cf)
    bid = breach["breach_id"]
    deadline = date.fromisoformat(breach["cure_deadline"])
    test_date = date.fromisoformat(breach["test_date"])
    assert deadline == test_date + timedelta(days=30)

    day_before = pipeline.get_breach_case(
        cf, bid, as_of=deadline - timedelta(days=1)
    )
    assert day_before["days_to_cure_deadline"] == 1
    assert day_before["cure_overdue"] is False

    on_the_day = pipeline.get_breach_case(cf, bid, as_of=deadline)
    assert on_the_day["days_to_cure_deadline"] == 0
    assert on_the_day["cure_overdue"] is False
    assert on_the_day["days_in_breach"] == 30

    day_after = pipeline.get_breach_case(
        cf, bid, as_of=deadline + timedelta(days=1)
    )
    assert day_after["days_to_cure_deadline"] == -1
    assert day_after["cure_overdue"] is True

    # A terminal breach never reads overdue, however late it is
    # viewed: the exemption is the state, not the date.
    cured_pipeline, cf2 = approved_with_fy2026()
    cured_pipeline.run_due_covenant_tests(cf2, as_of=date(2027, 8, 1))
    breach2 = dscr_breach(cured_pipeline, cf2)
    deliver_statements(cured_pipeline, cf2, 2027, FY2027_FIGURES)
    cured_pipeline.run_due_covenant_tests(cf2, as_of=date(2028, 8, 1))
    cure = next(
        v for v in results_for(cured_pipeline, cf2, metric_key="dscr")
        if v["fiscal_year"] == 2027 and v["outcome"] == "pass"
    )
    cured_pipeline.cure_breach(
        cf2, breach2["breach_id"], actor_id=OFFICER,
        actor_role=OFFICER_ROLE, cure_result_id=cure["result_id"],
    )
    late_view = cured_pipeline.get_breach_case(
        cf2, breach2["breach_id"], as_of=date(2029, 1, 1)
    )
    assert late_view["state"] == "cured"
    assert late_view["cure_overdue"] is False
