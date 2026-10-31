"""Round 19 — portfolio views.

The claims graded here:

  the figures     every section of the view is the fold it
                  claims to be — exposure summed by segment,
                  the book classified by each file's standing
                  (last-saved) policy report, the covenant
                  watch ordered by the cure clock, the staleness
                  census naming blocked files and their blocker
                  inputs, the adverse rollup counting each
                  memo's LATEST decision under the coded
                  vocabularies — and verify_portfolio_view
                  re-performs all of it and names any tamper
  the discipline  the builder runs over the caller's visible
                  book only: a file assigned to someone else
                  contributes nothing to a preparer's view, not
                  even to files_in_scope; a section whose family
                  the role does not hold (decisions, for the
                  preparer) is withheld under the coded reason
                  and the refusal is recorded as a denial
  the surface     GET /portfolio under the round 17 guards —
                  malformed callers refused by code, the system
                  posture reads the whole book

The book is five files: the seed Hartline file (blocked),
the committee-ready Calloway file, two bare applications in
other segments (blocked), and one file saved directly with no
artefacts yet — no policy report (not_evaluated) and an empty
checklist, which the readiness engine reads as ready: nothing
required, nothing missing. Policy reports, memos, decisions
and breach cases are saved as artefacts of record directly —
the fold reads the store, and the store is what is graded;
the workflows that mint those artefacts are graded in their
own rounds.
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.access import (
    Caller,
    visible_files,
)
from commercial_credit.committee import (
    ADVERSE_REASONS_VERSION,
    DEFERRAL_REASONS_VERSION,
)
from commercial_credit.errors import CreditError
from commercial_credit.models import (
    AdverseActionReason,
    Borrower,
    BorrowerSegment,
    BreachCase,
    CommitteeDecision,
    CreditFile,
    CreditMemo,
    FacilityRequest,
    FacilityType,
    MemoVerification,
    PolicyReport,
)
from commercial_credit.portfolio import (
    classify_policy_report,
    portfolio_view,
    verify_portfolio_view,
    withheld_sections,
)

from test_committee_gate import open_ready_file
from test_memo import hartline_pipeline, open_hartline

AS_OF = date(2025, 6, 30)
SYSTEM = Caller(caller_id="system", role="system")
OFFICER = Caller(caller_id="officer-1", role="credit_officer")
PREPARER = Caller(caller_id="preparer-1", role="analyst_preparer")


def _bare_file(pipeline, *, name, segment, amount, ein):
    return pipeline.open_application(
        Borrower(
            legal_name=name,
            ein_last4=ein,
            segment=segment,
            state_of_formation="OH",
            years_in_business=4,
            primary_contact_name="Contact",
            primary_contact_email="c@example.com",
        ),
        FacilityRequest(
            facility_type=FacilityType.TERM_LOAN,
            amount=Decimal(amount),
            purpose="working capital",
            term_months=36,
        ),
    )


def _policy_report(credit_file_id, report_id, summary):
    return PolicyReport(
        report_id=report_id,
        credit_file_id=credit_file_id,
        policy_pack_id="policy-pack",
        policy_version="v1",
        computation_pack_id="pack-1",
        as_of=AS_OF,
        outcomes=[],
        summary=summary,
    )


def _memo(credit_file_id, memo_id):
    return CreditMemo(
        memo_id=memo_id,
        credit_file_id=credit_file_id,
        borrower_name="Borrower",
        facility_type="term_loan",
        spread_id="spread-1",
        computation_pack_id="pack-1",
        policy_pack_id="policy-pack",
        policy_version="v1",
        policy_report_id="policy-1",
        as_of=AS_OF,
        composer_profile="offline",
        verification=MemoVerification(
            composer_profile="offline", figure_count=0
        ),
    )


def _decision(credit_file_id, memo_id, decision_id, outcome, **kw):
    return CommitteeDecision(
        decision_id=decision_id,
        credit_file_id=credit_file_id,
        memo_id=memo_id,
        outcome=outcome,
        quorum_required=3,
        members=[],
        recorded_by="officer-1",
        recorded_by_role="credit_officer",
        **kw,
    )


def _breach(credit_file_id, breach_id, borrower, *, state, test_date,
            cure_deadline):
    return BreachCase(
        breach_id=breach_id,
        credit_file_id=credit_file_id,
        borrower_name=borrower,
        set_id="set-1",
        covenant_id="cov-1",
        covenant_version=1,
        covenant_name="Maximum leverage",
        metric_key="leverage",
        operator="<=",
        threshold=Decimal("2.00"),
        test_result_id=f"result-{breach_id}",
        instance_id=f"inst-{breach_id}",
        test_date=test_date,
        period_label="Q1",
        cure_deadline=cure_deadline,
        state=state,
    )


@pytest.fixture()
def book():
    pipeline = hartline_pipeline()
    hartline = open_hartline(pipeline)
    calloway = open_ready_file(pipeline)
    retail = _bare_file(
        pipeline, name="Retail Row LLC", segment=BorrowerSegment.RETAIL,
        amount="500000", ein="3333",
    )
    health = _bare_file(
        pipeline, name="Health Way LLC",
        segment=BorrowerSegment.HEALTHCARE, amount="750000", ein="4444",
    )
    # Saved directly, before any workflow runs over it: no
    # checklist, no spread, no policy report — the book's
    # not_evaluated file. (A pipeline-opened file would carry
    # the intake run's own policy report, so the bare-open
    # route cannot make this case.)
    estate = CreditFile(
        credit_file_id="cf-estate-hold",
        borrower=Borrower(
            legal_name="Estate Hold LLC",
            ein_last4="5555",
            segment=BorrowerSegment.REAL_ESTATE,
            state_of_formation="OH",
            years_in_business=6,
            primary_contact_name="Contact",
            primary_contact_email="c@example.com",
        ),
        facility=FacilityRequest(
            facility_type=FacilityType.TERM_LOAN,
            amount=Decimal("250000"),
            purpose="acquisition",
            term_months=36,
        ),
    )
    pipeline.store.save_credit_file(estate)
    store = pipeline.store
    hid, cid = hartline.credit_file_id, calloway.credit_file_id
    rid, eid = retail.credit_file_id, estate.credit_file_id
    # Policy reports: Hartline's standing report is the LAST
    # saved — a passing report saved first must not shadow the
    # blocker-failing one saved after it.
    store.save_policy_report(_policy_report(
        hid, "rep-h-pass", {"pass": 8, "fail": 0, "not_evaluable": 0,
                            "blocker_failures": 0}))
    store.save_policy_report(_policy_report(
        hid, "rep-h-blocker", {"pass": 6, "fail": 1, "not_evaluable": 0,
                               "blocker_failures": 1}))
    store.save_policy_report(_policy_report(
        cid, "rep-c-pass", {"pass": 8, "fail": 0, "not_evaluable": 0,
                            "blocker_failures": 0}))
    store.save_policy_report(_policy_report(
        rid, "rep-r-fail", {"pass": 6, "fail": 2, "not_evaluable": 0,
                            "blocker_failures": 0}))
    store.save_policy_report(_policy_report(
        health.credit_file_id, "rep-he-nfe",
        {"pass": 5, "fail": 0, "not_evaluable": 3, "blocker_failures": 0}))
    # estate: no report at all — not_evaluated.
    # Committee decisions: memo-1 deferred then approved on
    # re-presentment (only the latest counts); memo-2 declined
    # with two coded reasons; memo-3 declined on one shared
    # code; memo-4 deferred and still standing.
    for memo_id, fid in (("memo-1", hid), ("memo-2", hid),
                         ("memo-3", cid), ("memo-4", cid)):
        store.save_memo(_memo(fid, memo_id))
    store.save_committee_decision(_decision(
        hid, "memo-1", "dec-1a", "defer",
        deferral_reasons=["awaiting_required_documents",
                          "awaiting_updated_financials"],
        deferral_vocabulary=DEFERRAL_REASONS_VERSION,
        representment_requirements=["updated financials"],
    ))
    store.save_committee_decision(_decision(
        hid, "memo-1", "dec-1b", "approve"))
    store.save_committee_decision(_decision(
        hid, "memo-2", "dec-2", "decline",
        adverse_reasons=[
            AdverseActionReason(
                code="debt_service_coverage_below_policy",
                tie_kind="file_fact", checklist_item_id="chk-01"),
            AdverseActionReason(
                code="leverage_above_policy_limit",
                tie_kind="file_fact", checklist_item_id="chk-02"),
        ],
        reason_vocabulary=ADVERSE_REASONS_VERSION,
    ))
    store.save_committee_decision(_decision(
        cid, "memo-3", "dec-3", "decline",
        adverse_reasons=[
            AdverseActionReason(
                code="debt_service_coverage_below_policy",
                tie_kind="file_fact", checklist_item_id="chk-01"),
        ],
        reason_vocabulary=ADVERSE_REASONS_VERSION,
    ))
    store.save_committee_decision(_decision(
        cid, "memo-4", "dec-4", "defer",
        deferral_reasons=["awaiting_appraisal_or_valuation"],
        deferral_vocabulary=DEFERRAL_REASONS_VERSION,
        representment_requirements=["appraisal"],
    ))
    # Breach cases: the watch orders by cure deadline — the
    # overdue case first, then the nearest clock, then the
    # later one; the cured case is census-only.
    store.save_breach_case(_breach(
        hid, "br-1", "Hartline", state="open",
        test_date=date(2025, 3, 31), cure_deadline=date(2025, 5, 30)))
    store.save_breach_case(_breach(
        cid, "br-2", "Calloway", state="escalated",
        test_date=date(2025, 6, 15), cure_deadline=date(2025, 7, 15)))
    store.save_breach_case(_breach(
        hid, "br-3", "Hartline", state="open",
        test_date=date(2025, 6, 1), cure_deadline=date(2025, 7, 1)))
    store.save_breach_case(_breach(
        cid, "br-4", "Calloway", state="cured",
        test_date=date(2025, 1, 31), cure_deadline=date(2025, 3, 2)))
    files = {
        "hartline": hartline, "calloway": calloway, "retail": retail,
        "health": health, "estate": estate,
    }
    return pipeline, files


def _view(pipeline, caller=SYSTEM):
    files = visible_files(caller, pipeline.store.list_credit_files())
    return portfolio_view(pipeline, files, caller=caller, as_of=AS_OF)


# -- the figures ----------------------------------------------------


def test_exposure_by_segment_sums_the_book(book):
    pipeline, _ = book
    view = _view(pipeline)
    assert view["files_in_scope"] == 5
    assert view["scope"] == "whole_book"
    section = view["sections"]["exposure_by_segment"]
    rows = {r["segment"]: r for r in section["by_segment"]}
    assert rows["manufacturing"] == {
        "segment": "manufacturing", "files": 2,
        "facility_amount": "3000000",
    }
    assert rows["retail"]["facility_amount"] == "500000"
    assert rows["healthcare"]["facility_amount"] == "750000"
    assert rows["real_estate"]["facility_amount"] == "250000"
    assert section["total"] == {"files": 5, "facility_amount": "4500000"}
    # Largest exposure first.
    assert section["by_segment"][0]["segment"] == "manufacturing"


def test_exposure_by_policy_outcome_uses_the_standing_report(book):
    pipeline, _ = book
    section = _view(pipeline)["sections"]["exposure_by_policy_outcome"]
    rows = {r["outcome"]: r for r in section["by_outcome"]}
    assert rows["blocker_fail"]["files"] == 1  # Hartline, last saved
    assert rows["blocker_fail"]["facility_amount"] == "1500000"
    assert rows["pass"]["files"] == 1
    assert rows["fail"]["files"] == 1
    assert rows["not_fully_evaluable"]["files"] == 1
    assert rows["not_evaluated"]["files"] == 1
    assert rows["not_evaluated"]["facility_amount"] == "250000"
    # Every bucket named, in the stated order, zeros included.
    assert [r["outcome"] for r in section["by_outcome"]] == [
        "pass", "fail", "blocker_fail", "not_fully_evaluable",
        "not_evaluated",
    ]


def test_classify_policy_report_reads_the_summary_only():
    assert classify_policy_report(None) == "not_evaluated"
    report = _policy_report("cf-x", "r-1", {"pass": 8})
    assert classify_policy_report(report) == "pass"
    report.summary = {"pass": 5, "not_evaluable": 2}
    assert classify_policy_report(report) == "not_fully_evaluable"
    report.summary = {"pass": 5, "fail": 1, "not_evaluable": 2}
    assert classify_policy_report(report) == "fail"
    report.summary = {"fail": 1, "blocker_failures": 1}
    assert classify_policy_report(report) == "blocker_fail"


def test_covenant_watch_orders_by_the_cure_clock(book):
    pipeline, _ = book
    section = _view(pipeline)["sections"]["covenant_watch"]
    assert [c["breach_id"] for c in section["open"]] == [
        "br-1", "br-3", "br-2",
    ]
    first = section["open"][0]
    assert first["cure_overdue"] is True
    assert first["days_to_cure_deadline"] == -31
    assert section["open"][1]["days_to_cure_deadline"] == 1
    assert section["open"][2]["days_to_cure_deadline"] == 15
    assert section["open_count"] == 3
    assert section["overdue_count"] == 1
    assert {r["state"]: r["cases"] for r in section["by_state"]} == {
        "cured": 1, "escalated": 1, "open": 2,
    }


def test_staleness_census_names_blocked_files_and_inputs(book):
    pipeline, files = book
    section = _view(pipeline)["sections"]["staleness_census"]
    # Calloway is ready; so is the direct-saved file, whose
    # empty checklist requires nothing (the readiness engine's
    # own semantics — the census reports what the engine says).
    assert section["ready_files"] == 2
    assert section["not_computable_files"] == 0
    assert section["blocked_files"] == 3
    blocked_ids = {row["credit_file_id"] for row in section["blocked"]}
    assert files["calloway"].credit_file_id not in blocked_ids
    assert files["hartline"].credit_file_id in blocked_ids
    hartline_row = next(
        row for row in section["blocked"]
        if row["credit_file_id"] == files["hartline"].credit_file_id
    )
    assert hartline_row["blockers"]
    assert all(
        set(b) == {"input_key", "state"} for b in hartline_row["blockers"]
    )
    # The histogram accounts for every blocker on every blocked
    # file, and its file counts never exceed the blocked count.
    total_occurrences = sum(
        len(row["blockers"]) for row in section["blocked"]
    )
    assert sum(
        r["occurrences"] for r in section["blockers_by_input"]
    ) == total_occurrences
    assert all(
        r["files"] <= section["blocked_files"]
        for r in section["blockers_by_input"]
    )


def test_staleness_census_names_a_file_that_cannot_compute(book):
    pipeline, files = book
    target = files["estate"].credit_file_id
    original = pipeline.compute_readiness

    def raising(credit_file_id, **kw):
        if credit_file_id == target:
            raise CreditError("no computation pack stands")
        return original(credit_file_id, **kw)

    pipeline.compute_readiness = raising
    try:
        section = _view(pipeline)["sections"]["staleness_census"]
    finally:
        pipeline.compute_readiness = original
    assert section["not_computable"] == [target]
    assert section["not_computable_files"] == 1
    assert section["blocked_files"] == 3
    assert section["ready_files"] == 1


def test_adverse_rollup_counts_latest_decisions_by_code(book):
    pipeline, _ = book
    section = _view(pipeline)["sections"]["adverse_rollup"]
    assert section["decided_memos"] == 4
    assert {r["outcome"]: r["decisions"]
            for r in section["by_outcome"]} == {
        "approve": 1, "approve_with_conditions": 0,
        "decline": 2, "defer": 1,
    }
    # memo-1's deferral was superseded by its approval: its
    # codes appear nowhere in the rollup.
    assert section["declines_by_reason_code"] == [
        {"code": "debt_service_coverage_below_policy", "decisions": 2},
        {"code": "leverage_above_policy_limit", "decisions": 1},
    ]
    assert section["deferrals_by_reason_code"] == [
        {"code": "awaiting_appraisal_or_valuation", "decisions": 1},
    ]
    assert section["vocabularies"] == {
        "adverse_reasons": ADVERSE_REASONS_VERSION,
        "deferral_reasons": DEFERRAL_REASONS_VERSION,
    }


# -- the verifier ---------------------------------------------------


def test_verifier_re_performs_every_section(book):
    pipeline, _ = book
    for caller in (SYSTEM, OFFICER):
        files = visible_files(caller, pipeline.store.list_credit_files())
        view = portfolio_view(pipeline, files, caller=caller, as_of=AS_OF)
        assert verify_portfolio_view(
            view, pipeline, files, caller=caller, as_of=AS_OF
        ) == []


def test_verifier_names_a_tampered_figure(book):
    pipeline, _ = book
    files = visible_files(SYSTEM, pipeline.store.list_credit_files())
    view = portfolio_view(pipeline, files, caller=SYSTEM, as_of=AS_OF)
    tampered = copy.deepcopy(view)
    tampered["sections"]["exposure_by_segment"]["total"][
        "facility_amount"
    ] = "1"
    problems = verify_portfolio_view(
        tampered, pipeline, files, caller=SYSTEM, as_of=AS_OF
    )
    assert any("exposure_by_segment" in p for p in problems)
    tampered = copy.deepcopy(view)
    tampered["sections"]["covenant_watch"]["open_count"] = 0
    problems = verify_portfolio_view(
        tampered, pipeline, files, caller=SYSTEM, as_of=AS_OF
    )
    assert any("covenant_watch" in p for p in problems)


# -- the access discipline ------------------------------------------


def test_preparer_sees_only_their_book_and_no_decisions(book):
    pipeline, files = book
    pipeline.assign_file(
        files["hartline"].credit_file_id,
        actor="officer-1", actor_role="credit_officer",
        assignee="someone-else",
    )
    pipeline.assign_file(
        files["retail"].credit_file_id,
        actor="officer-1", actor_role="credit_officer",
        assignee="preparer-1",
    )
    view = _view(pipeline, PREPARER)
    assert view["scope"] == "assigned"
    # retail (theirs) + the three unassigned files; Hartline,
    # assigned elsewhere, contributes nothing — not even here.
    assert view["files_in_scope"] == 4
    segments = {
        r["segment"]: r
        for r in view["sections"]["exposure_by_segment"]["by_segment"]
    }
    assert segments["manufacturing"]["files"] == 1  # Calloway only
    assert segments["manufacturing"]["facility_amount"] == "1500000"
    assert view["sections"]["exposure_by_segment"]["total"] == {
        "files": 4, "facility_amount": "3000000",
    }
    # decisions is a family the preparer does not hold.
    assert view["sections"]["adverse_rollup"] == {
        "withheld": True, "family": "decisions",
        "reason_code": "role_not_permitted",
    }
    assert withheld_sections(PREPARER) == [
        {"section": "adverse_rollup", "family": "decisions",
         "reason_code": "role_not_permitted"},
    ]
    assert withheld_sections(OFFICER) == []
    # The withheld view still verifies: the re-performance
    # grades the withholding itself.
    visible = visible_files(PREPARER, pipeline.store.list_credit_files())
    assert verify_portfolio_view(
        view, pipeline, visible, caller=PREPARER, as_of=AS_OF
    ) == []


# -- the surface ------------------------------------------------------


@pytest.fixture()
def client(book):
    pipeline, files = book
    old_pipeline, old_store = api_module.pipeline, api_module.store
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    try:
        yield TestClient(api_module.app), pipeline, files
    finally:
        api_module.pipeline = old_pipeline
        api_module.store = old_store


def test_portfolio_route_serves_the_whole_book_by_default(client):
    http, pipeline, _ = client
    response = http.get("/portfolio", params={"as_of": "2025-06-30"})
    assert response.status_code == 200
    body = response.json()
    assert body["files_in_scope"] == 5
    assert body["sections"]["adverse_rollup"]["decided_memos"] == 4


def test_portfolio_route_withholds_and_records_for_the_preparer(client):
    http, pipeline, files = client
    pipeline.assign_file(
        files["hartline"].credit_file_id,
        actor="officer-1", actor_role="credit_officer",
        assignee="someone-else",
    )
    headers = {
        "x-caller": "preparer-1", "x-caller-role": "analyst_preparer",
    }
    response = http.get(
        "/portfolio", params={"as_of": "2025-06-30"}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["files_in_scope"] == 4
    assert body["sections"]["adverse_rollup"]["withheld"] is True
    denials = [
        d for d in pipeline.store.list_denials()
        if d.family == "decisions" and d.credit_file_id is None
    ]
    assert len(denials) == 1
    assert denials[0].reason_code == "role_not_permitted"
    assert denials[0].surface == "/portfolio"


def test_portfolio_route_refuses_malformed_callers_by_code(client):
    http, _, _ = client
    response = http.get(
        "/portfolio", params={"as_of": "2025-06-30"},
        headers={"x-caller": "x", "x-caller-role": "superuser"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "unknown_role"
    response = http.get(
        "/portfolio", params={"as_of": "2025-06-30"},
        headers={"x-caller-role": "credit_officer"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "caller_required"
