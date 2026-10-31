"""Round 9: committee readiness — design law 7 as an engine.

A credit file is committee-ready only when every required input
is on file and fresh. The round is graded on:

  one consolidated report        checklist states, the policy
                                 pack's age limits and the
                                 computation pack's not-computable
                                 figures in a single content-
                                 addressed artefact — not three lists
  the convene block              an officer-final memo cannot go
                                 before the committee while any
                                 blocker stands, and the refusal
                                 names every blocker
  recomputation at convene       a report minted earlier cannot
                                 bless a file whose inputs have
                                 since gone stale at a later as_of
  no waivers                     there is no override flag, no
                                 note-waiver, no waive method —
                                 a blocker clears only when the
                                 input itself refreshes
  the approval stamp             officer approval still lands on a
                                 blocked file, but its record
                                 carries the readiness report id
                                 and blocker count
  the outstanding-items list     generated from the report,
                                 never hand-typed

Fixtures: Hartline (the seed borrower — 7 of 9 documents missing,
no cash-flow page, so DSCR cannot compute) for the blocked paths,
and the committee-ready Calloway file from test_committee_gate for
the clear paths.
"""

import inspect
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.config import MIGRATIONS_DIR
from commercial_credit.db import migration_files
from commercial_credit.documents import LocalDocumentStore, register_document
from commercial_credit.errors import NotFoundError, ValidationError
from commercial_credit.models import (
    Borrower,
    DocumentKind,
    FacilityRequest,
    FacilityType,
    ReadinessEntry,
    ReadinessReport,
)
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.readiness import (
    READINESS_POLICY_ID,
    READINESS_POLICY_VERSION,
)
from commercial_credit.store import InMemoryCreditStore, SqliteCreditStore
from test_committee_gate import (
    OFFICER,
    OFFICER_ROLE,
    RM,
    RM_ROLE,
    calloway_borrower,
    calloway_documents,
    calloway_facility,
    hartline_pipeline,
    officer_final,
    open_hartline,
    open_ready_file,
)


def hartline_officer_final(pipeline: CreditPipeline):
    """Hartline's officer-final memo under its pinned v1 report —
    approvable (round 6/9), but never committee-ready."""
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


def entries_by_key(report) -> dict[str, object]:
    return {e.input_key: e for e in report.entries}


# ---------------------------------------------------------------------
# The consolidated report
# ---------------------------------------------------------------------


def test_report_consolidates_checklist_pack_and_figures():
    pipeline = hartline_pipeline()
    credit_file, draft, final = hartline_officer_final(pipeline)
    cf = credit_file.credit_file_id
    report = pipeline.compute_readiness(
        cf, memo_id=final.memo_id, as_of=date(2025, 6, 30)
    )
    assert report.readiness_policy_id == READINESS_POLICY_ID
    assert report.readiness_policy_version == READINESS_POLICY_VERSION
    assert report.policy_version == "v1"
    assert report.policy_report_id == final.policy_report_id
    by_key = entries_by_key(report)

    # Source 1: the checklist. Seven items missing, each a blocker,
    # each linked to the policy test that failed over them.
    for item_id in ("chk-03", "chk-04", "chk-05", "chk-06",
                    "chk-07", "chk-08", "chk-09"):
        entry = by_key[item_id]
        assert entry.state == "missing" and entry.blocker
        assert "required_documents_complete" in entry.related_test_ids
    # Statements are on file and fresh: FY2024 year-end is 5
    # complete months before 2025-06-30, within the v1 pack's
    # 18-month statement-age limit — the limit comes from the pack.
    statements = by_key["chk-01"]
    assert statements.state == "present_fresh" and not statements.blocker
    assert statements.age_months == 5
    assert statements.limit_months == 18
    assert "policy pack v1" in (statements.rule_source or "")

    # Source 2: the computation pack's not-computable figures.
    # Hartline has no cash-flow page, so FY2024 DSCR waits on
    # depreciation_and_amortization — named, not paraphrased.
    dscr = by_key["figure:dscr@FY2024"]
    assert dscr.state == "not_evaluable_input" and dscr.blocker
    assert any(
        "depreciation_and_amortization" in m for m in dscr.missing_inputs
    )
    assert "min_dscr" in dscr.related_test_ids

    # Hartline's facility DOES name guarantors, but no global pack
    # was ever combined: the global test's not_evaluable outcome
    # has no figure entry to attach to, so it stands as its own
    # blocker entry, naming the missing global view.
    global_entry = by_key["policy:min_global_dscr"]
    assert global_entry.state == "not_evaluable_input"
    assert global_entry.blocker
    assert any("guarantor" in m for m in global_entry.missing_inputs)
    assert not report.ready and report.blocker_count >= 9

    # Stored, retrievable, and deterministic: recomputation over
    # the same artefacts at the same date reproduces the report
    # and stores nothing new. (The approval already minted its own
    # report at its own date — a different artefact, also kept.)
    stored = pipeline.get_readiness_report(cf, report.report_id)
    assert stored.model_dump() == report.model_dump()
    ids_before = [
        r.report_id for r in pipeline.list_readiness_reports(cf)
    ]
    again = pipeline.compute_readiness(
        cf, memo_id=final.memo_id, as_of=date(2025, 6, 30)
    )
    assert again.report_id == report.report_id
    assert [
        r.report_id for r in pipeline.list_readiness_reports(cf)
    ] == ids_before


def test_ready_file_reports_no_blockers():
    pipeline = hartline_pipeline()
    credit_file, draft, final = officer_final(pipeline)
    cf = credit_file.credit_file_id
    report = pipeline.compute_readiness(
        cf, memo_id=final.memo_id, as_of=date(2026, 6, 30)
    )
    assert report.ready and report.blocker_count == 0
    by_key = entries_by_key(report)
    assert all(
        e.state == "present_fresh"
        for e in report.entries if e.source == "checklist"
    )
    # A policy test that does not apply to this file at all (a
    # global test on an unguaranteed facility) is NOT entered as a
    # blocker: there is no input this file could produce for it.
    assert "policy:min_global_dscr" not in by_key
    # The FY2023 debt-service gap is recorded — the first year's
    # debt service needs a pre-window year — but it does not block:
    # no policy test reads a prior period.
    prior = by_key["figure:dscr@FY2023"]
    assert prior.state == "not_evaluable_input"
    assert not prior.blocker
    assert "does not block" in prior.detail or "not blocking" in prior.detail


def test_freshness_limits_come_from_the_pinned_pack_version():
    # Under v2 the statement-age limit tightens to 15 months: the
    # same file at the same date reads its limit from the pinned
    # pack, not from a constant in the checker.
    pipeline = hartline_pipeline()
    credit_file = open_ready_file(pipeline)
    cf = credit_file.credit_file_id
    report_v2 = pipeline.evaluate_policy(
        cf, pack_version="v2", as_of=date(2027, 6, 30)
    )
    memo = pipeline.compose_memo(cf, report_id=report_v2.report_id)
    pipeline.submit_memo_for_review(
        cf, memo.memo_id, actor=RM, actor_role=RM_ROLE
    )
    pipeline.approve_memo(
        cf, memo.memo_id, officer_id=OFFICER, officer_role=OFFICER_ROLE
    )
    readiness = pipeline.compute_readiness(
        cf, memo_id=memo.memo_id, as_of=date(2027, 6, 30)
    )
    statements = entries_by_key(readiness)["chk-01"]
    # FY2025 year-end is 17 complete months before 2027-06-30:
    # stale under v2's 15, though it would pass v1's 18.
    assert statements.age_months == 17
    assert statements.limit_months == 15
    assert statements.state == "present_stale" and statements.blocker


# ---------------------------------------------------------------------
# The convene block
# ---------------------------------------------------------------------


def test_convene_refused_and_every_blocker_named():
    pipeline = hartline_pipeline()
    credit_file, draft, final = hartline_officer_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    with pytest.raises(ValidationError) as caught:
        pipeline.convene_committee(
            cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE,
            as_of=date(2025, 6, 30),
        )
    message = str(caught.value)
    reports = pipeline.list_readiness_reports(cf)
    refusal_report = reports[-1]
    assert refusal_report.report_id in message
    # Every blocker is named — key, label and state — in the
    # refusal itself, not behind a second lookup.
    for blocker in refusal_report.blockers:
        assert blocker.input_key in message
        assert blocker.label in message
        assert blocker.state in message
    assert "chk-03" in message and "Business tax returns" in message
    # The memo never moved.
    assert pipeline.memo_status(cf, fid) == "officer_approved"
    assert pipeline.store.list_committee_decisions(cf) == []


def test_missing_tax_returns_block_by_name():
    pipeline = hartline_pipeline()
    docs = [
        d for d in calloway_documents()
        if d["kind"] != DocumentKind.TAX_RETURN.value
    ]
    credit_file = pipeline.open_application(
        calloway_borrower(), calloway_facility(), documents=docs
    )
    cf = credit_file.credit_file_id
    report = pipeline.evaluate_policy(cf, pack_version="v1", as_of=date(2026, 6, 30))
    memo = pipeline.compose_memo(cf, report_id=report.report_id)
    pipeline.submit_memo_for_review(
        cf, memo.memo_id, actor=RM, actor_role=RM_ROLE
    )
    final = pipeline.approve_memo(
        cf, memo.memo_id, officer_id=OFFICER, officer_role=OFFICER_ROLE
    )
    with pytest.raises(ValidationError) as caught:
        pipeline.convene_committee(
            cf, final.memo_id, actor=OFFICER, actor_role=OFFICER_ROLE,
            as_of=date(2026, 6, 30),
        )
    message = str(caught.value)
    assert "[chk-03] Business tax returns" in message
    assert "[chk-04] Guarantor personal tax returns" in message


def test_old_report_cannot_bless_a_file_at_a_later_date():
    pipeline = hartline_pipeline()
    credit_file, draft, final = officer_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    fresh = pipeline.compute_readiness(
        cf, memo_id=fid, as_of=date(2026, 6, 30)
    )
    assert fresh.ready
    # Two years on, the same artefacts are stale: the convene
    # recomputes at ITS date and refuses — the fresh report above
    # is a record, not a blessing.
    with pytest.raises(ValidationError) as caught:
        pipeline.convene_committee(
            cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE,
            as_of=date(2028, 1, 31),
        )
    assert "financial statements" in str(caught.value).lower()
    reports = pipeline.list_readiness_reports(cf)
    assert reports[-1].as_of == date(2028, 1, 31)
    assert reports[-1].report_id != fresh.report_id
    # The earlier report itself is untouched.
    assert pipeline.get_readiness_report(cf, fresh.report_id).ready


# ---------------------------------------------------------------------
# Stale appraisal: block, refresh, land (CRE)
# ---------------------------------------------------------------------


def _cre_officer_final(pipeline: CreditPipeline, *, backdate_appraisal: bool):
    borrower = Borrower(
        legal_name="Harborpoint Retail Center LLC",
        ein_last4="9021",
        segment="real_estate",
        state_of_formation="NC",
        years_in_business=9,
        primary_contact_name="Marcus Yuen",
        primary_contact_email="marcus.yuen@example.com",
    )
    facility = FacilityRequest(
        facility_type=FacilityType.CRE_LOAN,
        amount=Decimal("4000000"),
        purpose="Refinance of a stabilized retail center",
        term_months=84,
        guarantors=[],
    )
    docs = calloway_documents()
    for kind, filename in (
        (DocumentKind.APPRAISAL, "appraisal-2025.json"),
        (DocumentKind.RENT_ROLL, "rent-roll.json"),
        (DocumentKind.LEASES, "leases.json"),
        (DocumentKind.ENVIRONMENTAL_REPORT, "environmental.json"),
    ):
        docs.append({
            "kind": kind.value,
            "filename": filename,
            "data_hex": json.dumps({}).encode().hex(),
        })
    credit_file = pipeline.open_application(borrower, facility, documents=docs)
    cf = credit_file.credit_file_id
    if backdate_appraisal:
        stored = pipeline.store.get_credit_file(cf)
        appraisal = next(
            d for d in stored.documents if d.kind == DocumentKind.APPRAISAL
        )
        appraisal.received_at = datetime(
            2025, 1, 15, tzinfo=timezone.utc
        )
        pipeline.store.save_credit_file(stored)
    report = pipeline.evaluate_policy(cf, pack_version="v1", as_of=date(2026, 6, 30))
    memo = pipeline.compose_memo(cf, report_id=report.report_id)
    pipeline.submit_memo_for_review(
        cf, memo.memo_id, actor=RM, actor_role=RM_ROLE
    )
    final = pipeline.approve_memo(
        cf, memo.memo_id, officer_id=OFFICER, officer_role=OFFICER_ROLE
    )
    return pipeline.store.get_credit_file(cf), memo, final


def test_stale_appraisal_blocks_then_refresh_clears():
    pipeline = hartline_pipeline()
    credit_file, draft, final = _cre_officer_final(
        pipeline, backdate_appraisal=True
    )
    cf, fid = credit_file.credit_file_id, final.memo_id
    readiness = pipeline.compute_readiness(
        cf, memo_id=fid, as_of=date(2026, 6, 30)
    )
    appraisal = entries_by_key(readiness)["chk-09"]
    assert "Appraisal" in appraisal.label
    assert appraisal.state == "present_stale" and appraisal.blocker
    assert appraisal.age_months == 17 and appraisal.limit_months == 12
    with pytest.raises(ValidationError, match="Appraisal"):
        pipeline.convene_committee(
            cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE,
            as_of=date(2026, 6, 30),
        )
    # The refresh: a NEW appraisal is received. Nothing else moves.
    stored = pipeline.store.get_credit_file(cf)
    doc = register_document(
        pipeline.store, pipeline.documents, stored,
        kind=DocumentKind.APPRAISAL,
        filename="appraisal-2026.json",
        data=json.dumps({}).encode(),
    )
    stored = pipeline.store.get_credit_file(cf)
    new_doc = stored.document(doc.document_id)
    new_doc.received_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    pipeline.store.save_credit_file(stored)
    refreshed = pipeline.compute_readiness(
        cf, memo_id=fid, as_of=date(2026, 6, 30)
    )
    appraisal = entries_by_key(refreshed)["chk-09"]
    assert appraisal.state == "present_fresh" and not appraisal.blocker
    convened = pipeline.convene_committee(
        cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE,
        as_of=date(2026, 6, 30),
    )
    assert convened.status == "in_committee"


def test_blocker_clears_only_when_the_input_refreshes():
    pipeline = hartline_pipeline()
    docs = [
        d for d in calloway_documents()
        if d["kind"] != DocumentKind.INSURANCE_CERTIFICATE.value
    ]
    credit_file = pipeline.open_application(
        calloway_borrower(), calloway_facility(), documents=docs
    )
    cf = credit_file.credit_file_id
    report = pipeline.evaluate_policy(cf, pack_version="v1", as_of=date(2026, 6, 30))
    memo = pipeline.compose_memo(cf, report_id=report.report_id)
    pipeline.submit_memo_for_review(
        cf, memo.memo_id, actor=RM, actor_role=RM_ROLE
    )
    final = pipeline.approve_memo(
        cf, memo.memo_id, officer_id=OFFICER, officer_role=OFFICER_ROLE
    )
    first = pipeline.compute_readiness(
        cf, memo_id=final.memo_id, as_of=date(2026, 6, 30)
    )
    insurance = entries_by_key(first)["chk-08"]
    assert insurance.state == "missing" and insurance.blocker
    # Recomputing, re-approving, waiting — none of it clears the
    # blocker. Only the document arriving does.
    second = pipeline.compute_readiness(
        cf, memo_id=final.memo_id, as_of=date(2026, 6, 30)
    )
    assert second.report_id == first.report_id
    stored = pipeline.store.get_credit_file(cf)
    register_document(
        pipeline.store, pipeline.documents, stored,
        kind=DocumentKind.INSURANCE_CERTIFICATE,
        filename="insurance-certificate.json",
        data=json.dumps({}).encode(),
    )
    third = pipeline.compute_readiness(
        cf, memo_id=final.memo_id, as_of=date(2026, 6, 30)
    )
    insurance = entries_by_key(third)["chk-08"]
    assert insurance.state == "present_fresh" and not insurance.blocker
    assert third.ready


# ---------------------------------------------------------------------
# The approval stamp + no waivers
# ---------------------------------------------------------------------


def test_approval_record_carries_the_readiness_stamp():
    pipeline = hartline_pipeline()
    credit_file, draft, final = hartline_officer_final(pipeline)
    cf = credit_file.credit_file_id
    events = pipeline.store.list_memo_review_events(cf, draft.memo_id)
    approval = next(
        e for e in events if e.action.startswith("approved")
    )
    assert approval.readiness_report_id is not None
    assert approval.readiness_blocker_count is not None
    assert approval.readiness_blocker_count > 0
    report = pipeline.get_readiness_report(cf, approval.readiness_report_id)
    assert report.blocker_count == approval.readiness_blocker_count
    diff = pipeline.get_memo_diff(cf, final.memo_id)
    assert diff.readiness_report_id == approval.readiness_report_id
    assert diff.readiness_blocker_count == approval.readiness_blocker_count


def test_no_waiver_surface_exists():
    # No parameter, on any readiness-touching entry point.
    for fn in (
        CreditPipeline.convene_committee,
        CreditPipeline.approve_memo,
        CreditPipeline.compute_readiness,
    ):
        params = inspect.signature(fn).parameters
        assert not any(
            "waiv" in name or "override" in name or name == "force"
            for name in params
        ), fn.__name__
    # No method that waives or overrides anything — with three
    # named entries that are not readiness surfaces at all:
    # _overrides_for is round 12's private lookup (the review
    # gate reloading a pack's pinned countersign override) and
    # list_rating_overrides is round 13's read-only listing of
    # the recorded override artefacts;
    # round 11's waive_breach resolves a covenant BREACH, only
    # on a committee decision's authority, and cannot touch a
    # readiness blocker (its parameters name a breach and a
    # decision, never a file's readiness); round 13's
    # override_rating moves a risk rating's FINAL grade, at most
    # one grade from the assigned grade, for a coded reason,
    # countersigned by a second officer — its parameters name an
    # assignment and a grade, never a file's readiness, and the
    # readiness report it could supposedly clear has no field
    # for it to write.
    assert [
        name for name in dir(CreditPipeline)
        if "waiv" in name or "override" in name
    ] == [
        "_overrides_for", "list_rating_overrides",
        "override_rating", "waive_breach",
    ]
    # No waiver field on the report or its entries.
    for model in (ReadinessReport, ReadinessEntry):
        assert not any(
            "waiv" in name or "override" in name
            for name in model.model_fields
        )
    # And a caller who tries to pass one is refused by the
    # signature itself — the blocker stands.
    pipeline = hartline_pipeline()
    credit_file, draft, final = hartline_officer_final(pipeline)
    with pytest.raises(TypeError):
        pipeline.convene_committee(
            credit_file.credit_file_id, final.memo_id,
            actor=OFFICER, actor_role=OFFICER_ROLE,
            waive_blockers=True,
        )
    assert (
        pipeline.memo_status(credit_file.credit_file_id, final.memo_id)
        == "officer_approved"
    )


# ---------------------------------------------------------------------
# The borrower-facing list, storage, API
# ---------------------------------------------------------------------


def test_outstanding_items_are_generated_from_the_report():
    pipeline = hartline_pipeline()
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    text = pipeline.outstanding_items(cf, as_of=date(2025, 6, 30))
    assert "Business tax returns: not on file." in text
    assert "Guarantor personal financial statements" in text
    assert "depreciation_and_amortization" in text
    report = pipeline.compute_readiness(cf, as_of=date(2025, 6, 30))
    assert report.report_id in text
    # A ready file renders the empty list, in so many words.
    pipeline2 = hartline_pipeline()
    ready_file = open_ready_file(pipeline2)
    text = pipeline2.outstanding_items(
        ready_file.credit_file_id, as_of=date(2026, 6, 30)
    )
    assert "No outstanding items" in text


def test_sqlite_store_round_trips_readiness_reports(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    pipeline = hartline_pipeline(store=store)
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    report = pipeline.compute_readiness(cf, as_of=date(2025, 6, 30))
    reopened = SqliteCreditStore(tmp_path / "credit.db")
    stored = reopened.get_readiness_report(report.report_id)
    assert stored is not None
    assert stored.model_dump() == report.model_dump()
    assert reopened.list_readiness_reports(cf)[0].report_id == (
        report.report_id
    )


def test_migration_0008_is_registered():
    names = [f.name for f in migration_files(MIGRATIONS_DIR)]
    assert "0008_readiness_reports.sql" in names
    sql = (MIGRATIONS_DIR / "0008_readiness_reports.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS readiness_reports" in sql


@pytest.fixture()
def client():
    pipeline = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app)


def test_api_readiness_surfaces(client):
    resp = client.post(
        "/applications/from-los", json={"application_ref": "LOS-2026-0117"}
    )
    assert resp.status_code == 201
    cf = resp.json()["credit_file_id"]

    current = client.get(f"/applications/{cf}/readiness-report")
    assert current.status_code == 200
    body = current.json()
    assert body["credit_file_id"] == cf
    missing = {
        e["input_key"] for e in body["entries"] if e["state"] == "missing"
    }
    assert "chk-03" in missing
    assert body["summary"]["blockers"] > 0

    listed = client.get(f"/applications/{cf}/readiness-reports")
    assert listed.status_code == 200
    assert listed.json()[-1]["report_id"] == body["report_id"]

    fetched = client.get(
        f"/applications/{cf}/readiness-reports/{body['report_id']}"
    )
    assert fetched.status_code == 200
    assert fetched.json()["report_id"] == body["report_id"]

    unknown = client.get(
        f"/applications/{cf}/readiness-reports/readiness-nope"
    )
    assert unknown.status_code == 404

    outstanding = client.get(f"/applications/{cf}/outstanding-items")
    assert outstanding.status_code == 200
    assert outstanding.json()["readiness_report_id"] == body["report_id"]
    assert "Business tax returns: not on file." in (
        outstanding.json()["text"]
    )


def test_api_convene_refusal_names_blockers_in_the_422(client):
    resp = client.post(
        "/applications/from-los", json={"application_ref": "LOS-2026-0117"}
    )
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
    fid = approved.json()["memo_id"]
    refused = client.post(
        f"/applications/{cf}/memos/{fid}/committee/convene",
        json={"actor": OFFICER, "actor_role": OFFICER_ROLE},
    )
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert "committee-ready" in detail
    assert "chk-03" in detail and "Business tax returns" in detail
    assert "readiness-" in detail


# ---------------------------------------------------------------------
# Round 18: staleness edges — a document is fresh at exactly its
# limit in complete calendar months and stale one month past it,
# and a file with exactly one stale input among fresh ones is
# blocked by that input alone.
# ---------------------------------------------------------------------


def _set_received(pipeline, cf, kind, received: datetime):
    stored = pipeline.store.get_credit_file(cf)
    doc = next(d for d in stored.documents if d.kind == kind)
    doc.received_at = received
    pipeline.store.save_credit_file(stored)


def test_insurance_certificate_is_fresh_at_its_limit_and_stale_one_month_past():
    # The readiness policy's receipt-age limit for an insurance
    # certificate is 12 complete calendar months.
    pipeline = hartline_pipeline()
    credit_file, draft, final = officer_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    _set_received(
        pipeline, cf, DocumentKind.INSURANCE_CERTIFICATE,
        datetime(2025, 6, 30, tzinfo=timezone.utc),
    )
    at_limit = pipeline.compute_readiness(
        cf, memo_id=fid, as_of=date(2026, 6, 30)
    )
    insurance = entries_by_key(at_limit)["chk-08"]
    assert insurance.age_months == 12 and insurance.limit_months == 12
    assert insurance.state == "present_fresh" and not insurance.blocker
    assert at_limit.ready

    _set_received(
        pipeline, cf, DocumentKind.INSURANCE_CERTIFICATE,
        datetime(2025, 5, 30, tzinfo=timezone.utc),
    )
    past_limit = pipeline.compute_readiness(
        cf, memo_id=fid, as_of=date(2026, 6, 30)
    )
    insurance = entries_by_key(past_limit)["chk-08"]
    assert insurance.age_months == 13
    assert insurance.state == "present_stale" and insurance.blocker
    assert not past_limit.ready


def test_statement_age_edge_in_readiness():
    # Statements age by fiscal period against the pinned pack:
    # FY2025 year-end is exactly 18 complete months old at
    # 2027-07-01 (the v1 limit — fresh) and 19 at 2027-07-31.
    pipeline = hartline_pipeline()
    credit_file, draft, final = officer_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    at_limit = pipeline.compute_readiness(
        cf, memo_id=fid, as_of=date(2027, 7, 1)
    )
    statements = entries_by_key(at_limit)["chk-01"]
    assert statements.age_months == 18 and statements.limit_months == 18
    assert statements.state == "present_fresh" and not statements.blocker
    past_limit = pipeline.compute_readiness(
        cf, memo_id=fid, as_of=date(2027, 7, 31)
    )
    statements = entries_by_key(past_limit)["chk-01"]
    assert statements.age_months == 19
    assert statements.state == "present_stale" and statements.blocker


def test_one_stale_input_among_fresh_ones_blocks_the_convene_by_itself():
    pipeline = hartline_pipeline()
    credit_file, draft, final = officer_final(pipeline)
    cf, fid = credit_file.credit_file_id, final.memo_id
    _set_received(
        pipeline, cf, DocumentKind.INSURANCE_CERTIFICATE,
        datetime(2025, 5, 30, tzinfo=timezone.utc),
    )
    report = pipeline.compute_readiness(
        cf, memo_id=fid, as_of=date(2026, 6, 30)
    )
    assert report.blocker_count == 1
    blocker = report.blockers[0]
    assert blocker.input_key == "chk-08" and "Insurance" in blocker.label
    with pytest.raises(ValidationError, match="Insurance"):
        pipeline.convene_committee(
            cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE,
            as_of=date(2026, 6, 30),
        )
    # Refreshing that one input — and nothing else — clears it.
    stored = pipeline.store.get_credit_file(cf)
    doc = register_document(
        pipeline.store, pipeline.documents, stored,
        kind=DocumentKind.INSURANCE_CERTIFICATE,
        filename="insurance-certificate-2026.json",
        data=json.dumps({}).encode(),
    )
    stored = pipeline.store.get_credit_file(cf)
    stored.document(doc.document_id).received_at = datetime(
        2026, 6, 1, tzinfo=timezone.utc
    )
    pipeline.store.save_credit_file(stored)
    refreshed = pipeline.compute_readiness(
        cf, memo_id=fid, as_of=date(2026, 6, 30)
    )
    assert refreshed.ready
    convened = pipeline.convene_committee(
        cf, fid, actor=OFFICER, actor_role=OFFICER_ROLE,
        as_of=date(2026, 6, 30),
    )
    assert convened.status == "in_committee"
