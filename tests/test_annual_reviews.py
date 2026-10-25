"""Round 12 — annual review packs.

An approved facility owes the bank an annual review. These
tests pin:

  the clock     review instances derive from the covenant set's
                anchor — closing date, or the approval date when
                no closing is recorded — on the month lattice:
                leap-day closings are reviewed on Feb 28 in
                common years and Feb 29 in leap years; states run
                scheduled / due_soon / due / overdue, and an
                outcome closes the instance while the next derives
                from the anchor, unmoved
  the pack      assembled in code from stored artefacts only —
                fresh spread + computation pack through the
                existing engines, the period's covenant history
                with standing, its breaches, the policy report
                under the pinned version AND the current one
                (differences named, never re-based), readiness
                recomputed at the date; content-addressed, so a
                re-assembly over the same inputs is the same pack
  honesty       a pack whose inputs are stale is produced marked
                incomplete, naming every blocker — never blessed
                complete; the summary's locked facts stand
                verbatim, every figure traces, and prose that
                denies the record (a breach period called
                breach-free) or invents a figure is refused
  the stance    a named credit officer, coded reasons from the
                versioned vocabulary, conditions through the
                round 7 machinery; the memo's approving officer
                may not review alone (a countersigning officer is
                required); an instance closes exactly once; an
                escalation mints the committee referral it names
  the surfaces  the review queue, the scheduler opening due
                reviews idempotently, the tick carrying them, the
                API, the SQLite round-trip, migration 0011

Fixtures ride the committee-ready Calloway file (closing
2026-07-01, term 60 months, memo pinned to policy v1). FY2026
is the failing year from round 11: DSCR 0.9143 against the 1.25
floor, leverage 4.4348 against the 2.50 ceiling.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.covenants import maturity_date
from commercial_credit.db import migration_files
from commercial_credit.documents import LocalDocumentStore, register_document
from commercial_credit.errors import (
    MemoVerificationError,
    NotFoundError,
    ValidationError,
)
from commercial_credit.models import CommitteeCondition, DocumentKind
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.reviews import review_due_dates
from commercial_credit.store import InMemoryCreditStore, SqliteCreditStore

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
)
from test_covenant_testing import FY2026_FIGURES, statement_document
from test_covenants import CLOSING, approved_calloway

SECOND_OFFICER = "officer-mateo-reyes"
SECOND_OFFICER_ROLE = "senior_credit_officer"


def deliver_statements(pipe, cf, year, figures):
    stored = pipe.store.get_credit_file(cf)
    register_document(
        pipe.store, pipe.documents, stored,
        kind=DocumentKind.FINANCIAL_STATEMENT,
        filename=f"calloway-fy{year}-statements.json",
        data=statement_document(year, figures),
        period_label=f"FY{year}",
    )
    pipe.run_extraction(cf)


def breach_year_pipeline():
    """Calloway approved, FY2026 (the failing year) delivered,
    its due covenant tests run at 2027-03-01: breaches open, and
    every receipt-aged input still inside its freshness limit,
    so the review 1 pack assembles complete."""
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    deliver_statements(pipeline, cf, 2026, FY2026_FIGURES)
    pipeline.run_due_covenant_tests(cf, as_of=date(2027, 3, 1))
    return pipeline, credit_file, final, decision, covenant_set


# ---------------------------------------------------------------------
# The clock: derivation, leap years, states, completion
# ---------------------------------------------------------------------


def test_review_due_dates_walk_anniversaries_on_the_lattice():
    anchor = date(2026, 7, 1)
    assert review_due_dates(
        anchor, maturity=maturity_date(anchor, 60)
    ) == [
        date(2027, 7, 1), date(2028, 7, 1), date(2029, 7, 1),
        date(2030, 7, 1), date(2031, 7, 1),
    ]
    # A facility that matures inside its first year owes none.
    assert review_due_dates(
        anchor, maturity=maturity_date(anchor, 9)
    ) == []


def test_review_due_dates_grade_leap_year_anniversaries():
    # Closed on a leap day: reviewed Feb 28 in common years,
    # Feb 29 in leap years — never Mar 1, never skipped.
    anchor = date(2024, 2, 29)
    assert review_due_dates(
        anchor, maturity=maturity_date(anchor, 60)
    ) == [
        date(2025, 2, 28), date(2026, 2, 28), date(2027, 2, 28),
        date(2028, 2, 29), date(2029, 2, 28),
    ]


def test_a_leap_day_closing_anchors_leap_aware_reviews():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf = credit_file.credit_file_id
    decide(pipeline, cf, final.memo_id, outcome="approve")
    pipeline.record_closing(
        cf, closing_date=date(2024, 2, 29),
        actor=OFFICER, actor_role=OFFICER_ROLE,
    )
    schedule = pipeline.annual_review_schedule(cf, as_of=date(2025, 1, 1))
    view = schedule["sets"][0]
    assert view["anchor_basis"] == "closing_date"
    assert [i.due_date for i in view["instances"]] == [
        date(2025, 2, 28), date(2026, 2, 28), date(2027, 2, 28),
        date(2028, 2, 29), date(2029, 2, 28),
    ]


def test_the_anchor_falls_back_to_the_approval_date_named():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(
        pipeline, closing=None
    )
    cf = credit_file.credit_file_id
    schedule = pipeline.annual_review_schedule(cf, as_of=date(2027, 1, 1))
    view = schedule["sets"][0]
    assert view["anchor_basis"] == "approval_date"
    assert view["anchor_date"] == decision.decided_at.date()
    assert view["instances"][0].due_date == date(
        decision.decided_at.year + 1,
        decision.decided_at.month,
        decision.decided_at.day,
    )


def test_instance_states_run_the_calendar():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id

    def first_state(as_of):
        schedule = pipeline.annual_review_schedule(cf, as_of=as_of)
        return schedule["sets"][0]["instances"][0].state

    assert first_state(date(2027, 5, 15)) == "scheduled"
    assert first_state(date(2027, 6, 15)) == "due_soon"
    assert first_state(date(2027, 7, 1)) == "due"
    assert first_state(date(2027, 7, 2)) == "overdue"


def test_an_outcome_closes_the_instance_and_the_next_derives():
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )
    pipeline.record_review_outcome(
        cf, review_number=1, stance="place_on_watchlist",
        reason_codes=["covenant_breach_in_period"],
        reviewer_id=OFFICER, reviewer_role=OFFICER_ROLE,
        countersigner_id=SECOND_OFFICER,
        countersigner_role=SECOND_OFFICER_ROLE,
        pack_id=pack.pack_id,
    )
    schedule = pipeline.annual_review_schedule(cf, as_of=date(2027, 3, 2))
    first, second = schedule["sets"][0]["instances"][:2]
    assert first.state == "completed"
    assert first.outcome_id is not None
    assert first.pack_id == pack.pack_id
    # The next review derives from the anchor, unmoved by the
    # first one's timing: window (anchor, due 1], then (due 1, due 2].
    assert second.state == "scheduled"
    assert second.period_start == first.due_date == date(2027, 7, 1)
    assert second.due_date == date(2028, 7, 1)


# ---------------------------------------------------------------------
# The pack: pins, both policy versions, honesty
# ---------------------------------------------------------------------


def test_the_pack_names_both_policy_versions_and_their_differences():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    # No FY2026 statements: the freshest spread is FY2025, whose
    # DSCR of 1.3125 passes v1's 1.25 floor and fails v2's 1.35.
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 7, 1)
    )
    assert pack.pinned_policy_version == "v1"
    assert pack.current_policy_version == "v2"
    assert pack.pinned_policy_report_id != pack.current_policy_report_id
    differences = {
        d.test_id: (d.pinned_outcome, d.current_outcome)
        for d in pack.policy_differences
    }
    assert differences["min_dscr"] == ("pass", "fail")
    # The decided memo's own report is untouched by the re-runs:
    # the pack READ both versions, it re-based nothing.
    memo = pipeline.get_memo(cf, pack.memo_id)
    assert memo.policy_version == "v1"
    assert pack.spread_id and pack.computation_pack_id
    assert pack.latest_period == 2025


def test_a_stale_pack_is_produced_incomplete_naming_its_blockers():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 7, 1)
    )
    assert pack.complete is False
    assert pack.blockers, "a stale pack names its blockers"
    keys = {b.input_key for b in pack.blockers}
    assert "chk-01" in keys  # the fiscal statements, aged out
    incomplete = next(
        p for p in pack.summary.locked_facts
        if p.startswith("INCOMPLETE PACK")
    )
    for blocker in pack.blockers:
        assert f"`{blocker.input_key}`" in incomplete
    # And the readiness report the pack pins says the same.
    readiness = pipeline.get_readiness_report(cf, pack.readiness_report_id)
    assert readiness.blocker_count == len(pack.blockers)


def test_the_breach_year_pack_carries_the_periods_record():
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )
    assert pack.complete is True
    assert pack.blockers == []
    assert pack.latest_period == 2026
    results = pipeline.covenant_test_results(cf)
    dscr_fail = next(
        r for r in results
        if r["metric_key"] == "dscr" and r["outcome"] == "fail"
    )
    assert dscr_fail["result_id"] in pack.test_result_ids
    breaches = pipeline.list_breach_cases(
        as_of=date(2027, 3, 1), credit_file_id=cf
    )
    assert breaches and all(
        b["breach_id"] in pack.breach_ids for b in breaches
    )
    assert set(pack.open_breach_ids) == set(pack.breach_ids)
    locked = "\n".join(pack.summary.locked_facts)
    assert "FAILED COVENANT TEST" in locked
    assert "0.9143×" in locked  # the graded FY2026 DSCR
    assert "BREACH:" in locked
    assert pack.summary.verification.verified is True
    assert pack.summary.verification.figure_count > 0
    assert (
        pack.summary.verification.locked_facts_present
        == pack.summary.verification.locked_facts_expected
        == len(pack.summary.locked_facts)
    )


def test_pack_assembly_is_content_addressed():
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    first = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )
    second = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )
    assert second.pack_id == first.pack_id
    assert len(pipeline.list_review_packs(cf)) == 1


def test_the_packs_own_summary_reverifies_and_tampering_is_refused():
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )
    summary = pack.summary
    # The stored summary passes its own check.
    verification = pipeline.check_review_summary(
        cf, pack.pack_id,
        paragraphs=summary.paragraphs,
        locked_facts=summary.locked_facts,
    )
    assert verification.verified is True
    # An invented figure is refused by name.
    with pytest.raises(MemoVerificationError):
        pipeline.check_review_summary(
            cf, pack.pack_id,
            paragraphs=[
                *summary.paragraphs,
                "The exposure now totals $999,999.99.",
            ],
            locked_facts=summary.locked_facts,
        )
    # A dropped locked fact — the failed test — is refused.
    softened = [
        p for p in summary.locked_facts
        if not p.startswith("FAILED COVENANT TEST")
    ]
    assert len(softened) < len(summary.locked_facts)
    with pytest.raises(MemoVerificationError):
        pipeline.check_review_summary(
            cf, pack.pack_id,
            paragraphs=summary.paragraphs,
            locked_facts=softened,
        )
    # Prose denying the breach record is refused outright.
    with pytest.raises(MemoVerificationError):
        pipeline.check_review_summary(
            cf, pack.pack_id,
            paragraphs=[
                *summary.paragraphs,
                "There were no breaches in the period.",
            ],
            locked_facts=summary.locked_facts,
        )


# ---------------------------------------------------------------------
# The stance: discipline, countersignature, referral, one close
# ---------------------------------------------------------------------


def test_outcome_refusals_name_themselves():
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )

    def record(**overrides):
        kwargs = dict(
            review_number=1, stance="affirm",
            reason_codes=["performance_in_line"],
            reviewer_id=SECOND_OFFICER,
            reviewer_role=SECOND_OFFICER_ROLE,
            pack_id=pack.pack_id,
        )
        kwargs.update(overrides)
        return pipeline.record_review_outcome(cf, **kwargs)

    with pytest.raises(ValidationError):
        record(reviewer_id="")  # anonymous
    with pytest.raises(ValidationError):
        record(reviewer_id=RM, reviewer_role=RM_ROLE)  # not an officer
    with pytest.raises(ValidationError):
        record(stance="shrug")  # unknown stance
    with pytest.raises(ValidationError):
        record(reason_codes=[])  # a free-text stance
    with pytest.raises(ValidationError):
        record(reason_codes=["felt_fine"])  # unknown code
    with pytest.raises(ValidationError):
        record(reason_codes=["other"])  # 'other' without a note
    with pytest.raises(ValidationError):
        record(conditions=[CommitteeCondition(
            condition_id="", type="other", note="watch it",
            due_cadence="annual",
        )])  # conditions belong to affirm_with_conditions
    # Nothing landed through any refusal.
    assert pipeline.list_review_outcomes(cf) == []


def test_the_memo_approver_may_not_review_alone():
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )
    assert final.reviewed_by == OFFICER
    with pytest.raises(ValidationError, match="countersign"):
        pipeline.record_review_outcome(
            cf, review_number=1, stance="affirm",
            reason_codes=["performance_in_line"],
            reviewer_id=OFFICER, reviewer_role=OFFICER_ROLE,
            pack_id=pack.pack_id,
        )
    with pytest.raises(ValidationError, match="different officer"):
        pipeline.record_review_outcome(
            cf, review_number=1, stance="affirm",
            reason_codes=["performance_in_line"],
            reviewer_id=OFFICER, reviewer_role=OFFICER_ROLE,
            countersigner_id=OFFICER, countersigner_role=OFFICER_ROLE,
            pack_id=pack.pack_id,
        )
    outcome = pipeline.record_review_outcome(
        cf, review_number=1, stance="affirm",
        reason_codes=["performance_in_line"],
        reviewer_id=OFFICER, reviewer_role=OFFICER_ROLE,
        countersigner_id=SECOND_OFFICER,
        countersigner_role=SECOND_OFFICER_ROLE,
        pack_id=pack.pack_id,
    )
    assert outcome.countersigner_id == SECOND_OFFICER
    assert outcome.referral_id is None


def test_affirm_with_conditions_traces_its_conditions():
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )
    with pytest.raises(ValidationError):
        pipeline.record_review_outcome(
            cf, review_number=1, stance="affirm_with_conditions",
            reason_codes=["covenant_breach_in_period"],
            reviewer_id=SECOND_OFFICER,
            reviewer_role=SECOND_OFFICER_ROLE,
            pack_id=pack.pack_id,
        )
    outcome = pipeline.record_review_outcome(
        cf, review_number=1, stance="affirm_with_conditions",
        reason_codes=["covenant_breach_in_period"],
        reviewer_id=SECOND_OFFICER, reviewer_role=SECOND_OFFICER_ROLE,
        pack_id=pack.pack_id,
        conditions=[CommitteeCondition(
            condition_id="", type="financial_covenant",
            metric_key="dscr", operator=">=",
            threshold=Decimal("1.25"), due_cadence="annual",
        )],
    )
    assert len(outcome.conditions) == 1
    condition = outcome.conditions[0]
    assert condition.condition_id == "cond-1"
    assert condition.threshold_trace  # traced at the gate, as in R7
    # An invented threshold does not trace, and refuses the act.
    pipeline2, cf2_file, *_ = breach_year_pipeline()
    cf2 = cf2_file.credit_file_id
    pack2 = pipeline2.assemble_review_pack(
        cf2, review_number=1, as_of=date(2027, 3, 1)
    )
    with pytest.raises(ValidationError):
        pipeline2.record_review_outcome(
            cf2, review_number=1, stance="affirm_with_conditions",
            reason_codes=["covenant_breach_in_period"],
            reviewer_id=SECOND_OFFICER,
            reviewer_role=SECOND_OFFICER_ROLE,
            pack_id=pack2.pack_id,
            conditions=[CommitteeCondition(
                condition_id="", type="financial_covenant",
                metric_key="dscr", operator=">=",
                threshold=Decimal("1.37"), due_cadence="annual",
            )],
        )


def test_an_instance_closes_exactly_once():
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    pipeline.record_review_outcome(
        cf, review_number=1, stance="affirm",
        reason_codes=["performance_in_line"],
        reviewer_id=SECOND_OFFICER, reviewer_role=SECOND_OFFICER_ROLE,
        as_of=date(2027, 3, 1),
    )
    with pytest.raises(ValidationError, match="closes exactly once"):
        pipeline.record_review_outcome(
            cf, review_number=1, stance="place_on_watchlist",
            reason_codes=["covenant_breach_in_period"],
            reviewer_id=SECOND_OFFICER,
            reviewer_role=SECOND_OFFICER_ROLE,
            as_of=date(2027, 3, 2),
        )
    assert len(pipeline.list_review_outcomes(cf)) == 1


def test_escalation_mints_the_referral_it_names():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 7, 1)
    )
    outcome = pipeline.record_review_outcome(
        cf, review_number=1, stance="escalate_to_committee",
        reason_codes=["policy_exception_open"],
        reviewer_id=SECOND_OFFICER, reviewer_role=SECOND_OFFICER_ROLE,
        pack_id=pack.pack_id,
    )
    assert outcome.referral_id is not None
    referrals = pipeline.list_committee_referrals(cf)
    assert len(referrals) == 1
    referral = referrals[0]
    assert referral.referral_id == outcome.referral_id
    assert referral.outcome_id == outcome.outcome_id
    assert referral.pack_id == pack.pack_id
    # The referral names the memo the round 7 machinery acts on.
    assert referral.memo_id == final.memo_id == covenant_set.memo_id
    assert referral.status == "open"
    assert referral.reason_codes == ["policy_exception_open"]


# ---------------------------------------------------------------------
# Queue, scheduler, store, API
# ---------------------------------------------------------------------


def test_the_review_queue_and_the_scheduler_opening():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    assert pipeline.review_due_work(as_of=date(2026, 12, 15)) == []
    queue = pipeline.review_due_work(as_of=date(2027, 6, 15))
    assert len(queue) == 1
    assert queue[0]["instance"].review_number == 1
    assert queue[0]["instance"].state == "due_soon"
    # The covenant queue surface carries the review too.
    combined = pipeline.covenant_due_work(as_of=date(2027, 7, 1))
    assert any(
        entry.get("kind") == "annual_review"
        and entry["review_instance"].review_number == 1
        for entry in combined
    )
    opened = pipeline.open_due_reviews(as_of=date(2027, 7, 1))
    assert len(opened["reviews_opened"]) == 1
    assert opened["reviews_opened"][0]["pack_id"]
    # Opened is opened: the scheduler does not open it twice.
    assert pipeline.open_due_reviews(
        as_of=date(2027, 7, 2)
    )["reviews_opened"] == []
    # And the covenant tick carries the same opening.
    pipeline2 = hartline_pipeline()
    cf2_file, *_ = approved_calloway(pipeline2)
    tick = pipeline2.tick_covenants(as_of=date(2027, 7, 1))
    assert len(tick["reviews_opened"]) == 1
    tick2 = pipeline2.tick_covenants(as_of=date(2027, 7, 1))
    assert tick2["reviews_opened"] == []


def test_sqlite_round_trip_and_migration_0011(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    pipeline = hartline_pipeline(store=store)
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 7, 1)
    )
    outcome = pipeline.record_review_outcome(
        cf, review_number=1, stance="escalate_to_committee",
        reason_codes=["policy_exception_open"],
        reviewer_id=SECOND_OFFICER, reviewer_role=SECOND_OFFICER_ROLE,
        pack_id=pack.pack_id,
    )
    assert store.get_review_pack(pack.pack_id).model_dump() == (
        pack.model_dump()
    )
    assert [p.pack_id for p in store.list_review_packs(cf)] == [pack.pack_id]
    assert store.get_review_outcome(
        outcome.outcome_id
    ).model_dump() == outcome.model_dump()
    referrals = store.list_committee_referrals(cf)
    assert [r.referral_id for r in referrals] == [outcome.referral_id]
    # Insert-once: saving again records nothing twice.
    store.save_review_pack(pack)
    store.save_review_outcome(outcome)
    assert len(store.list_review_packs(cf)) == 1
    assert len(store.list_review_outcomes(cf)) == 1

    names = [f.name for f in migration_files(MIGRATIONS_DIR)]
    assert "0011_annual_reviews.sql" in names
    sql = (MIGRATIONS_DIR / "0011_annual_reviews.sql").read_text()
    for table in (
        "annual_review_packs", "annual_review_outcomes",
        "committee_referrals",
    ):
        assert table in sql


@pytest.fixture()
def client():
    pipeline = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app), pipeline


def test_api_review_surfaces(client):
    http, pipeline = client
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id

    schedule = http.get(
        f"/applications/{cf}/annual-reviews",
        params={"as_of": "2027-06-15"},
    )
    assert schedule.status_code == 200
    view = schedule.json()["sets"][0]
    assert view["anchor_basis"] == "closing_date"
    assert view["anchor_date"] == CLOSING.isoformat()
    first = view["instances"][0]
    assert first["due_date"] == "2027-07-01"
    assert first["state"] == "due_soon"

    assembled = http.post(
        f"/applications/{cf}/annual-reviews/1/pack",
        json={"as_of": "2027-07-01"},
    )
    assert assembled.status_code == 201
    pack = assembled.json()
    assert pack["complete"] is False
    assert pack["pinned_policy_version"] == "v1"
    assert pack["current_policy_version"] == "v2"

    packs = http.get(f"/applications/{cf}/annual-review-packs")
    assert [p["pack_id"] for p in packs.json()] == [pack["pack_id"]]
    detail = http.get(
        f"/applications/{cf}/annual-review-packs/{pack['pack_id']}"
    )
    assert detail.status_code == 200
    assert detail.json()["summary"]["verification"]["verified"] is True

    queue = http.get(
        "/annual-review-due-work", params={"as_of": "2027-07-01"}
    )
    assert queue.status_code == 200
    assert any(
        entry["review_instance"]["review_number"] == 1
        for entry in queue.json()
    )

    outcome = http.post(
        f"/applications/{cf}/annual-reviews/1/outcome",
        json={
            "stance": "escalate_to_committee",
            "reason_codes": ["policy_exception_open"],
            "reviewer_id": SECOND_OFFICER,
            "reviewer_role": SECOND_OFFICER_ROLE,
            "pack_id": pack["pack_id"],
        },
    )
    assert outcome.status_code == 201
    assert outcome.json()["referral_id"]
    referrals = http.get(f"/applications/{cf}/committee-referrals")
    assert len(referrals.json()) == 1
    outcomes = http.get(f"/applications/{cf}/annual-review-outcomes")
    assert len(outcomes.json()) == 1
    # A refused outcome is a 422 with the reason, never a 500.
    refused = http.post(
        f"/applications/{cf}/annual-reviews/1/outcome",
        json={
            "stance": "affirm",
            "reason_codes": ["performance_in_line"],
            "reviewer_id": SECOND_OFFICER,
            "reviewer_role": SECOND_OFFICER_ROLE,
            "pack_id": pack["pack_id"],
        },
    )
    assert refused.status_code == 422
    missing = http.get("/applications/cf-nope/annual-review-packs")
    assert missing.status_code == 404
