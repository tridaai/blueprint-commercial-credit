"""Round 6: the credit officer gate + diff recording.

The gate's discipline is graded the way the verifier was in
round 5 — on refusals as much as on landings:

  anonymous or role-less action      refused, always
  approve / return / flag a memo
    that is not in officer review    refused (illegal transition)
  an edit that introduces a figure
    the verifier cannot trace        refused, nothing stored
  an edit to the exceptions section  refused — the section renders
                                       the pinned report verbatim
  a return without coded reasons     refused (free text is not a
                                       reason)
  an officer approving a memo they
    declared a conflict on           refused, by name of the flag

And the landings: approval as-is mints an officer-final memo with
an empty diff; approval with clean edits records the section and
figure changes, each figure with its trace on both sides; a
return → recompose → resubmit → approve path completes, with the
lineage readable as version history.

Hand-decided anchors (from rounds 3–5): Hartline's facility is
$1,500,000.00 (a stated credit-file fact, so it traces); FY2024
working capital is $2,202,000.00 and total debt $3,120,000.00
(computation pack figures, so a swap between them traces and must
surface in the diff as an ALTERED figure); the pinned v1 report
carries five non-pass outcomes, all of which must survive the
gate in the final memo's exceptions.
"""

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.db import migration_files
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.errors import MemoVerificationError, ValidationError
from commercial_credit.memo import render_memo
from commercial_credit.models import DocumentKind
from commercial_credit.officer_gate import RETURN_REASONS_VERSION
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import ReferenceLosFeed, statement_document
from commercial_credit.store import InMemoryCreditStore, SqliteCreditStore

AS_OF_YEAR = 2025

OFFICER = "officer-amara-osei"
OFFICER_ROLE = "credit_officer"
OFFICER_B = "officer-daniel-reyes"
OFFICER_B_ROLE = "senior_credit_officer"
RM = "rm-priya-nair"
RM_ROLE = "relationship_manager"


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


def hartline_draft(pipeline: CreditPipeline):
    """The Hartline draft under the pinned v1 report (five non-pass
    outcomes), as in round 5."""
    from datetime import date

    credit_file = open_hartline(pipeline)
    report = pipeline.evaluate_policy(
        credit_file.credit_file_id, pack_version="v1", as_of=date(2025, 6, 30)
    )
    memo = pipeline.compose_memo(
        credit_file.credit_file_id, report_id=report.report_id
    )
    return credit_file, memo


def submitted_hartline(pipeline: CreditPipeline):
    credit_file, memo = hartline_draft(pipeline)
    pipeline.submit_memo_for_review(
        credit_file.credit_file_id, memo.memo_id, actor=RM, actor_role=RM_ROLE
    )
    return credit_file, memo


# ---------------------------------------------------------------------
# Identity: nobody anonymous decides anything
# ---------------------------------------------------------------------


def test_anonymous_or_roleless_actions_are_refused():
    pipeline = hartline_pipeline()
    credit_file, memo = hartline_draft(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    with pytest.raises(ValidationError):
        pipeline.submit_memo_for_review(cf, mid, actor="", actor_role=RM_ROLE)
    with pytest.raises(ValidationError):
        pipeline.submit_memo_for_review(cf, mid, actor=RM, actor_role="  ")
    with pytest.raises(ValidationError):
        pipeline.approve_memo(cf, mid, officer_id="", officer_role=OFFICER_ROLE)
    with pytest.raises(ValidationError):
        pipeline.return_memo(
            cf, mid, officer_id=OFFICER, officer_role="",
            reason_codes=["insufficient_analysis"],
        )
    with pytest.raises(ValidationError):
        pipeline.flag_memo(
            cf, mid, officer_id=" ", officer_role=OFFICER_ROLE,
            kind="committee_attention",
        )
    with pytest.raises(ValidationError):
        pipeline.recompose_memo(
            cf, supersedes_memo_id=mid, actor="", actor_role=RM_ROLE
        )
    # Nothing was recorded: the memo is still a plain draft.
    assert pipeline.memo_status(cf, mid) == "draft"
    assert pipeline.store.list_memo_review_events(cf) == []


def test_a_non_officer_role_cannot_decide():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    with pytest.raises(ValidationError, match="credit officer role"):
        pipeline.approve_memo(cf, mid, officer_id=RM, officer_role=RM_ROLE)
    with pytest.raises(ValidationError, match="credit officer role"):
        pipeline.return_memo(
            cf, mid, officer_id=RM, officer_role=RM_ROLE,
            reason_codes=["insufficient_analysis"],
        )
    assert pipeline.memo_status(cf, mid) == "in_officer_review"


# ---------------------------------------------------------------------
# State discipline
# ---------------------------------------------------------------------


def test_submit_opens_review_once_and_is_audited():
    pipeline = hartline_pipeline()
    credit_file, memo = hartline_draft(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    assert pipeline.memo_status(cf, mid) == "draft"
    submitted = pipeline.submit_memo_for_review(
        cf, mid, actor=RM, actor_role=RM_ROLE
    )
    assert submitted.status == "in_officer_review"
    assert pipeline.memo_status(cf, mid) == "in_officer_review"
    with pytest.raises(ValidationError, match="only a draft"):
        pipeline.submit_memo_for_review(cf, mid, actor=RM, actor_role=RM_ROLE)
    audits = [
        e for e in pipeline.store.list_audit(cf)
        if e.action == "memo_submitted_for_review"
    ]
    assert len(audits) == 1 and audits[0].actor == RM


def test_decisions_on_a_memo_not_in_review_are_refused():
    pipeline = hartline_pipeline()
    credit_file, memo = hartline_draft(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    # Still a draft: approve, return and flag are all illegal.
    with pytest.raises(ValidationError, match="in officer review"):
        pipeline.approve_memo(cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE)
    with pytest.raises(ValidationError, match="in officer review"):
        pipeline.return_memo(
            cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
            reason_codes=["insufficient_analysis"],
        )
    with pytest.raises(ValidationError, match="in officer review"):
        pipeline.flag_memo(
            cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
            kind="committee_attention",
        )
    assert pipeline.memo_status(cf, mid) == "draft"


# ---------------------------------------------------------------------
# Approval as-is: a new artefact, an empty diff, the draft untouched
# ---------------------------------------------------------------------


def test_approve_as_is_mints_the_final_memo_and_an_empty_diff():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    draft_sections = [
        s.model_dump() for s in pipeline.store.get_memo(mid).sections
    ]
    final = pipeline.approve_memo(
        cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE
    )
    assert final.memo_id != mid
    assert final.status == "officer_approved"
    assert final.supersedes_memo_id == mid
    assert final.reviewed_by == OFFICER
    assert final.verification.verified is True
    # The draft artefact was never edited in place, and its derived
    # status now reads approved through the event stream.
    stored_draft = pipeline.store.get_memo(mid)
    assert [s.model_dump() for s in stored_draft.sections] == draft_sections
    assert pipeline.memo_status(cf, mid) == "officer_approved"
    assert pipeline.get_memo(cf, mid).status == "officer_approved"
    # The diff exists and records that nothing changed.
    diff = pipeline.get_memo_diff(cf, final.memo_id)
    assert diff.from_memo_id == mid and diff.to_memo_id == final.memo_id
    assert diff.action == "approved_as_is"
    assert diff.officer_id == OFFICER and diff.officer_role == OFFICER_ROLE
    assert diff.section_changes == [] and diff.figure_changes == []
    # The final memo renders as what it is.
    text = render_memo(final)
    assert "OFFICER APPROVED" in text and "committee-ready" in text
    assert f"Reviewed by: `{OFFICER}`" in text
    # The gate actions are on the audit trail, under the officer.
    actions = {
        (e.action, e.actor) for e in pipeline.store.list_audit(cf)
    }
    assert ("memo_approved", OFFICER) in actions


# ---------------------------------------------------------------------
# Approval with edits: verified again, diffed, exceptions intact
# ---------------------------------------------------------------------


def test_approve_with_clean_edits_records_section_and_figure_changes():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    final = pipeline.approve_memo(
        cf,
        mid,
        officer_id=OFFICER,
        officer_role=OFFICER_ROLE,
        edits={
            "recommendation": [
                "Recommend approval of the $1,500,000.00 term "
                "facility, subject to the conditions recorded below."
            ],
            "conditions": [
                "Approval is conditional on every outstanding "
                "required item being filed before closing."
            ],
        },
    )
    assert final.status == "officer_approved"
    assert final.section("recommendation").paragraphs == [
        "Recommend approval of the $1,500,000.00 term facility, "
        "subject to the conditions recorded below."
    ]
    diff = pipeline.get_memo_diff(cf, final.memo_id)
    assert diff.action == "approved_with_edits"
    changed_sections = {c.section_key for c in diff.section_changes}
    assert changed_sections == {"recommendation", "conditions"}
    added = [c for c in diff.figure_changes if c.kind == "added"]
    assert any(
        c.section_key == "recommendation"
        and c.new_text == "$1,500,000.00"
        and "facility.amount" in (c.new_trace or "")
        for c in added
    )
    # The placeholder conditions carried the outstanding count (7);
    # replacing the text removes that figure, and the diff says so.
    removed = [c for c in diff.figure_changes if c.kind == "removed"]
    assert any(
        c.section_key == "conditions" and c.old_text == "7"
        for c in removed
    )
    # All five pinned exceptions survive the gate verbatim.
    report = pipeline.store.get_policy_report(final.policy_report_id)
    assert final.section("policy_exceptions").exception_test_ids == [
        o.test_id for o in report.non_pass_outcomes()
    ]
    assert len(final.section("policy_exceptions").exception_test_ids) == 5


def test_an_edit_swapping_one_traced_figure_for_another_is_an_altered_change():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    section = memo.section("financial_analysis")
    paragraphs = list(section.paragraphs)
    idx = next(
        i for i, p in enumerate(paragraphs) if "$2,202,000.00" in p
    )
    paragraphs[idx] = paragraphs[idx].replace(
        "$2,202,000.00", "$3,120,000.00"
    )
    final = pipeline.approve_memo(
        cf,
        mid,
        officer_id=OFFICER,
        officer_role=OFFICER_ROLE,
        edits={"financial_analysis": paragraphs},
    )
    diff = pipeline.get_memo_diff(cf, final.memo_id)
    altered = [c for c in diff.figure_changes if c.kind == "altered"]
    assert any(
        c.section_key == "financial_analysis"
        and c.old_text == "$2,202,000.00"
        and "working_capital" in (c.old_trace or "")
        and c.new_text == "$3,120,000.00"
        and "total_debt" in (c.new_trace or "")
        for c in altered
    )


def test_an_edit_with_an_untraceable_figure_is_refused_and_stores_nothing():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    memos_before = pipeline.store.list_memos(cf)
    with pytest.raises(MemoVerificationError) as caught:
        pipeline.approve_memo(
            cf,
            mid,
            officer_id=OFFICER,
            officer_role=OFFICER_ROLE,
            edits={
                "recommendation": [
                    "Recommend approval; the borrower also holds "
                    "$8,888,888.88 in unencumbered reserves."
                ]
            },
        )
    assert "$8,888,888.88" in str(caught.value)
    # Nothing stored: no final memo, no diff, no approval event —
    # and the memo is still in review, decidable by a clean action.
    assert pipeline.store.list_memos(cf) == memos_before
    assert pipeline.store.list_memo_diffs(cf) == []
    assert pipeline.memo_status(cf, mid) == "in_officer_review"
    final = pipeline.approve_memo(
        cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE
    )
    assert final.status == "officer_approved"


def test_the_exceptions_section_is_locked_at_the_gate():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    # Dropping the exceptions wholesale...
    with pytest.raises(ValidationError, match="not editable"):
        pipeline.approve_memo(
            cf,
            mid,
            officer_id=OFFICER,
            officer_role=OFFICER_ROLE,
            edits={"policy_exceptions": ["No exceptions."]},
        )
    # ...or softening one not_evaluable paragraph — both are edits
    # to a section the gate does not let an officer touch.
    softened = [
        p.replace("NOT EVALUABLE", "PASS")
        for p in memo.section("policy_exceptions").paragraphs
    ]
    with pytest.raises(ValidationError, match="not editable"):
        pipeline.approve_memo(
            cf,
            mid,
            officer_id=OFFICER,
            officer_role=OFFICER_ROLE,
            edits={"policy_exceptions": softened},
        )
    assert pipeline.store.list_memo_diffs(cf) == []
    assert pipeline.memo_status(cf, mid) == "in_officer_review"


def test_an_edit_to_an_unknown_section_is_refused():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    with pytest.raises(ValidationError, match="does not have"):
        pipeline.approve_memo(
            credit_file.credit_file_id,
            memo.memo_id,
            officer_id=OFFICER,
            officer_role=OFFICER_ROLE,
            edits={"management_discussion": ["New section."]},
        )


# ---------------------------------------------------------------------
# Returns: coded reasons or no return; then recomposition
# ---------------------------------------------------------------------


def test_a_return_requires_coded_reasons():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    # A note alone is a free-text return — refused.
    with pytest.raises(ValidationError, match="coded"):
        pipeline.return_memo(
            cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
            reason_codes=[], note="Needs more work.",
        )
    # A code outside the vocabulary — refused, the version named.
    with pytest.raises(ValidationError, match=RETURN_REASONS_VERSION):
        pipeline.return_memo(
            cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
            reason_codes=["vibes"],
        )
    # 'other' without a note is a free-text return in disguise.
    with pytest.raises(ValidationError, match="'other'"):
        pipeline.return_memo(
            cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
            reason_codes=["other"],
        )
    assert pipeline.memo_status(cf, mid) == "in_officer_review"

    returned = pipeline.return_memo(
        cf,
        mid,
        officer_id=OFFICER,
        officer_role=OFFICER_ROLE,
        reason_codes=["insufficient_analysis", "conditions_incomplete"],
        note="Coverage analysis and conditions need rework.",
    )
    assert returned.status == "returned"
    events = pipeline.store.list_memo_review_events(cf, mid)
    event = next(e for e in events if e.action == "returned_for_rework")
    assert event.reason_codes == [
        "insufficient_analysis", "conditions_incomplete",
    ]
    assert event.reason_vocabulary == RETURN_REASONS_VERSION
    assert event.actor_id == OFFICER


def test_return_recompose_resubmit_approve_and_the_officer_may_return():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    pipeline.return_memo(
        cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
        reason_codes=["insufficient_analysis"],
    )
    # A returned memo cannot be approved or resubmitted as-is.
    with pytest.raises(ValidationError, match="in officer review"):
        pipeline.approve_memo(
            cf, mid, officer_id=OFFICER_B, officer_role=OFFICER_B_ROLE
        )
    with pytest.raises(ValidationError, match="only a draft"):
        pipeline.submit_memo_for_review(cf, mid, actor=RM, actor_role=RM_ROLE)
    # Recomposition produces a NEW draft version naming the old one.
    draft2 = pipeline.recompose_memo(
        cf, supersedes_memo_id=mid, actor=RM, actor_role=RM_ROLE
    )
    assert draft2.memo_id != mid
    assert draft2.supersedes_memo_id == mid
    assert draft2.status == "draft"
    assert pipeline.memo_status(cf, draft2.memo_id) == "draft"
    pipeline.submit_memo_for_review(
        cf, draft2.memo_id, actor=RM, actor_role=RM_ROLE
    )
    # The officer who returned the original may decide the
    # recomposition — a return is not a recusal.
    final = pipeline.approve_memo(
        cf, draft2.memo_id, officer_id=OFFICER, officer_role=OFFICER_ROLE
    )
    assert final.status == "officer_approved"
    # The lineage reads end to end.
    history = pipeline.memo_history(cf, final.memo_id)
    assert [v["memo_id"] for v in history["versions"]] == [
        mid, draft2.memo_id, final.memo_id,
    ]
    assert [v["status"] for v in history["versions"]] == [
        "returned", "officer_approved", "officer_approved",
    ]
    event_actions = [e.action for e in history["events"]]
    assert event_actions == ["approved_as_is"]
    draft2_history = pipeline.memo_history(cf, draft2.memo_id)
    assert [e.action for e in draft2_history["events"]] == [
        "recomposed", "submitted_for_review", "approved_as_is",
    ]
    assert draft2_history["diff"] is None
    final_history = pipeline.memo_history(cf, final.memo_id)
    assert final_history["diff"] is not None
    assert final_history["diff"].from_memo_id == draft2.memo_id


def test_recompose_of_a_memo_that_was_not_returned_is_refused():
    pipeline = hartline_pipeline()
    credit_file, memo = hartline_draft(pipeline)
    with pytest.raises(ValidationError, match="only a returned memo"):
        pipeline.recompose_memo(
            credit_file.credit_file_id,
            supersedes_memo_id=memo.memo_id,
            actor=RM,
            actor_role=RM_ROLE,
        )


# ---------------------------------------------------------------------
# Flags: committee attention carried, conflicts recuse
# ---------------------------------------------------------------------


def test_committee_attention_flags_are_carried_onto_the_final_memo():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    event = pipeline.flag_memo(
        cf,
        mid,
        officer_id=OFFICER_B,
        officer_role=OFFICER_B_ROLE,
        kind="committee_attention",
        section_key="policy_exceptions",
        note="Five open exceptions; the committee should see the DSCR gap first.",
    )
    assert event.flag is not None and event.flag.kind == "committee_attention"
    # A flag does not move the status or the approval path.
    assert pipeline.memo_status(cf, mid) == "in_officer_review"
    final = pipeline.approve_memo(
        cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE
    )
    assert len(final.committee_flags) == 1
    flag = final.committee_flags[0]
    assert flag.kind == "committee_attention"
    assert flag.section_key == "policy_exceptions"
    assert flag.officer_id == OFFICER_B


def test_a_declared_conflict_recuses_that_officer_only():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    pipeline.flag_memo(
        cf,
        mid,
        officer_id=OFFICER,
        officer_role=OFFICER_ROLE,
        kind="conflict_of_interest",
        note="I advised this borrower before joining the bank.",
    )
    with pytest.raises(ValidationError, match="conflict of interest"):
        pipeline.approve_memo(
            cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE
        )
    # A different officer decides — and the flag rides along so the
    # committee sees the recusal on the record.
    final = pipeline.approve_memo(
        cf, mid, officer_id=OFFICER_B, officer_role=OFFICER_B_ROLE
    )
    assert final.reviewed_by == OFFICER_B
    assert [f.kind for f in final.committee_flags] == ["conflict_of_interest"]
    assert final.committee_flags[0].officer_id == OFFICER


def test_flag_validation():
    pipeline = hartline_pipeline()
    credit_file, memo = submitted_hartline(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    with pytest.raises(ValidationError, match="unknown flag kind"):
        pipeline.flag_memo(
            cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
            kind="fyi",
        )
    with pytest.raises(ValidationError, match="does not have"):
        pipeline.flag_memo(
            cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
            kind="committee_attention", section_key="no_such_section",
        )


# ---------------------------------------------------------------------
# Storage + migration
# ---------------------------------------------------------------------


def test_sqlite_store_round_trips_the_gate_records(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    pipeline = hartline_pipeline(store=store)
    credit_file, memo = hartline_draft(pipeline)
    cf, mid = credit_file.credit_file_id, memo.memo_id
    pipeline.submit_memo_for_review(cf, mid, actor=RM, actor_role=RM_ROLE)
    final = pipeline.approve_memo(
        cf, mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
        edits={
            "recommendation": [
                "Recommend approval of the $1,500,000.00 term facility."
            ]
        },
    )
    reopened = SqliteCreditStore(tmp_path / "credit.db")
    pipeline2 = CreditPipeline(
        store=reopened, documents=LocalDocumentStore()
    )
    assert pipeline2.memo_status(cf, mid) == "officer_approved"
    diff = reopened.get_memo_diff_by_final(final.memo_id)
    assert diff is not None and diff.from_memo_id == mid
    events = reopened.list_memo_review_events(cf, mid)
    assert [e.action for e in events] == [
        "submitted_for_review", "approved_with_edits",
    ]
    assert reopened.get_memo_diff(diff.diff_id).diff_id == diff.diff_id


def test_migration_0006_is_registered():
    names = [f.name for f in migration_files(MIGRATIONS_DIR)]
    assert "0006_officer_gate.sql" in names
    sql = (MIGRATIONS_DIR / "0006_officer_gate.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS memo_review_events" in sql
    assert "CREATE TABLE IF NOT EXISTS memo_diffs" in sql


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


def _open_via_api(client) -> tuple[str, str]:
    resp = client.post(
        "/applications/from-los", json={"application_ref": "LOS-2026-0117"}
    )
    assert resp.status_code == 201
    credit_file_id = resp.json()["credit_file_id"]
    memos = client.get(f"/applications/{credit_file_id}/memos").json()
    assert len(memos) == 1
    return credit_file_id, memos[0]["memo_id"]


def test_api_officer_gate_flow(client):
    cf, mid = _open_via_api(client)
    base = f"/applications/{cf}/memos/{mid}"

    approve_early = client.post(
        f"{base}/approve",
        json={"officer_id": OFFICER, "officer_role": OFFICER_ROLE},
    )
    assert approve_early.status_code == 422  # not in review yet

    submitted = client.post(
        f"{base}/submit", json={"actor": RM, "actor_role": RM_ROLE}
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "in_officer_review"

    anonymous = client.post(
        f"{base}/approve", json={"officer_id": "", "officer_role": OFFICER_ROLE}
    )
    assert anonymous.status_code == 422

    flagged = client.post(
        f"{base}/flag",
        json={
            "officer_id": OFFICER_B,
            "officer_role": OFFICER_B_ROLE,
            "kind": "committee_attention",
            "section_key": "policy_exceptions",
            "note": "Committee should see the open exceptions.",
        },
    )
    assert flagged.status_code == 201
    assert flagged.json()["flag"]["kind"] == "committee_attention"

    approved = client.post(
        f"{base}/approve",
        json={
            "officer_id": OFFICER,
            "officer_role": OFFICER_ROLE,
            "edits": {
                "recommendation": [
                    "Recommend approval of the $1,500,000.00 term facility."
                ]
            },
        },
    )
    assert approved.status_code == 201
    final = approved.json()
    assert final["status"] == "officer_approved"
    assert final["supersedes_memo_id"] == mid
    assert final["reviewed_by"] == OFFICER
    assert len(final["committee_flags"]) == 1

    diff = client.get(f"/applications/{cf}/memos/{final['memo_id']}/diff")
    assert diff.status_code == 200
    assert diff.json()["from_memo_id"] == mid
    assert diff.json()["action"] == "approved_with_edits"

    no_diff = client.get(f"{base}/diff")
    assert no_diff.status_code == 404  # a draft has no approval diff

    history = client.get(f"/applications/{cf}/memos/{final['memo_id']}/history")
    assert history.status_code == 200
    body = history.json()
    assert [v["memo_id"] for v in body["versions"]] == [mid, final["memo_id"]]
    assert body["diff"]["to_memo_id"] == final["memo_id"]
    assert [e["action"] for e in body["events"]] == ["approved_with_edits"]

    text = client.get(f"/applications/{cf}/memos/{final['memo_id']}/text")
    assert "OFFICER APPROVED" in text.json()["text"]


def test_api_return_recompose_flow(client):
    cf, mid = _open_via_api(client)
    base = f"/applications/{cf}/memos/{mid}"
    client.post(f"{base}/submit", json={"actor": RM, "actor_role": RM_ROLE})

    free_text = client.post(
        f"{base}/return",
        json={
            "officer_id": OFFICER, "officer_role": OFFICER_ROLE,
            "reason_codes": [], "note": "Needs work.",
        },
    )
    assert free_text.status_code == 422

    returned = client.post(
        f"{base}/return",
        json={
            "officer_id": OFFICER, "officer_role": OFFICER_ROLE,
            "reason_codes": ["insufficient_analysis"],
        },
    )
    assert returned.status_code == 200
    assert returned.json()["status"] == "returned"

    approve_returned = client.post(
        f"{base}/approve",
        json={"officer_id": OFFICER, "officer_role": OFFICER_ROLE},
    )
    assert approve_returned.status_code == 422

    recomposed = client.post(
        f"/applications/{cf}/memos/recompose",
        json={
            "supersedes_memo_id": mid, "actor": RM, "actor_role": RM_ROLE,
        },
    )
    assert recomposed.status_code == 201
    draft2 = recomposed.json()
    assert draft2["status"] == "draft"
    assert draft2["supersedes_memo_id"] == mid
    assert draft2["memo_id"] != mid
