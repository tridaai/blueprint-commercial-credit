"""Round 10 — covenants: the model, the schedule, the tick, the
amendment discipline.

An approval starts a facility's monitoring life. These tests pin:

  the set         an approval (plain or conditional) creates the
                  covenant set from the decision's conditions plus
                  the policy template; declines and deferrals
                  create none; the set pins the decision id and
                  the policy pack version it came from
  the anchor      without a recorded closing date the set stands
                  in the named awaiting_closing_date state and
                  derives no schedule; recording the closing date
                  is an audited act by a named actor
  the schedule    pure date arithmetic — the graded table:
                  quarterly and annual cadences, leap years,
                  fiscal-year offsets, the maturity cap; every
                  test instance names the inputs it needs
  the queue       the due-work queue lists what is due, for which
                  borrower, needing which inputs
  the tick        idempotent: a crossing is recorded on the audit
                  trail once, however often the tick runs
  the amendment   a threshold changes only by supersession — a new
                  version with a named author, a coded reason and
                  an authorising committee decision; the stored set
                  is byte-identical before and after; there is no
                  in-place mutation surface anywhere
  the store       SQLite round-trip; migration 0009 registered

Fixtures ride the committee-ready Calloway file from
test_committee_gate: FY2025 DSCR exactly 1.3125, the v1 floor 1.25
traced, spread years 2023–2025.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.covenants import (
    add_months,
    covenant_test_dates,
    deliverable_due_dates,
    fiscal_year_end,
)
from commercial_credit.db import migration_files
from commercial_credit.errors import NotFoundError, ValidationError
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.models import CommitteeCondition
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.store import InMemoryCreditStore, SqliteCreditStore

from test_committee_gate import (
    CHAIR,
    CHAIR_ROLE,
    OFFICER,
    OFFICER_ROLE,
    convened_final,
    decide,
    hartline_pipeline,
)

CLOSING = date(2026, 7, 1)

DSCR_ANNUAL = CommitteeCondition(
    condition_id="",
    type="financial_covenant",
    metric_key="dscr",
    operator=">=",
    threshold=Decimal("1.25"),  # the v1 policy floor — traced
    due_cadence="annual",
)
ANNUAL_REPORTING = CommitteeCondition(
    condition_id="",
    type="reporting_requirement",
    report_what="Annual audited financial statements",
    due_cadence="annual",
)


def approved_calloway(pipeline: CreditPipeline, *, closing=CLOSING):
    """Calloway, committee-approved with conditions, the closing
    date recorded before the decision. Returns (credit_file,
    final_memo, decision, covenant_set)."""
    credit_file, draft, final = convened_final(pipeline)
    cf = credit_file.credit_file_id
    if closing is not None:
        pipeline.record_closing(
            cf, closing_date=closing, actor=OFFICER,
            actor_role=OFFICER_ROLE,
        )
    decision = decide(
        pipeline, cf, final.memo_id,
        outcome="approve_with_conditions",
        conditions=[DSCR_ANNUAL, ANNUAL_REPORTING],
    )
    sets = pipeline.list_covenant_sets(cf)
    assert len(sets) == 1
    return credit_file, final, decision, sets[0]


# ---------------------------------------------------------------------
# The set: created by approvals, from conditions + template
# ---------------------------------------------------------------------


def test_conditional_approval_creates_the_set():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    # The set pins what it came from.
    assert covenant_set.decision_id == decision.decision_id
    assert covenant_set.memo_id == final.memo_id
    assert covenant_set.policy_pack_id == "aldergrove-policy"
    assert covenant_set.policy_version == "v1"
    assert covenant_set.state == "active"
    assert covenant_set.closing_date == CLOSING
    assert covenant_set.term_months == 60
    assert covenant_set.created_by == CHAIR
    # The condition's covenant governs the template's DSCR entry:
    # exactly one annual DSCR covenant, sourced from the condition,
    # carrying the gate's threshold trace.
    dscr = [c for c in covenant_set.covenants if c.metric_key == "dscr"]
    assert len(dscr) == 1
    assert dscr[0].source == "committee_condition"
    assert dscr[0].origin_id == "cond-1"
    assert dscr[0].threshold == Decimal("1.25")
    assert "min_dscr" in (dscr[0].threshold_trace or "")
    assert dscr[0].frequency == "annual"
    # The template contributes leverage + liquidity, by origin id.
    template = {
        c.origin_id: c
        for c in covenant_set.covenants if c.source == "policy_template"
    }
    assert set(template) == {
        "tpl-max-leverage-quarterly", "tpl-min-current-ratio-quarterly",
    }
    assert template["tpl-max-leverage-quarterly"].metric_key == (
        "debt_to_tangible_net_worth"
    )
    assert template["tpl-max-leverage-quarterly"].frequency == "quarterly"
    assert template["tpl-min-current-ratio-quarterly"].basis == (
        "point_in_time"
    )
    # Deliverables: the committee's reporting condition plus the
    # template's annual statements.
    origins = {(d.source, d.origin_id) for d in covenant_set.deliverables}
    assert ("committee_condition", "cond-2") in origins
    assert ("policy_template", "tpl-annual-audited-statements") in origins
    # Creation is on the audit trail.
    actions = [e.action for e in pipeline.store.list_audit(cf)]
    assert "covenant_set_created" in actions
    assert "facility_closed" in actions


def test_plain_approval_creates_a_template_only_set():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf = credit_file.credit_file_id
    pipeline.record_closing(
        cf, closing_date=CLOSING, actor=OFFICER, actor_role=OFFICER_ROLE
    )
    decision = decide(pipeline, cf, final.memo_id, outcome="approve")
    sets = pipeline.list_covenant_sets(cf)
    assert len(sets) == 1
    covenant_set = sets[0]
    assert covenant_set.decision_id == decision.decision_id
    assert all(c.source == "policy_template" for c in covenant_set.covenants)
    assert {c.metric_key for c in covenant_set.covenants} == {
        "debt_to_tangible_net_worth", "dscr", "current_ratio",
    }
    assert [d.origin_id for d in covenant_set.deliverables] == [
        "tpl-annual-audited-statements"
    ]


def test_decline_and_defer_create_no_set():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf = credit_file.credit_file_id
    deferred = decide(
        pipeline, cf, final.memo_id, outcome="defer",
        deferral_reasons=["awaiting_required_documents"],
        representment_requirements=["FY2025 audited statements"],
    )
    assert pipeline.list_covenant_sets(cf) == []
    with pytest.raises(ValidationError, match="never by a decline"):
        pipeline.create_covenant_set(cf, decision_id=deferred.decision_id,
                                     actor=CHAIR)


def test_a_set_without_a_closing_date_waits_named():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(
        pipeline, closing=None
    )
    cf = credit_file.credit_file_id
    assert covenant_set.state == "awaiting_closing_date"
    assert covenant_set.closing_date is None
    # The covenants exist; the schedule does not — yet.
    schedule = pipeline.covenant_schedule(cf, as_of=date(2027, 1, 15))
    assert schedule["sets"][0]["instances"] == []
    # Recording the closing date completes the set for scheduling,
    # without rewriting the stored artefact.
    pipeline.record_closing(
        cf, closing_date=CLOSING, actor=OFFICER, actor_role=OFFICER_ROLE
    )
    stored = pipeline.get_covenant_set(cf, covenant_set.set_id)
    assert stored.state == "awaiting_closing_date"  # as recorded
    schedule = pipeline.covenant_schedule(cf, as_of=date(2027, 1, 15))
    assert schedule["sets"][0]["covenant_set"].state == "active"
    assert schedule["sets"][0]["instances"] != []


def test_closing_recording_is_audited_and_corrections_are_named():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf = credit_file.credit_file_id
    pipeline.record_closing(
        cf, closing_date=CLOSING, actor=OFFICER, actor_role=OFFICER_ROLE
    )
    # Same date again: a no-op, not a second event.
    audit_before = len(pipeline.store.list_audit(cf))
    pipeline.record_closing(
        cf, closing_date=CLOSING, actor=OFFICER, actor_role=OFFICER_ROLE
    )
    assert len(pipeline.store.list_audit(cf)) == audit_before
    # A different date is a correction, and it says so.
    pipeline.record_closing(
        cf, closing_date=date(2026, 7, 15), actor=OFFICER,
        actor_role=OFFICER_ROLE,
    )
    events = [
        e for e in pipeline.store.list_audit(cf)
        if e.action == "closing_date_corrected"
    ]
    assert len(events) == 1
    assert "2026-07-01" in events[0].detail
    assert "2026-07-15" in events[0].detail
    with pytest.raises(ValidationError, match="named actor"):
        pipeline.record_closing(
            cf, closing_date=date(2026, 8, 1), actor="",
            actor_role=OFFICER_ROLE,
        )


# ---------------------------------------------------------------------
# The schedule: pure date arithmetic, graded
# ---------------------------------------------------------------------


def test_add_months_clamps_to_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert add_months(date(2026, 7, 1), 60) == date(2031, 7, 1)


def test_fiscal_year_end_clamps():
    assert fiscal_year_end(2024, month=2, day=29) == date(2024, 2, 29)
    assert fiscal_year_end(2025, month=2, day=29) == date(2025, 2, 28)
    assert fiscal_year_end(2026, month=6, day=30) == date(2026, 6, 30)


def test_annual_fiscal_tests_land_on_year_ends_within_term():
    dates = covenant_test_dates(
        date(2026, 1, 15), frequency="annual",
        basis="fiscal_period_end", term_months=36,
    )
    assert dates == [
        (date(2026, 12, 31), 2026),
        (date(2027, 12, 31), 2027),
        (date(2028, 12, 31), 2028),
    ]  # maturity 2029-01-15: the 2029 year end is beyond it


def test_quarterly_fiscal_tests_walk_the_quarter_ends():
    dates = covenant_test_dates(
        date(2026, 1, 15), frequency="quarterly",
        basis="fiscal_period_end", term_months=12,
    )
    assert dates == [
        (date(2026, 3, 31), 2026),
        (date(2026, 6, 30), 2026),
        (date(2026, 9, 30), 2026),
        (date(2026, 12, 31), 2026),
    ]


def test_fiscal_year_offset_moves_the_test_dates():
    dates = covenant_test_dates(
        date(2026, 1, 15), frequency="annual",
        basis="fiscal_period_end", term_months=36,
        fiscal_month=6, fiscal_day=30,
    )
    assert dates == [
        (date(2026, 6, 30), 2026),
        (date(2027, 6, 30), 2027),
        (date(2028, 6, 30), 2028),
    ]


def test_point_in_time_adds_a_final_test_at_maturity():
    # Closing on a leap day: maturity clamps to 2025-02-28, which
    # is no period end — the facility is still tested at the end.
    dates = covenant_test_dates(
        date(2024, 2, 29), frequency="annual",
        basis="point_in_time", term_months=12,
    )
    assert dates == [
        (date(2024, 12, 31), 2024),
        (date(2025, 2, 28), None),
    ]


def test_deliverable_due_dates_carry_the_reporting_lag():
    dates = deliverable_due_dates(
        date(2026, 7, 1), frequency="annual", term_months=60,
    )
    assert dates == [
        (date(2027, 3, 31), 2026),
        (date(2028, 3, 30), 2027),  # 2028 is a leap year: +90 lands Mar 30
        (date(2029, 3, 31), 2028),
        (date(2030, 3, 31), 2029),
        (date(2031, 3, 31), 2030),
    ]


def test_the_calloway_schedule_is_exact():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    schedule = pipeline.covenant_schedule(cf, as_of=date(2026, 8, 1))
    instances = schedule["sets"][0]["instances"]
    by_kind_metric = {}
    for instance in instances:
        key = (instance.kind, instance.metric_key or instance.name)
        by_kind_metric.setdefault(key, []).append(instance)

    # The condition's annual DSCR test: Dec 31 of 2026..2030.
    dscr = by_kind_metric[("covenant_test", "dscr")]
    assert [i.test_date for i in dscr] == [
        date(2026, 12, 31), date(2027, 12, 31), date(2028, 12, 31),
        date(2029, 12, 31), date(2030, 12, 31),
    ]
    # Template leverage, quarterly on fiscal period ends.
    leverage = by_kind_metric[("covenant_test", "debt_to_tangible_net_worth")]
    assert len(leverage) == 20
    assert leverage[0].test_date == date(2026, 9, 30)
    assert leverage[-1].test_date == date(2031, 6, 30)
    # Template liquidity, quarterly point-in-time + maturity.
    liquidity = by_kind_metric[("covenant_test", "current_ratio")]
    assert len(liquidity) == 21
    assert liquidity[-1].test_date == date(2031, 7, 1)
    # Both annual deliverables ride the same due dates.
    deliverables = [
        i for i in instances if i.kind == "reporting_deliverable"
    ]
    assert len(deliverables) == 10  # 2 deliverables x 5 fiscal years
    assert {i.test_date for i in deliverables} == {
        date(2027, 3, 31), date(2028, 3, 30), date(2029, 3, 31),
        date(2030, 3, 31), date(2031, 3, 31),
    }
    # At 2026-08-01 nothing is in its window yet.
    assert all(i.state == "scheduled" for i in instances)


def test_an_instance_without_its_period_stands_awaiting_inputs():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    schedule = pipeline.covenant_schedule(cf, as_of=date(2026, 12, 15))
    instances = schedule["sets"][0]["instances"]
    dscr_2026 = next(
        i for i in instances
        if i.metric_key == "dscr" and i.test_date == date(2026, 12, 31)
    )
    # FY2026 statements are not on file: the test names them.
    assert dscr_2026.state == "awaiting_inputs"
    assert any("FY2026" in name for name in dscr_2026.required_inputs)
    # And it is in the due-work queue, with the inputs named.
    queue = pipeline.covenant_due_work(as_of=date(2026, 12, 15))
    entry = next(
        e for e in queue if e["instance"].instance_id == dscr_2026.instance_id
    )
    assert entry["borrower"] == "Calloway Precision Manufacturing, Inc."
    assert entry["instance"].state == "awaiting_inputs"
    assert entry["instance"].required_inputs == dscr_2026.required_inputs


def test_a_test_on_a_spread_period_stands_by_its_date():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    # The point-in-time liquidity test at 2026-09-30 measures the
    # latest spread period at that date (FY2025 — on file): it
    # stands by its date and grace, not by missing inputs.
    schedule = pipeline.covenant_schedule(cf, as_of=date(2026, 10, 15))
    instances = schedule["sets"][0]["instances"]
    liquidity = next(
        i for i in instances
        if i.metric_key == "current_ratio"
        and i.test_date == date(2026, 9, 30)
    )
    assert liquidity.state == "due"  # within its 30-day grace
    assert liquidity.period_label == "FY2025"
    schedule = pipeline.covenant_schedule(cf, as_of=date(2026, 11, 15))
    liquidity = next(
        i for i in schedule["sets"][0]["instances"]
        if i.metric_key == "current_ratio"
        and i.test_date == date(2026, 9, 30)
    )
    assert liquidity.state == "overdue"


# ---------------------------------------------------------------------
# The tick: crossings recorded once
# ---------------------------------------------------------------------


def test_the_tick_records_each_crossing_once():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    as_of = date(2027, 1, 15)
    first = pipeline.tick_covenants(as_of=as_of)
    # Due/overdue at 2027-01-15: the liquidity tests at 2026-09-30
    # (overdue, past grace) and 2026-12-31 (due, within grace).
    # Everything fiscal-period stands awaiting its FY2026 figures.
    assert len(first["crossings_recorded"]) == 2
    assert {c["state"] for c in first["crossings_recorded"]} == {
        "due", "overdue",
    }
    assert all(
        c["credit_file_id"] == cf for c in first["crossings_recorded"]
    )
    # Re-running the tick records nothing new.
    second = pipeline.tick_covenants(as_of=as_of)
    assert second["crossings_recorded"] == []
    audit = pipeline.store.list_audit(cf)
    crossings = [
        e for e in audit
        if e.action in ("covenant_test_due", "covenant_test_overdue")
    ]
    assert len(crossings) == 2


# ---------------------------------------------------------------------
# The amendment: supersession, never edit
# ---------------------------------------------------------------------


def _dscr_covenant(covenant_set):
    return next(
        c for c in covenant_set.covenants if c.metric_key == "dscr"
    )


def test_amendment_mints_a_new_version_and_moves_the_schedule():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    original = _dscr_covenant(covenant_set)
    stored_before = pipeline.get_covenant_set(
        cf, covenant_set.set_id
    ).model_dump_json()
    record = pipeline.amend_covenant(
        cf, original.covenant_id,
        author_id=CHAIR, author_role=CHAIR_ROLE,
        reason_code="committee_redecision",
        decision_id=decision.decision_id,
        threshold=Decimal("1.30"),
    )
    assert record.covenant.version == 2
    assert record.covenant.threshold == Decimal("1.30")
    assert record.covenant.supersedes_covenant_id == original.covenant_id
    assert record.covenant.amendment_decision_id == decision.decision_id
    # The stored set is byte-identical: the amendment lives beside
    # it, not inside it.
    stored_after = pipeline.get_covenant_set(
        cf, covenant_set.set_id
    ).model_dump_json()
    assert stored_after == stored_before
    # The derived current covenants — and the schedule — moved.
    schedule = pipeline.covenant_schedule(cf, as_of=date(2026, 8, 1))
    current = schedule["sets"][0]["current_covenants"]
    current_dscr = next(c for c in current if c.metric_key == "dscr")
    assert current_dscr.threshold == Decimal("1.30")
    assert current_dscr.version == 2
    dscr_instance = next(
        i for i in schedule["sets"][0]["instances"]
        if i.metric_key == "dscr"
    )
    assert dscr_instance.threshold == Decimal("1.30")
    # Amending by the SUPERSEDED id amends the current version:
    # the lineage is walked, never forked.
    record2 = pipeline.amend_covenant(
        cf, original.covenant_id,
        author_id=CHAIR, author_role=CHAIR_ROLE,
        reason_code="committee_redecision",
        decision_id=decision.decision_id,
        threshold=Decimal("1.35"),
    )
    assert record2.covenant.version == 3
    assert record2.superseded_covenant_id == record.covenant.covenant_id
    assert len(pipeline.list_covenant_amendments(cf)) == 2


def test_amendment_refusals_name_themselves():
    pipeline = hartline_pipeline()
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    original = _dscr_covenant(covenant_set)

    def amend(**overrides):
        kwargs = dict(
            author_id=CHAIR, author_role=CHAIR_ROLE,
            reason_code="committee_redecision",
            decision_id=decision.decision_id,
            threshold=Decimal("1.30"),
        )
        kwargs.update(overrides)
        return pipeline.amend_covenant(cf, original.covenant_id, **kwargs)

    with pytest.raises(ValidationError, match="unknown amendment reason"):
        amend(reason_code="felt_like_it")
    with pytest.raises(ValidationError, match="requires a note"):
        amend(reason_code="other", note="")
    with pytest.raises(ValidationError, match="may not amend covenants"):
        amend(author_id="rm-priya-nair",
              author_role="relationship_manager")
    with pytest.raises(ValidationError, match="requires the committee"):
        amend(decision_id="decision-does-not-exist")
    with pytest.raises(ValidationError, match="changes nothing"):
        amend(threshold=Decimal("1.25"))
    with pytest.raises(NotFoundError, match="not found"):
        pipeline.amend_covenant(
            cf, "cov-nope",
            author_id=CHAIR, author_role=CHAIR_ROLE,
            reason_code="committee_redecision",
            decision_id=decision.decision_id,
            threshold=Decimal("1.30"),
        )
    # Nothing landed through any refusal.
    assert pipeline.list_covenant_amendments(cf) == []


def test_there_is_no_in_place_covenant_mutation_surface():
    # No pipeline or store method updates a covenant or a set in
    # place — the only write paths are save (insert-once) and the
    # amendment record.
    for cls in (CreditPipeline,):
        names = [n for n in dir(cls) if not n.startswith("__")]
        offenders = [
            n for n in names
            if ("covenant" in n or "closing" in n)
            and any(
                verb in n
                for verb in ("update", "edit", "mutate", "replace",
                             "set_threshold", "modify")
            )
        ]
        assert offenders == []
    from commercial_credit.store import (
        InMemoryCreditStore,
        PostgresCreditStore,
        SqliteCreditStore,
    )
    for cls in (InMemoryCreditStore, SqliteCreditStore, PostgresCreditStore):
        names = [n for n in dir(cls) if "covenant" in n]
        assert set(names) == {
            "save_covenant_set", "get_covenant_set",
            "list_covenant_sets", "save_covenant_amendment",
            "list_covenant_amendments",
        }, (cls.__name__, names)
    # And the pipeline's covenant surface is exactly the round's.
    pipeline_fns = {
        n for n in dir(CreditPipeline)
        if ("covenant" in n or n == "record_closing")
        and not n.startswith("_")
    }
    assert pipeline_fns == {
        "record_closing", "create_covenant_set", "list_covenant_sets",
        "get_covenant_set", "list_covenant_amendments",
        "covenant_schedule", "covenant_due_work", "tick_covenants",
        "amend_covenant",
    }
    assert inspect.signature(
        CreditPipeline.amend_covenant
    ).parameters.keys() >= {"threshold", "operator", "frequency",
                            "grace_days"}


# ---------------------------------------------------------------------
# The store: SQLite round-trip + migration 0009
# ---------------------------------------------------------------------


def test_sqlite_round_trip_for_sets_and_amendments(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    pipeline = hartline_pipeline(store=store)
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    original = _dscr_covenant(covenant_set)
    pipeline.amend_covenant(
        cf, original.covenant_id,
        author_id=CHAIR, author_role=CHAIR_ROLE,
        reason_code="committee_redecision",
        decision_id=decision.decision_id,
        threshold=Decimal("1.30"),
    )
    fetched = store.get_covenant_set(covenant_set.set_id)
    assert fetched is not None
    assert fetched.model_dump() == covenant_set.model_dump()
    assert [s.set_id for s in store.list_covenant_sets(cf)] == [
        covenant_set.set_id
    ]
    amendments = store.list_covenant_amendments(cf)
    assert len(amendments) == 1
    assert amendments[0].covenant.threshold == Decimal("1.30")
    # Insert-once: saving the same set again changes nothing.
    store.save_covenant_set(covenant_set)
    assert len(store.list_covenant_sets(cf)) == 1


def test_migration_0009_is_registered():
    names = [f.name for f in migration_files(MIGRATIONS_DIR)]
    assert "0009_covenant_sets.sql" in names
    sql = (MIGRATIONS_DIR / "0009_covenant_sets.sql").read_text()
    assert "covenant_sets" in sql
    assert "covenant_amendments" in sql


# ---------------------------------------------------------------------
# The API surface
# ---------------------------------------------------------------------


@pytest.fixture()
def client():
    pipeline = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app), pipeline


def test_api_covenant_surfaces(client):
    http, pipeline = client
    credit_file, final, decision, covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id

    sets = http.get(f"/applications/{cf}/covenant-sets")
    assert sets.status_code == 200
    assert [s["set_id"] for s in sets.json()] == [covenant_set.set_id]
    one = http.get(f"/applications/{cf}/covenant-sets/{covenant_set.set_id}")
    assert one.status_code == 200
    assert one.json()["policy_version"] == "v1"

    schedule = http.get(
        f"/applications/{cf}/covenant-schedule",
        params={"as_of": "2026-12-15"},
    )
    assert schedule.status_code == 200
    view = schedule.json()["sets"][0]
    dscr = next(
        i for i in view["instances"] if i["metric_key"] == "dscr"
        and i["test_date"] == "2026-12-31"
    )
    assert dscr["state"] == "awaiting_inputs"
    assert any("FY2026" in name for name in dscr["required_inputs"])

    queue = http.get("/covenant-due-work", params={"as_of": "2026-12-15"})
    assert queue.status_code == 200
    assert any(
        entry["instance"]["instance_id"] == dscr["instance_id"]
        for entry in queue.json()
    )

    tick = http.post(
        "/covenant-scheduler/tick", json={"as_of": "2027-01-15"}
    )
    assert tick.status_code == 200
    assert len(tick.json()["crossings_recorded"]) == 2
    tick2 = http.post(
        "/covenant-scheduler/tick", json={"as_of": "2027-01-15"}
    )
    assert tick2.json()["crossings_recorded"] == []

    original = _dscr_covenant(covenant_set)
    amended = http.post(
        f"/applications/{cf}/covenants/{original.covenant_id}/amendments",
        json={
            "author_id": CHAIR,
            "author_role": CHAIR_ROLE,
            "reason_code": "committee_redecision",
            "decision_id": decision.decision_id,
            "threshold": "1.30",
        },
    )
    assert amended.status_code == 201
    assert amended.json()["covenant"]["threshold"] == "1.30"
    amendments = http.get(f"/applications/{cf}/covenant-amendments")
    assert len(amendments.json()) == 1
    # A refused amendment is a 422 with the reason, never a 500.
    refused = http.post(
        f"/applications/{cf}/covenants/{original.covenant_id}/amendments",
        json={
            "author_id": CHAIR,
            "author_role": CHAIR_ROLE,
            "reason_code": "felt_like_it",
            "decision_id": decision.decision_id,
            "threshold": "1.40",
        },
    )
    assert refused.status_code == 422
