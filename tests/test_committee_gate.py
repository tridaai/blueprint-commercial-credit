"""Round 7: the committee gate + coded adverse-action reasons.

The second human gate, graded on its refusals as much as its
landings:

  a committee of one                 refused (the quorum is at
                                      least two, mechanically)
  a recorder who was not a member    refused
  the approving officer as the
    committee's only member          refused (law 4)
  deciding a memo that was never
    convened, or was already
    decided, or is a superseded
    draft                            refused (state discipline)
  a decline with no reasons, with a
    reason citing a PASSED test,
    with a reason wearing the wrong
    evidence, or with an untraced
    figure                           refused, the offender named
  a condition whose threshold does
    not trace                        refused

And the landings: a quorum approval closes the memo as
committee_approved; a conditional approval stores its structured
conditions, thresholds traced; a decline with properly tied
reasons renders an adverse-action notice generated from those
reasons; a deferral names its requirements and the memo is
re-presented and decided on the same artefact.

Hand-decided anchors (rounds 3–5): Hartline's pinned v1 report
passes leverage (1.5337 vs the 2.00 ceiling) and concentration,
fails required_documents_complete (7 of 9 items outstanding:
chk-03…chk-09), and cannot evaluate DSCR, global DSCR, fixed
charge coverage or appraisal age. FY2024 working capital is
$2,202,000.00, traced in the memo's verification record.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.committee import (
    ADVERSE_REASONS_VERSION,
    DEFERRAL_REASONS_VERSION,
)
from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.db import migration_files
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.errors import NotFoundError, ValidationError
from commercial_credit.memo import render_memo
from commercial_credit.models import (
    AdverseActionReason,
    CommitteeCondition,
    CommitteeMember,
    DocumentKind,
)
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import ReferenceLosFeed, statement_document
from commercial_credit.store import InMemoryCreditStore, SqliteCreditStore

OFFICER = "officer-amara-osei"
OFFICER_ROLE = "credit_officer"
RM = "rm-priya-nair"
RM_ROLE = "relationship_manager"
CHAIR = "chair-eleanor-voss"
CHAIR_ROLE = "committee_chair"
MEMBER = "member-tomas-lindqvist"
MEMBER_ROLE = "committee_member"

CHAIR_MEMBER = CommitteeMember(member_id=CHAIR, member_role=CHAIR_ROLE)
PLAIN_MEMBER = CommitteeMember(member_id=MEMBER, member_role=MEMBER_ROLE)
OFFICER_MEMBER = CommitteeMember(member_id=OFFICER, member_role=OFFICER_ROLE)
QUORUM = [CHAIR_MEMBER, PLAIN_MEMBER]


def hartline_pipeline(store=None) -> CreditPipeline:
    return CreditPipeline(
        store=store or InMemoryCreditStore(), documents=LocalDocumentStore()
    )


def open_hartline(pipeline: CreditPipeline):
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


def officer_final(pipeline: CreditPipeline):
    """The Hartline officer-final memo under the pinned v1 report."""
    credit_file = open_hartline(pipeline)
    report = pipeline.evaluate_policy(
        credit_file.credit_file_id, pack_version="v1", as_of=date(2025, 6, 30)
    )
    memo = pipeline.compose_memo(
        credit_file.credit_file_id, report_id=report.report_id
    )
    cf = credit_file.credit_file_id
    pipeline.submit_memo_for_review(
        cf, memo.memo_id, actor=RM, actor_role=RM_ROLE
    )
    final = pipeline.approve_memo(
        cf, memo.memo_id, officer_id=OFFICER, officer_role=OFFICER_ROLE
    )
    return credit_file, memo, final


def convened_final(pipeline: CreditPipeline):
    credit_file, draft, final = officer_final(pipeline)
    pipeline.convene_committee(
        credit_file.credit_file_id, final.memo_id,
        actor=OFFICER, actor_role=OFFICER_ROLE,
    )
    return credit_file, draft, final


def decide(pipeline, credit_file_id, memo_id, **overrides):
    kwargs = dict(
        recorder_id=CHAIR,
        recorder_role=CHAIR_ROLE,
        members=list(QUORUM),
        outcome="approve",
    )
    kwargs.update(overrides)
    return pipeline.decide_committee(credit_file_id, memo_id, **kwargs)


# ---------------------------------------------------------------------
# Convening: only the officer-final memo, only from a lawful state
# ---------------------------------------------------------------------


def test_convene_moves_an_officer_final_memo_into_committee():
    pipeline = hartline_pipeline()
    credit_file, draft, final = officer_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    assert pipeline.memo_status(cf, fid) == "officer_approved"
    convened = pipeline.convene_committee(
        cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE
    )
    assert convened.status == "in_committee"
    assert pipeline.memo_status(cf, fid) == "in_committee"
    # Convening twice is not a thing.
    with pytest.raises(ValidationError, match="re-presented"):
        pipeline.convene_committee(
            cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE
        )
    # The draft's own status is untouched by the committee's life.
    assert pipeline.memo_status(cf, draft.memo_id) == "officer_approved"
    audits = [
        e.action for e in pipeline.store.list_audit(cf)
    ]
    assert "committee_convened" in audits


def test_the_committee_cannot_touch_a_draft_or_a_superseded_version():
    pipeline = hartline_pipeline()
    credit_file, draft, final = officer_final(pipeline)
    cf = credit_file.credit_file_id
    # The draft was superseded by the officer-final memo.
    with pytest.raises(ValidationError, match="superseded"):
        pipeline.convene_committee(
            cf, draft.memo_id, actor=OFFICER, actor_role=OFFICER_ROLE
        )
    with pytest.raises(ValidationError, match="superseded"):
        decide(pipeline, cf, draft.memo_id)
    # A memo still at the officer gate cannot be convened either.
    pipeline2 = hartline_pipeline()
    credit_file2 = open_hartline(pipeline2)
    memo2 = pipeline2.store.list_memos(credit_file2.credit_file_id)[0]
    with pytest.raises(ValidationError, match="officer-final"):
        pipeline2.convene_committee(
            credit_file2.credit_file_id, memo2.memo_id,
            actor=OFFICER, actor_role=OFFICER_ROLE,
        )
    assert pipeline.store.list_committee_decisions(cf) == []


def test_convene_requires_a_named_actor():
    pipeline = hartline_pipeline()
    credit_file, draft, final = officer_final(pipeline)
    with pytest.raises(ValidationError):
        pipeline.convene_committee(
            credit_file.credit_file_id, final.memo_id,
            actor="", actor_role=OFFICER_ROLE,
        )
    assert (
        pipeline.memo_status(credit_file.credit_file_id, final.memo_id)
        == "officer_approved"
    )


# ---------------------------------------------------------------------
# Quorum discipline
# ---------------------------------------------------------------------


def test_a_single_member_committee_is_refused():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    # One member, quorum honestly stated as 1 — still refused:
    # the quorum floor is mechanical, not a caller preference.
    with pytest.raises(ValidationError, match="quorum of 1"):
        decide(
            pipeline, cf, fid,
            members=[CHAIR_MEMBER], quorum_required=1,
        )
    # One member under a quorum of 2 — refused as short.
    with pytest.raises(ValidationError, match="quorum of 2"):
        decide(pipeline, cf, fid, members=[CHAIR_MEMBER])
    assert pipeline.store.list_committee_decisions(cf) == []
    assert pipeline.memo_status(cf, fid) == "in_committee"


def test_the_approving_officer_cannot_be_the_sole_member():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    with pytest.raises(ValidationError, match="quorum"):
        decide(
            pipeline, cf, fid,
            recorder_id=OFFICER, recorder_role=OFFICER_ROLE,
            members=[OFFICER_MEMBER],
        )
    # But the officer MAY sit as one member of a real committee.
    decision = decide(
        pipeline, cf, fid,
        recorder_id=OFFICER, recorder_role=OFFICER_ROLE,
        members=[OFFICER_MEMBER, CHAIR_MEMBER],
    )
    assert decision.outcome == "approve"
    assert pipeline.memo_status(cf, fid) == "committee_approved"


def test_quorum_mechanics_duplicates_recorder_and_roles():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    # The same person twice is not two members.
    with pytest.raises(ValidationError, match="distinct"):
        decide(
            pipeline, cf, fid,
            members=[CHAIR_MEMBER, CHAIR_MEMBER],
        )
    # The recorder must be one of the deciding members.
    with pytest.raises(ValidationError, match="not one of its recorded members"):
        decide(
            pipeline, cf, fid,
            recorder_id="clerk-outside", recorder_role="committee_chair",
        )
    # The preparer's side of the house does not sit.
    with pytest.raises(ValidationError, match="may not sit"):
        decide(
            pipeline, cf, fid,
            members=[
                CHAIR_MEMBER,
                CommitteeMember(member_id=RM, member_role=RM_ROLE),
            ],
        )
    # Anonymous members are nobody.
    with pytest.raises(ValidationError, match="empty id"):
        decide(
            pipeline, cf, fid,
            members=[
                CHAIR_MEMBER,
                CommitteeMember(member_id=" ", member_role=MEMBER_ROLE),
            ],
        )
    assert pipeline.store.list_committee_decisions(cf) == []


# ---------------------------------------------------------------------
# Approve: the happy path, and its finality
# ---------------------------------------------------------------------


def test_quorum_approval_lands_and_is_final():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    decision = decide(pipeline, cf, fid)
    assert decision.memo_id == fid
    assert decision.outcome == "approve"
    assert decision.quorum_required == 2
    assert [m.member_id for m in decision.members] == [CHAIR, MEMBER]
    assert decision.recorded_by == CHAIR
    assert decision.conditions == [] and decision.adverse_reasons == []
    assert pipeline.memo_status(cf, fid) == "committee_approved"
    assert pipeline.get_memo(cf, fid).status == "committee_approved"
    # The full chain reads in the memo history.
    history = pipeline.memo_history(cf, fid)
    assert history["status"] == "committee_approved"
    assert [d.decision_id for d in history["committee_decisions"]] == [
        decision.decision_id
    ]
    event_actions = [e.action for e in history["events"]]
    assert event_actions[-2:] == ["committee_convened", "committee_decided"]
    decided = history["events"][-1]
    assert decided.committee_outcome == "approve"
    assert decided.actor_id == CHAIR
    # The rendered memo now says what it is.
    assert "COMMITTEE APPROVED" in render_memo(pipeline.get_memo(cf, fid))
    # Final means final: no reconvening, no second decision.
    with pytest.raises(ValidationError):
        pipeline.convene_committee(
            cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE
        )
    with pytest.raises(ValidationError, match="cannot be decided again"):
        # (status is committee_approved; convene refused above, so
        # decide is probed against the same wall directly)
        decide(pipeline, cf, fid)
    assert len(pipeline.store.list_committee_decisions(cf)) == 1


def test_deciding_without_convening_is_refused():
    pipeline = hartline_pipeline()
    credit_file, draft, final = officer_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    with pytest.raises(ValidationError, match="convened"):
        decide(pipeline, cf, fid)
    assert pipeline.store.list_committee_decisions(cf) == []
    assert pipeline.memo_status(cf, fid) == "officer_approved"


# ---------------------------------------------------------------------
# Conditions: structured, traced, or refused
# ---------------------------------------------------------------------

DSCR_COVENANT = CommitteeCondition(
    condition_id="",
    type="financial_covenant",
    metric_key="dscr",
    operator=">=",
    threshold=Decimal("1.25"),  # the v1 policy floor — traced
    due_cadence="quarterly",
)
REPORTING = CommitteeCondition(
    condition_id="",
    type="reporting_requirement",
    report_what="Interim financial statements for the most recent quarter",
    due_cadence="quarterly",
)
DOCUMENTATION = CommitteeCondition(
    condition_id="",
    type="documentation",
    checklist_item_id="chk-03",  # business tax returns — outstanding
    due_cadence="before_closing",
)


def test_conditional_approval_stores_traced_conditions():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    decision = decide(
        pipeline, cf, fid,
        outcome="approve_with_conditions",
        conditions=[DSCR_COVENANT, REPORTING, DOCUMENTATION],
    )
    assert pipeline.memo_status(cf, fid) == "committee_approved_conditional"
    assert [c.condition_id for c in decision.conditions] == [
        "cond-1", "cond-2", "cond-3",
    ]
    covenant = decision.conditions[0]
    assert covenant.metric_key == "dscr"
    assert covenant.threshold == Decimal("1.25")
    assert "min_dscr" in (covenant.threshold_trace or "")
    assert decision.conditions[2].checklist_item_id == "chk-03"
    # Retrievable as a record, by id and by memo.
    fetched = pipeline.get_committee_decision(cf, decision.decision_id)
    assert fetched.model_dump() == decision.model_dump()
    state = pipeline.committee_state(cf, fid)
    assert state["latest_decision"].decision_id == decision.decision_id
    assert len(state["decisions"]) == 1


def test_conditional_approval_requires_conditions():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    with pytest.raises(ValidationError, match="at least one structured"):
        decide(pipeline, cf, fid, outcome="approve_with_conditions")
    # And conditions on a plain approval are refused, not ignored.
    with pytest.raises(ValidationError, match="carries no conditions"):
        decide(
            pipeline, cf, fid,
            outcome="approve", conditions=[REPORTING],
        )
    assert pipeline.store.list_committee_decisions(cf) == []


def test_an_untraceable_condition_threshold_is_refused():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    invented = DSCR_COVENANT.model_copy(
        update={"threshold": Decimal("1.37")}
    )
    with pytest.raises(ValidationError, match="does not trace"):
        decide(
            pipeline, cf, fid,
            outcome="approve_with_conditions", conditions=[invented],
        )
    # A metric the pinned pack never computed is refused too.
    unknown_metric = DSCR_COVENANT.model_copy(
        update={"metric_key": "interest_coverage"}
    )
    with pytest.raises(ValidationError, match="not a figure"):
        decide(
            pipeline, cf, fid,
            outcome="approve_with_conditions",
            conditions=[unknown_metric],
        )
    # A condition without a cadence is a wish.
    no_cadence = REPORTING.model_copy(update={"due_cadence": " "})
    with pytest.raises(ValidationError, match="cadence"):
        decide(
            pipeline, cf, fid,
            outcome="approve_with_conditions", conditions=[no_cadence],
        )
    # Documentation for an item the file does not have.
    wrong_item = DOCUMENTATION.model_copy(
        update={"checklist_item_id": "chk-99"}
    )
    with pytest.raises(ValidationError, match="not on this credit file"):
        decide(
            pipeline, cf, fid,
            outcome="approve_with_conditions", conditions=[wrong_item],
        )
    assert pipeline.store.list_committee_decisions(cf) == []
    assert pipeline.memo_status(cf, fid) == "in_committee"


# ---------------------------------------------------------------------
# Decline: coded reasons, tied to evidence — or no decline
# ---------------------------------------------------------------------

DOCS_REASON = AdverseActionReason(
    code="required_documentation_missing",
    tie_kind="policy_test",
    test_id="required_documents_complete",
)
DOCS_FACT_REASON = AdverseActionReason(
    code="required_documentation_missing",
    tie_kind="file_fact",
    checklist_item_id="chk-03",
)
INFO_REASON = AdverseActionReason(
    code="financial_information_insufficient",
    tie_kind="policy_test",
    test_id="min_dscr",
)


def test_decline_with_tied_reasons_lands_and_renders_the_notice():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    decision = decide(
        pipeline, cf, fid,
        outcome="decline",
        adverse_reasons=[DOCS_REASON, DOCS_FACT_REASON, INFO_REASON],
    )
    assert pipeline.memo_status(cf, fid) == "declined"
    assert decision.reason_vocabulary == ADVERSE_REASONS_VERSION
    assert [r.code for r in decision.adverse_reasons] == [
        "required_documentation_missing",
        "required_documentation_missing",
        "financial_information_insufficient",
    ]
    notice = pipeline.get_adverse_action_notice(cf, fid)
    assert "NOTICE OF ACTION TAKEN" in notice
    assert "Hartline Manufacturing" in notice
    assert "Required documentation is missing from the credit file" in notice
    assert "Required documents on file, by facility type" in notice
    assert "chk-03 (Business tax returns) is not on file" in notice
    assert (
        "The financial information provided does not allow the bank "
        "to evaluate the application" in notice
    )
    assert "depreciation_and_amortization" in notice
    assert ADVERSE_REASONS_VERSION in notice
    # Nothing in the notice is a hand-typed reason: every sentence
    # of substance comes from the vocabulary or the evidence.
    assert "committee felt" not in notice


def test_a_decline_without_coded_reasons_is_refused():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    with pytest.raises(ValidationError, match="coded adverse-action"):
        decide(pipeline, cf, fid, outcome="decline")
    with pytest.raises(ValidationError, match="coded adverse-action"):
        decide(
            pipeline, cf, fid, outcome="decline",
            note="The committee just did not like it.",
        )
    # Reasons belong to a decline; an approval carrying them is
    # refused rather than quietly storing them.
    with pytest.raises(ValidationError, match="carries no adverse-action"):
        decide(
            pipeline, cf, fid, outcome="approve",
            adverse_reasons=[DOCS_REASON],
        )
    assert pipeline.store.list_committee_decisions(cf) == []


def test_a_reason_citing_a_passed_test_is_refused_and_named():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    # Leverage PASSED at 1.5337 against the 2.00 ceiling.
    passed = AdverseActionReason(
        code="leverage_above_policy_limit",
        tie_kind="policy_test",
        test_id="max_leverage_by_segment",
    )
    with pytest.raises(ValidationError, match="PASS"):
        decide(
            pipeline, cf, fid, outcome="decline",
            adverse_reasons=[passed],
        )
    assert "max_leverage_by_segment" in str(
        _decline_error(pipeline, cf, fid, passed)
    )
    assert pipeline.store.list_committee_decisions(cf) == []


def _decline_error(pipeline, cf, fid, reason) -> Exception:
    with pytest.raises(ValidationError) as caught:
        pipeline.decide_committee(
            cf, fid, recorder_id=CHAIR, recorder_role=CHAIR_ROLE,
            members=list(QUORUM), outcome="decline",
            adverse_reasons=[reason],
        )
    return caught.value


def test_reasons_wearing_the_wrong_evidence_are_refused():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    # DSCR could not be evaluated: a coverage FAILURE claim over a
    # not_evaluable test is inaccurate — refused by name.
    miscast = AdverseActionReason(
        code="debt_service_coverage_below_policy",
        tie_kind="policy_test",
        test_id="min_dscr",
    )
    assert "financial_information_insufficient" in str(
        _decline_error(pipeline, cf, fid, miscast)
    )
    # A figure the memo never traced.
    ghost = AdverseActionReason(
        code="key_figure_shortfall",
        tie_kind="figure",
        figure_text="$9,999,999.99",
        figure_value="9999999.99",
        figure_source="computation pack, invented figure",
    )
    assert "does not trace" in str(_decline_error(pipeline, cf, fid, ghost))
    # A document that IS on file (statements were registered).
    on_file = AdverseActionReason(
        code="required_documentation_missing",
        tie_kind="file_fact",
        checklist_item_id="chk-01",
    )
    assert "on file" in str(_decline_error(pipeline, cf, fid, on_file))
    # An unknown code is not a code.
    unknown = AdverseActionReason(
        code="bad_feeling", tie_kind="policy_test",
        test_id="required_documents_complete",
    )
    assert "unknown adverse-action reason code" in str(
        _decline_error(pipeline, cf, fid, unknown)
    )
    # An untied reason is a free-text reason.
    untied = AdverseActionReason(code="key_figure_shortfall", tie_kind="none")
    assert "free-text" in str(_decline_error(pipeline, cf, fid, untied))
    assert pipeline.store.list_committee_decisions(cf) == []
    assert pipeline.memo_status(cf, fid) == "in_committee"


def test_a_decline_tied_to_a_traced_figure_lands():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    traced = next(
        t for t in final.verification.figures
        if t.text == "$2,202,000.00"
    )
    reason = AdverseActionReason(
        code="key_figure_shortfall",
        tie_kind="figure",
        figure_text=traced.text,
        figure_value=traced.value,
        figure_source=traced.source,
    )
    decision = decide(
        pipeline, cf, fid, outcome="decline", adverse_reasons=[reason]
    )
    assert decision.adverse_reasons[0].figure_text == "$2,202,000.00"
    notice = pipeline.get_adverse_action_notice(cf, fid)
    assert "$2,202,000.00" in notice
    assert "working_capital" in notice


def test_the_notice_exists_only_for_a_decline():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    with pytest.raises(NotFoundError):
        pipeline.get_adverse_action_notice(cf, fid)
    decide(pipeline, cf, fid)
    with pytest.raises(NotFoundError):
        pipeline.get_adverse_action_notice(cf, fid)


# ---------------------------------------------------------------------
# Defer: coded reasons + named requirements, then re-presentment
# ---------------------------------------------------------------------


def test_defer_requires_codes_and_named_requirements():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    with pytest.raises(ValidationError, match="coded reason"):
        decide(pipeline, cf, fid, outcome="defer")
    with pytest.raises(ValidationError, match="re-presentment"):
        decide(
            pipeline, cf, fid, outcome="defer",
            deferral_reasons=["awaiting_required_documents"],
        )
    with pytest.raises(ValidationError, match=DEFERRAL_REASONS_VERSION):
        decide(
            pipeline, cf, fid, outcome="defer",
            deferral_reasons=["when_ready"],
            representment_requirements=["chk-03 on file"],
        )
    assert pipeline.store.list_committee_decisions(cf) == []


def test_defer_then_represent_then_decide_on_the_same_artefact():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    memos_before = len(pipeline.store.list_memos(cf))
    deferral = decide(
        pipeline, cf, fid,
        outcome="defer",
        deferral_reasons=["awaiting_required_documents"],
        representment_requirements=[
            "chk-03 (Business tax returns) on file",
            "chk-07 (Business debt schedule) on file",
        ],
    )
    assert deferral.deferral_vocabulary == DEFERRAL_REASONS_VERSION
    assert deferral.representment_requirements[0].startswith("chk-03")
    # Deferred: back with the officer — not a draft, no new artefact.
    assert pipeline.memo_status(cf, fid) == "deferred"
    assert len(pipeline.store.list_memos(cf)) == memos_before
    # Deciding again without re-convening is refused.
    with pytest.raises(ValidationError):
        decide(pipeline, cf, fid)
    # Re-presentment: convene again on the same artefact, decide.
    pipeline.convene_committee(
        cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE
    )
    assert pipeline.memo_status(cf, fid) == "in_committee"
    decision = decide(
        pipeline, cf, fid,
        outcome="approve_with_conditions", conditions=[REPORTING],
    )
    assert pipeline.memo_status(cf, fid) == "committee_approved_conditional"
    decisions = pipeline.list_committee_decisions(cf, fid)
    assert [d.outcome for d in decisions] == [
        "defer", "approve_with_conditions",
    ]
    assert decisions[1].decision_id == decision.decision_id


# ---------------------------------------------------------------------
# Storage + migration
# ---------------------------------------------------------------------


def test_sqlite_store_round_trips_committee_records(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    pipeline = hartline_pipeline(store=store)
    credit_file, draft, final = officer_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    pipeline.convene_committee(
        cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE
    )
    decision = decide(
        pipeline, cf, fid, outcome="decline",
        adverse_reasons=[DOCS_REASON, INFO_REASON],
    )
    reopened = SqliteCreditStore(tmp_path / "credit.db")
    pipeline2 = CreditPipeline(store=reopened, documents=LocalDocumentStore())
    assert pipeline2.memo_status(cf, fid) == "declined"
    stored = reopened.get_committee_decision(decision.decision_id)
    assert stored is not None and stored.outcome == "decline"
    assert [r.code for r in stored.adverse_reasons] == [
        "required_documentation_missing",
        "financial_information_insufficient",
    ]
    notice = pipeline2.get_adverse_action_notice(cf, fid)
    assert "NOTICE OF ACTION TAKEN" in notice


def test_migration_0007_is_registered():
    names = [f.name for f in migration_files(MIGRATIONS_DIR)]
    assert "0007_committee_gate.sql" in names
    sql = (MIGRATIONS_DIR / "0007_committee_gate.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS committee_decisions" in sql


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


def _officer_final_via_api(client) -> tuple[str, str]:
    resp = client.post(
        "/applications/from-los", json={"application_ref": "LOS-2026-0117"}
    )
    assert resp.status_code == 201
    cf = resp.json()["credit_file_id"]
    memos = client.get(f"/applications/{cf}/memos").json()
    draft_id = memos[0]["memo_id"]
    base = f"/applications/{cf}/memos/{draft_id}"
    client.post(f"{base}/submit", json={"actor": RM, "actor_role": RM_ROLE})
    approved = client.post(
        f"{base}/approve",
        json={"officer_id": OFFICER, "officer_role": OFFICER_ROLE},
    )
    assert approved.status_code == 201
    return cf, approved.json()["memo_id"]


MEMBERS_JSON = [
    {"member_id": CHAIR, "member_role": CHAIR_ROLE},
    {"member_id": MEMBER, "member_role": MEMBER_ROLE},
]


def test_api_committee_approve_flow(client):
    cf, fid = _officer_final_via_api(client)
    base = f"/applications/{cf}/memos/{fid}/committee"

    early = client.post(
        f"{base}/decide",
        json={
            "recorder_id": CHAIR, "recorder_role": CHAIR_ROLE,
            "members": MEMBERS_JSON, "outcome": "approve",
        },
    )
    assert early.status_code == 422  # not convened yet

    convened = client.post(
        f"{base}/convene",
        json={"actor": OFFICER, "actor_role": OFFICER_ROLE},
    )
    assert convened.status_code == 200
    assert convened.json()["status"] == "in_committee"

    solo = client.post(
        f"{base}/decide",
        json={
            "recorder_id": CHAIR, "recorder_role": CHAIR_ROLE,
            "members": MEMBERS_JSON[:1], "outcome": "approve",
        },
    )
    assert solo.status_code == 422  # a committee of one

    decided = client.post(
        f"{base}/decide",
        json={
            "recorder_id": CHAIR, "recorder_role": CHAIR_ROLE,
            "members": MEMBERS_JSON, "outcome": "approve",
        },
    )
    assert decided.status_code == 201
    decision = decided.json()
    assert decision["outcome"] == "approve"
    assert decision["quorum_required"] == 2
    assert [m["member_id"] for m in decision["members"]] == [CHAIR, MEMBER]

    state = client.get(base)
    assert state.status_code == 200
    assert state.json()["status"] == "committee_approved"
    assert state.json()["latest_decision"]["decision_id"] == (
        decision["decision_id"]
    )

    fetched = client.get(f"{base}/decisions/{decision['decision_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["memo_id"] == fid

    history = client.get(f"/applications/{cf}/memos/{fid}/history")
    body = history.json()
    assert body["status"] == "committee_approved"
    assert [d["decision_id"] for d in body["committee_decisions"]] == [
        decision["decision_id"]
    ]
    assert [e["action"] for e in body["events"]][-2:] == [
        "committee_convened", "committee_decided",
    ]

    text = client.get(f"/applications/{cf}/memos/{fid}/text")
    assert "COMMITTEE APPROVED" in text.json()["text"]


def test_api_committee_decline_and_notice_flow(client):
    cf, fid = _officer_final_via_api(client)
    base = f"/applications/{cf}/memos/{fid}/committee"
    client.post(
        f"{base}/convene",
        json={"actor": OFFICER, "actor_role": OFFICER_ROLE},
    )

    bare = client.post(
        f"{base}/decide",
        json={
            "recorder_id": CHAIR, "recorder_role": CHAIR_ROLE,
            "members": MEMBERS_JSON, "outcome": "decline",
            "note": "Not comfortable.",
        },
    )
    assert bare.status_code == 422  # no coded reasons, no decline

    passed_test = client.post(
        f"{base}/decide",
        json={
            "recorder_id": CHAIR, "recorder_role": CHAIR_ROLE,
            "members": MEMBERS_JSON, "outcome": "decline",
            "adverse_reasons": [{
                "code": "leverage_above_policy_limit",
                "tie_kind": "policy_test",
                "test_id": "max_leverage_by_segment",
            }],
        },
    )
    assert passed_test.status_code == 422
    # The offender is named. (On the API's from-LOS path the memo
    # pins the current-version report over a document-less intake,
    # where leverage is not_evaluable rather than passed — either
    # way the reason is refused: the test it cites did not fail.)
    assert "max_leverage_by_segment" in passed_test.json()["detail"]

    declined = client.post(
        f"{base}/decide",
        json={
            "recorder_id": CHAIR, "recorder_role": CHAIR_ROLE,
            "members": MEMBERS_JSON, "outcome": "decline",
            "adverse_reasons": [
                {
                    "code": "required_documentation_missing",
                    "tie_kind": "file_fact",
                    "checklist_item_id": "chk-03",
                },
                {
                    "code": "financial_information_insufficient",
                    "tie_kind": "policy_test",
                    "test_id": "min_dscr",
                },
            ],
        },
    )
    assert declined.status_code == 201
    assert declined.json()["reason_vocabulary"] == ADVERSE_REASONS_VERSION

    notice = client.get(f"{base}/adverse-action-notice")
    assert notice.status_code == 200
    body = notice.json()
    assert body["decision_id"] == declined.json()["decision_id"]
    assert "NOTICE OF ACTION TAKEN" in body["text"]
    assert "chk-03 (Business tax returns) is not on file" in body["text"]

    state = client.get(base)
    assert state.json()["status"] == "declined"
