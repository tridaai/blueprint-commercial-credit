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

Fixtures: since round 9 a memo reaches the committee only on a
committee-ready file (design law 7) — every required document on
file and fresh, every figure computable — so the committee
fixtures below use a purpose-built borrower, Calloway Precision
Manufacturing, whose file is complete by construction. Its
hand-decided anchors under the pinned v1 report (as of
2026-06-30): leverage FAILS at 5.0000 against the 2.00
manufacturing ceiling (the decline anchor); DSCR passes at 1.3125
(EBITDA $420,000 over debt service $320,000); global DSCR cannot
be evaluated (the facility names no guarantors); working capital
is $900,000.00 in FY2023 and $1,000,000.00 in FY2025, traced in
the memo's verification record. The readiness gate's own refusals — including what it
does to an incomplete Hartline file — are graded in
tests/test_readiness.py.
"""

import json
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
    Borrower,
    CommitteeCondition,
    CommitteeMember,
    DocumentKind,
    FacilityRequest,
    FacilityType,
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


# ---------------------------------------------------------------------
# The committee-ready borrower (round 9 fixture). Statements carry
# all three sections per year, so every figure computes; the
# checklist is satisfied in full; the one designed weakness is
# leverage — a credit judgment for the committee, not a readiness
# blocker.
# ---------------------------------------------------------------------

_BS_FIELDS = (
    "cash_and_equivalents", "accounts_receivable", "inventory",
    "total_current_assets", "net_fixed_assets", "total_assets",
    "accounts_payable", "current_portion_ltd",
    "total_current_liabilities", "long_term_debt",
    "total_liabilities", "total_equity",
)
_IS_FIELDS = (
    "revenue", "cost_of_goods_sold", "gross_profit",
    "operating_expenses", "ebit", "interest_expense", "net_income",
)
_CF_FIELDS = (
    "depreciation_and_amortization", "change_in_receivables",
    "change_in_inventory", "change_in_payables",
    "net_cash_from_operations", "capital_expenditures",
    "net_cash_from_investing", "debt_proceeds", "debt_repayments",
    "net_cash_from_financing", "net_change_in_cash",
)

# Hand-set so the balance sheet foots every year, FY2025 EBITDA is
# $420,000 (EBIT $300,000 + D&A $120,000), FY2025 debt service is
# $320,000 (FY2024 CPLTD $220,000 + interest $100,000), and FY2025
# leverage is exactly 5.0000.
CALLOWAY_FIGURES: dict[int, dict[str, int]] = {
    2023: dict(zip(_BS_FIELDS, (
        200000, 800000, 1000000, 2000000, 3000000, 5000000,
        650000, 200000, 1100000, 2900000, 4000000, 1000000,
    )), **dict(zip(_IS_FIELDS, (
        7500000, 5250000, 2250000, 1970000, 280000, 90000, 145000,
    ))), **dict(zip(_CF_FIELDS, (
        100000, -40000, -50000, 30000, 300000, 50000, -50000,
        50000, -180000, -130000, 120000,
    )))),
    2024: dict(zip(_BS_FIELDS, (
        250000, 840000, 1110000, 2200000, 3300000, 5500000,
        700000, 220000, 1200000, 3300000, 4500000, 1000000,
    )), **dict(zip(_IS_FIELDS, (
        8200000, 5740000, 2460000, 2140000, 320000, 95000, 170000,
    ))), **dict(zip(_CF_FIELDS, (
        110000, -40000, -110000, 50000, 350000, 55000, -55000,
        80000, -200000, -120000, 175000,
    )))),
    2025: dict(zip(_BS_FIELDS, (
        300000, 900000, 1200000, 2400000, 3600000, 6000000,
        800000, 240000, 1400000, 3600000, 5000000, 1000000,
    )), **dict(zip(_IS_FIELDS, (
        9000000, 6300000, 2700000, 2400000, 300000, 100000, 150000,
    ))), **dict(zip(_CF_FIELDS, (
        120000, -60000, -90000, 100000, 380000, 60000, -60000,
        100000, -220000, -120000, 200000,
    )))),
}


def calloway_statement_document(year: int) -> bytes:
    """One fiscal year in the reference extractor's document shape:
    label-keyed lines on three pages (balance sheet, income
    statement, cash-flow statement) — the cash-flow page is what
    Hartline never had, and why its DSCR could not compute."""
    from commercial_credit.spreading import TEMPLATE_BY_FIELD

    figures = CALLOWAY_FIGURES[year]

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
        "borrower": "Calloway Precision Manufacturing, Inc.",
        "fiscal_year": year,
        "period_label": f"FY{year}",
        "basis": "audited",
        "pages": [
            page(1, "Balance Sheet", _BS_FIELDS),
            page(2, "Income Statement", _IS_FIELDS),
            page(3, "Cash Flow Statement", _CF_FIELDS),
        ],
    }
    return json.dumps(payload, indent=2).encode()


def calloway_tax_document() -> bytes:
    """The FY2025 business tax return: income lines exactly as the
    audited statements report them, so round 8 records no
    disagreement on this file."""
    from commercial_credit.spreading import TEMPLATE_BY_FIELD

    figures = CALLOWAY_FIGURES[2025]
    payload = {
        "document_type": "tax_return",
        "borrower": "Calloway Precision Manufacturing, Inc.",
        "fiscal_year": 2025,
        "period_label": "FY2025",
        "pages": [{
            "page": 1,
            "title": "Income Statement (as filed)",
            "lines": [
                {
                    "line": idx,
                    "label": TEMPLATE_BY_FIELD[field].label,
                    "value": figures[field],
                }
                for idx, field in enumerate(_IS_FIELDS, start=1)
            ],
        }],
    }
    return json.dumps(payload, indent=2).encode()


def calloway_borrower() -> Borrower:
    return Borrower(
        legal_name="Calloway Precision Manufacturing, Inc.",
        ein_last4="4417",
        segment="manufacturing",
        state_of_formation="NC",
        years_in_business=14,
        primary_contact_name="Dana Whitfield",
        primary_contact_email="dana.whitfield@example.com",
    )


def calloway_facility() -> FacilityRequest:
    return FacilityRequest(
        facility_type=FacilityType.TERM_LOAN,
        amount=Decimal("1500000"),
        purpose="CNC machining line expansion",
        term_months=60,
        collateral_description="Machining equipment and receivables",
        guarantors=[],
    )


def calloway_documents() -> list[dict]:
    docs = [
        {
            "kind": DocumentKind.FINANCIAL_STATEMENT.value,
            "filename": f"calloway-fy{year}-statements.json",
            "data_hex": calloway_statement_document(year).hex(),
            "period_label": f"FY{year}",
        }
        for year in (2023, 2024, 2025)
    ]
    docs.append({
        "kind": DocumentKind.TAX_RETURN.value,
        "filename": "calloway-business-tax-fy2025.json",
        "data_hex": calloway_tax_document().hex(),
        "period_label": "FY2025",
    })
    docs.append({
        "kind": DocumentKind.TAX_RETURN.value,
        "filename": "guarantor-personal-tax.json",
        "data_hex": json.dumps(
            {"document_type": "tax_return", "pages": [{"page": 1, "lines": []}]}
        ).encode().hex(),
        "period_label": "FY2025",
    })
    for kind, filename in (
        (DocumentKind.PERSONAL_FINANCIAL_STATEMENT, "guarantor-pfs.json"),
        (DocumentKind.ORG_DOCUMENT, "articles-of-incorporation.json"),
        (DocumentKind.DEBT_SCHEDULE, "debt-schedule.json"),
        (DocumentKind.INSURANCE_CERTIFICATE, "insurance-certificate.json"),
        (DocumentKind.PROJECTIONS, "projections-fy2026.json"),
    ):
        docs.append({
            "kind": kind.value,
            "filename": filename,
            "data_hex": json.dumps({}).encode().hex(),
        })
    return docs


def open_ready_file(pipeline: CreditPipeline):
    return pipeline.open_application(
        calloway_borrower(), calloway_facility(),
        documents=calloway_documents(),
    )


def officer_final(pipeline: CreditPipeline):
    """The Calloway officer-final memo under the pinned v1 report —
    a committee-ready file (round 9) whose leverage fails policy."""
    credit_file = open_ready_file(pipeline)
    report = pipeline.evaluate_policy(
        credit_file.credit_file_id, pack_version="v1", as_of=date(2026, 6, 30)
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
    checklist_item_id="chk-03",  # business tax returns
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

LEVERAGE_REASON = AdverseActionReason(
    code="leverage_above_policy_limit",
    tie_kind="policy_test",
    test_id="max_leverage_by_segment",
)
INFO_REASON = AdverseActionReason(
    code="financial_information_insufficient",
    tie_kind="policy_test",
    test_id="min_global_dscr",
)


def test_decline_with_tied_reasons_lands_and_renders_the_notice():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    decision = decide(
        pipeline, cf, fid,
        outcome="decline",
        adverse_reasons=[LEVERAGE_REASON, INFO_REASON],
    )
    assert pipeline.memo_status(cf, fid) == "declined"
    assert decision.reason_vocabulary == ADVERSE_REASONS_VERSION
    assert [r.code for r in decision.adverse_reasons] == [
        "leverage_above_policy_limit",
        "financial_information_insufficient",
    ]
    notice = pipeline.get_adverse_action_notice(cf, fid)
    assert "NOTICE OF ACTION TAKEN" in notice
    assert "Calloway Precision Manufacturing" in notice
    assert (
        "Leverage exceeds the bank's policy limit for the "
        "borrower's segment" in notice
    )
    assert "Maximum leverage by borrower segment" in notice
    assert "5.0000" in notice
    assert (
        "The financial information provided does not allow the bank "
        "to evaluate the application" in notice
    )
    assert "names no guarantors" in notice
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
            adverse_reasons=[LEVERAGE_REASON],
        )
    assert pipeline.store.list_committee_decisions(cf) == []


def test_a_reason_citing_a_passed_test_is_refused_and_named():
    pipeline = hartline_pipeline()
    credit_file, draft, final = convened_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    # DSCR PASSED at 1.3125 against the 1.25 floor.
    passed = AdverseActionReason(
        code="debt_service_coverage_below_policy",
        tie_kind="policy_test",
        test_id="min_dscr",
    )
    with pytest.raises(ValidationError, match="PASS"):
        decide(
            pipeline, cf, fid, outcome="decline",
            adverse_reasons=[passed],
        )
    assert "min_dscr" in str(
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
    # Global DSCR could not be evaluated: a coverage FAILURE claim
    # over a not_evaluable test is inaccurate — refused by name.
    miscast = AdverseActionReason(
        code="debt_service_coverage_below_policy",
        tie_kind="policy_test",
        test_id="min_global_dscr",
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
        if t.text == "$900,000.00" and "working_capital" in t.source
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
    assert decision.adverse_reasons[0].figure_text == "$900,000.00"
    notice = pipeline.get_adverse_action_notice(cf, fid)
    assert "$900,000.00" in notice
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
        adverse_reasons=[LEVERAGE_REASON, INFO_REASON],
    )
    reopened = SqliteCreditStore(tmp_path / "credit.db")
    pipeline2 = CreditPipeline(store=reopened, documents=LocalDocumentStore())
    assert pipeline2.memo_status(cf, fid) == "declined"
    stored = reopened.get_committee_decision(decision.decision_id)
    assert stored is not None and stored.outcome == "decline"
    assert [r.code for r in stored.adverse_reasons] == [
        "leverage_above_policy_limit",
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
    """The Calloway file, built end-to-end through the API: open,
    register every document, extract, spread, compute, evaluate
    under v1, compose, submit, approve. (Since round 9 the
    committee can only be reached on a committee-ready file, so
    the API committee tests build one.)"""
    import base64

    resp = client.post(
        "/applications",
        json={
            "borrower": calloway_borrower().model_dump(mode="json"),
            "facility": calloway_facility().model_dump(mode="json"),
        },
    )
    assert resp.status_code == 201
    cf = resp.json()["credit_file_id"]
    for doc in calloway_documents():
        data = bytes.fromhex(doc["data_hex"])
        posted = client.post(
            f"/applications/{cf}/documents",
            json={
                "kind": doc["kind"],
                "filename": doc["filename"],
                "content_b64": base64.b64encode(data).decode("ascii"),
                "period_label": doc.get("period_label"),
            },
        )
        assert posted.status_code == 201, posted.text
    assert client.post(f"/applications/{cf}/extract").status_code == 200
    assert client.get(f"/applications/{cf}/spread").status_code == 200
    assert client.post(
        f"/applications/{cf}/computations", json={}
    ).status_code == 201
    evaluated = client.post(
        f"/applications/{cf}/policy-evaluations",
        json={"pack_version": "v1", "as_of": "2026-06-30"},
    )
    assert evaluated.status_code == 201, evaluated.text
    composed = client.post(
        f"/applications/{cf}/memos",
        json={"report_id": evaluated.json()["report_id"]},
    )
    assert composed.status_code == 201, composed.text
    draft_id = composed.json()["memo_id"]
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
                "code": "debt_service_coverage_below_policy",
                "tie_kind": "policy_test",
                "test_id": "min_dscr",
            }],
        },
    )
    assert passed_test.status_code == 422
    # The offender is named: DSCR passed at 1.3125, so a coverage
    # decline reason citing it is refused.
    assert "min_dscr" in passed_test.json()["detail"]

    declined = client.post(
        f"{base}/decide",
        json={
            "recorder_id": CHAIR, "recorder_role": CHAIR_ROLE,
            "members": MEMBERS_JSON, "outcome": "decline",
            "adverse_reasons": [
                {
                    "code": "leverage_above_policy_limit",
                    "tie_kind": "policy_test",
                    "test_id": "max_leverage_by_segment",
                },
                {
                    "code": "financial_information_insufficient",
                    "tie_kind": "policy_test",
                    "test_id": "min_global_dscr",
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
    assert "Leverage exceeds the bank's policy limit" in body["text"]
    assert "5.0000" in body["text"]

    state = client.get(base)
    assert state.json()["status"] == "declined"
