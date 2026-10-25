"""HTTP API for the Commercial Credit Memo & Covenant Monitoring Agent.

Round 1 surface: health/readiness, application intake (LOS reference
or inline borrower + facility), the generated checklist, document
registration with hash recording, verified reads, the extraction
pass, and the audit trail. Round 2 adds the spread: the multi-period
spread for a credit file, its adjustments, and the adjusted view.
Round 3 adds computations: the deterministic engine's pack for a
credit file (with supplemental figures and covenant definitions the
caller hands in), stored packs by id, and the global cash-flow view
combining the borrower with guarantor spreads. Round 4 adds the
policy pack: the shipped pack versions, an evaluation of a file
against one pinned version, and the stored policy reports. Round
5 adds memo composition, the composed memo, its verification
record and its rendered text. Round 6 adds the credit officer
gate: submit a draft for review, approve it (as-is or with
edits), return it for rework with coded reasons, flag items on
it, recompose a returned memo, and read a memo's version history
and its approval diff. Round 7 adds the committee gate: convene
the committee on an officer-final memo, record its decision
(approve, approve with structured conditions, decline with coded
adverse-action reasons tied to the evidence, or defer with named
re-presentment requirements), and read the decision record, its
conditions, and the generated adverse-action notice. Every
request carries a request id into the structured logs.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from datetime import date
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import load_settings
from .documents import read_document_verified, register_document
from .errors import CreditError, NotFoundError, ValidationError
from .logging_setup import configure_logging, request_id_var
from .models import (
    AdjustmentReasonCode,
    AdverseActionReason,
    Borrower,
    CommitteeCondition,
    CommitteeMember,
    CovenantDefinition,
    DocumentKind,
    Elimination,
    ExtractedValue,
    FacilityRequest,
    Provenance,
    Spread,
    SpreadAdjustment,
)
from .memo import render_memo
from .pipeline import CreditPipeline
from .policy import get_policy_pack, list_policy_packs
from .seed import ReferenceLosFeed
from .wiring import build_pipeline

log = logging.getLogger("commercial_credit.api")
configure_logging()

settings = load_settings()
pipeline: CreditPipeline = build_pipeline(settings)
store = pipeline.store
los_feed = ReferenceLosFeed()


def _file_or_404(credit_file_id: str):  # noqa: ANN202
    credit_file = store.get_credit_file(credit_file_id)
    if credit_file is None:
        raise NotFoundError(f"credit file {credit_file_id} not found")
    return credit_file


class OpenApplicationRequest(BaseModel):
    borrower: Borrower
    facility: FacilityRequest


class OpenFromLosRequest(BaseModel):
    application_ref: str


class RegisterDocumentRequest(BaseModel):
    kind: DocumentKind
    filename: str
    content_b64: str
    period_label: str | None = None
    content_type: str = "application/json"


app = FastAPI(
    title="Trida AI Blueprint — Commercial Credit Memo & Covenant Monitoring Agent",
    version="0.1.0",
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):  # noqa: ANN001, ANN202
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["x-request-id"] = rid
    return response


@app.exception_handler(CreditError)
async def credit_error_handler(request: Request, exc: CreditError) -> JSONResponse:
    status = 404 if isinstance(exc, NotFoundError) else 422
    return JSONResponse(status_code=status, content={"detail": str(exc)})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/readiness")
def readiness() -> dict:
    ok = store.ping()
    return {
        "status": "ready" if ok else "unavailable",
        "store_profile": settings.store_profile,
        "store_reachable": ok,
        "document_store": "s3" if settings.s3_bucket else "local",
    }


@app.post("/applications", status_code=201)
def open_application(body: OpenApplicationRequest) -> dict:
    credit_file = pipeline.open_application(body.borrower, body.facility)
    return credit_file.model_dump(mode="json")


@app.post("/applications/from-los", status_code=201)
def open_from_los(body: OpenFromLosRequest) -> dict:
    borrower, facility = los_feed.fetch_application(body.application_ref)
    credit_file = pipeline.open_application(borrower, facility)
    return credit_file.model_dump(mode="json")


@app.get("/applications")
def list_applications() -> list[dict]:
    return [cf.model_dump(mode="json") for cf in store.list_credit_files()]


@app.get("/applications/{credit_file_id}")
def get_application(credit_file_id: str) -> dict:
    return _file_or_404(credit_file_id).model_dump(mode="json")


@app.get("/applications/{credit_file_id}/checklist")
def get_checklist(credit_file_id: str) -> list[dict]:
    credit_file = _file_or_404(credit_file_id)
    return [item.model_dump(mode="json") for item in credit_file.checklist]


@app.post("/applications/{credit_file_id}/documents", status_code=201)
def add_document(credit_file_id: str, body: RegisterDocumentRequest) -> dict:
    credit_file = _file_or_404(credit_file_id)
    try:
        data = base64.b64decode(body.content_b64, validate=True)
    except binascii.Error as exc:
        raise ValidationError("content_b64 is not valid base64") from exc
    doc = register_document(
        store,
        pipeline.documents,
        credit_file,
        kind=body.kind,
        filename=body.filename,
        data=data,
        period_label=body.period_label,
        content_type=body.content_type,
    )
    pipeline._audit(credit_file_id, "document_registered",
                    f"{doc.kind.value} {doc.filename} via API")
    return doc.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/documents/{document_id}")
def get_document(credit_file_id: str, document_id: str) -> dict:
    credit_file = _file_or_404(credit_file_id)
    doc = credit_file.document(document_id)
    if doc is None:
        raise NotFoundError(f"document {document_id} not found")
    data = read_document_verified(pipeline.documents, doc)
    return {
        "document": doc.model_dump(mode="json"),
        "content_b64": base64.b64encode(data).decode("ascii"),
        "hash_verified": True,
    }


@app.post("/applications/{credit_file_id}/extract")
def extract(credit_file_id: str) -> dict:
    _file_or_404(credit_file_id)
    pipeline.run_extraction(credit_file_id)
    values = store.get_extractions(credit_file_id)
    return {
        "credit_file_id": credit_file_id,
        "extraction_count": len(values),
        "values": [v.model_dump(mode="json") for v in values],
    }


@app.get("/applications/{credit_file_id}/audit")
def get_audit(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [e.model_dump(mode="json") for e in store.list_audit(credit_file_id)]


class AddAdjustmentRequest(BaseModel):
    line_key: str
    fiscal_year: int
    amount: Decimal
    reason_code: AdjustmentReasonCode
    reason: str
    source: Provenance
    proposed_by: str


@app.get("/applications/{credit_file_id}/spread")
def get_spread(credit_file_id: str) -> dict:
    _file_or_404(credit_file_id)
    spread = pipeline.get_spread(credit_file_id)
    return spread.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/adjustments")
def list_adjustments(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        a.model_dump(mode="json") for a in store.list_adjustments(credit_file_id)
    ]


@app.post("/applications/{credit_file_id}/adjustments", status_code=201)
def add_adjustment(credit_file_id: str, body: AddAdjustmentRequest) -> dict:
    _file_or_404(credit_file_id)
    adjustment = SpreadAdjustment(
        credit_file_id=credit_file_id,
        line_key=body.line_key,
        fiscal_year=body.fiscal_year,
        amount=body.amount,
        reason_code=body.reason_code,
        reason=body.reason,
        source=body.source,
        proposed_by=body.proposed_by,
    )
    spread = pipeline.add_adjustment(credit_file_id, adjustment)
    return {
        "adjustment": adjustment.model_dump(mode="json"),
        "spread": spread.model_dump(mode="json"),
    }


class ComputeRequest(BaseModel):
    view: str = "adjusted"
    supplemental: list[ExtractedValue] = []
    covenant_definitions: list[CovenantDefinition] = []


@app.get("/applications/{credit_file_id}/computations")
def get_computations(credit_file_id: str) -> dict:
    _file_or_404(credit_file_id)
    pack = pipeline.compute_metrics(credit_file_id)
    return pack.model_dump(mode="json")


@app.post("/applications/{credit_file_id}/computations", status_code=201)
def compute(credit_file_id: str, body: ComputeRequest) -> dict:
    _file_or_404(credit_file_id)
    pack = pipeline.compute_metrics(
        credit_file_id,
        view=body.view,
        supplemental=body.supplemental,
        covenant_definitions=body.covenant_definitions,
    )
    return pack.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/computations/packs")
def list_computation_packs(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        p.model_dump(mode="json")
        for p in store.list_computation_packs(credit_file_id)
    ]


@app.get("/applications/{credit_file_id}/computations/{pack_id}")
def get_computation_pack(credit_file_id: str, pack_id: str) -> dict:
    _file_or_404(credit_file_id)
    pack = store.get_computation_pack(pack_id)
    if pack is None or pack.credit_file_id != credit_file_id:
        raise NotFoundError(f"computation pack {pack_id} not found")
    return pack.model_dump(mode="json")


class GuarantorSpreadInput(BaseModel):
    entity: str
    spread: Spread
    extractions: list[ExtractedValue] = []


class GlobalComputeRequest(BaseModel):
    view: str = "adjusted"
    guarantors: list[GuarantorSpreadInput] = []
    eliminations: list[Elimination] = []
    expected_guarantors: list[str] = []
    exclude_missing_guarantors: bool = False


@app.post(
    "/applications/{credit_file_id}/computations/global", status_code=201
)
def compute_global(credit_file_id: str, body: GlobalComputeRequest) -> dict:
    _file_or_404(credit_file_id)
    pack = pipeline.compute_global(
        credit_file_id,
        guarantor_spreads={
            g.entity: (g.spread, g.extractions) for g in body.guarantors
        },
        eliminations=body.eliminations,
        expected_guarantors=body.expected_guarantors,
        exclude_missing_guarantors=body.exclude_missing_guarantors,
        view=body.view,
    )
    return pack.model_dump(mode="json")


@app.get("/policy-packs")
def list_packs() -> list[dict]:
    return [p.model_dump(mode="json") for p in list_policy_packs()]


@app.get("/policy-packs/{version}")
def get_pack(version: str) -> dict:
    return get_policy_pack(version).model_dump(mode="json")


class EvaluatePolicyRequest(BaseModel):
    pack_version: str | None = None
    as_of: date | None = None


@app.post(
    "/applications/{credit_file_id}/policy-evaluations", status_code=201
)
def evaluate_policy(credit_file_id: str, body: EvaluatePolicyRequest) -> dict:
    _file_or_404(credit_file_id)
    report = pipeline.evaluate_policy(
        credit_file_id,
        pack_version=body.pack_version,
        as_of=body.as_of,
    )
    return report.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/policy-reports")
def list_policy_reports(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        r.model_dump(mode="json")
        for r in store.list_policy_reports(credit_file_id)
    ]


@app.get("/applications/{credit_file_id}/policy-reports/{report_id}")
def get_policy_report(credit_file_id: str, report_id: str) -> dict:
    _file_or_404(credit_file_id)
    report = store.get_policy_report(report_id)
    if report is None or report.credit_file_id != credit_file_id:
        raise NotFoundError(f"policy report {report_id} not found")
    return report.model_dump(mode="json")


class ComposeMemoRequest(BaseModel):
    report_id: str | None = None
    pack_version: str | None = None
    profile: str = "offline"


@app.post("/applications/{credit_file_id}/memos", status_code=201)
def compose_memo(credit_file_id: str, body: ComposeMemoRequest) -> dict:
    _file_or_404(credit_file_id)
    memo = pipeline.compose_memo(
        credit_file_id,
        report_id=body.report_id,
        pack_version=body.pack_version,
        profile=body.profile,
    )
    return memo.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/memos")
def list_memos(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        m.model_dump(mode="json") for m in store.list_memos(credit_file_id)
    ]


@app.get("/applications/{credit_file_id}/memos/{memo_id}")
def get_memo(credit_file_id: str, memo_id: str) -> dict:
    _file_or_404(credit_file_id)
    return pipeline.get_memo(credit_file_id, memo_id).model_dump(mode="json")


@app.get("/applications/{credit_file_id}/memos/{memo_id}/verification")
def get_memo_verification(credit_file_id: str, memo_id: str) -> dict:
    _file_or_404(credit_file_id)
    memo = pipeline.get_memo(credit_file_id, memo_id)
    return memo.verification.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/memos/{memo_id}/text")
def get_memo_text(credit_file_id: str, memo_id: str) -> dict:
    _file_or_404(credit_file_id)
    memo = pipeline.get_memo(credit_file_id, memo_id)
    return {"memo_id": memo.memo_id, "text": render_memo(memo)}


# -- the credit officer gate (round 6) -------------------------------


class SubmitMemoRequest(BaseModel):
    actor: str
    actor_role: str


class ApproveMemoRequest(BaseModel):
    officer_id: str
    officer_role: str
    edits: dict[str, list[str]] | None = None
    note: str = ""


class ReturnMemoRequest(BaseModel):
    officer_id: str
    officer_role: str
    reason_codes: list[str] = []
    note: str = ""


class FlagMemoRequest(BaseModel):
    officer_id: str
    officer_role: str
    kind: str
    section_key: str | None = None
    note: str = ""


class RecomposeMemoRequest(BaseModel):
    supersedes_memo_id: str
    actor: str
    actor_role: str


@app.post("/applications/{credit_file_id}/memos/{memo_id}/submit")
def submit_memo(credit_file_id: str, memo_id: str, body: SubmitMemoRequest) -> dict:
    _file_or_404(credit_file_id)
    memo = pipeline.submit_memo_for_review(
        credit_file_id, memo_id, actor=body.actor, actor_role=body.actor_role
    )
    return memo.model_dump(mode="json")


@app.post(
    "/applications/{credit_file_id}/memos/{memo_id}/approve",
    status_code=201,
)
def approve_memo(credit_file_id: str, memo_id: str, body: ApproveMemoRequest) -> dict:
    _file_or_404(credit_file_id)
    memo = pipeline.approve_memo(
        credit_file_id,
        memo_id,
        officer_id=body.officer_id,
        officer_role=body.officer_role,
        edits=body.edits,
        note=body.note,
    )
    return memo.model_dump(mode="json")


@app.post("/applications/{credit_file_id}/memos/{memo_id}/return")
def return_memo(credit_file_id: str, memo_id: str, body: ReturnMemoRequest) -> dict:
    _file_or_404(credit_file_id)
    memo = pipeline.return_memo(
        credit_file_id,
        memo_id,
        officer_id=body.officer_id,
        officer_role=body.officer_role,
        reason_codes=body.reason_codes,
        note=body.note,
    )
    return memo.model_dump(mode="json")


@app.post(
    "/applications/{credit_file_id}/memos/{memo_id}/flag", status_code=201
)
def flag_memo(credit_file_id: str, memo_id: str, body: FlagMemoRequest) -> dict:
    _file_or_404(credit_file_id)
    event = pipeline.flag_memo(
        credit_file_id,
        memo_id,
        officer_id=body.officer_id,
        officer_role=body.officer_role,
        kind=body.kind,
        section_key=body.section_key,
        note=body.note,
    )
    return event.model_dump(mode="json")


@app.post("/applications/{credit_file_id}/memos/recompose", status_code=201)
def recompose_memo(credit_file_id: str, body: RecomposeMemoRequest) -> dict:
    _file_or_404(credit_file_id)
    memo = pipeline.recompose_memo(
        credit_file_id,
        supersedes_memo_id=body.supersedes_memo_id,
        actor=body.actor,
        actor_role=body.actor_role,
    )
    return memo.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/memos/{memo_id}/diff")
def get_memo_diff(credit_file_id: str, memo_id: str) -> dict:
    _file_or_404(credit_file_id)
    diff = pipeline.get_memo_diff(credit_file_id, memo_id)
    return diff.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/memos/{memo_id}/history")
def get_memo_history(credit_file_id: str, memo_id: str) -> dict:
    _file_or_404(credit_file_id)
    history = pipeline.memo_history(credit_file_id, memo_id)
    return {
        "memo_id": history["memo_id"],
        "status": history["status"],
        "versions": [
            {**v, "created_at": v["created_at"].isoformat()}
            for v in history["versions"]
        ],
        "events": [e.model_dump(mode="json") for e in history["events"]],
        "diff": (
            history["diff"].model_dump(mode="json")
            if history["diff"] is not None
            else None
        ),
        "committee_decisions": [
            d.model_dump(mode="json")
            for d in history["committee_decisions"]
        ],
    }


# -- the committee gate (round 7) ------------------------------------


class ConveneCommitteeRequest(BaseModel):
    actor: str
    actor_role: str


class DecideCommitteeRequest(BaseModel):
    recorder_id: str
    recorder_role: str
    members: list[CommitteeMember] = []
    outcome: str
    quorum_required: int = 2
    conditions: list[CommitteeCondition] = []
    adverse_reasons: list[AdverseActionReason] = []
    deferral_reasons: list[str] = []
    representment_requirements: list[str] = []
    note: str = ""


@app.post("/applications/{credit_file_id}/memos/{memo_id}/committee/convene")
def convene_committee(
    credit_file_id: str, memo_id: str, body: ConveneCommitteeRequest
) -> dict:
    _file_or_404(credit_file_id)
    memo = pipeline.convene_committee(
        credit_file_id, memo_id, actor=body.actor, actor_role=body.actor_role
    )
    return memo.model_dump(mode="json")


@app.post(
    "/applications/{credit_file_id}/memos/{memo_id}/committee/decide",
    status_code=201,
)
def decide_committee(
    credit_file_id: str, memo_id: str, body: DecideCommitteeRequest
) -> dict:
    _file_or_404(credit_file_id)
    decision = pipeline.decide_committee(
        credit_file_id,
        memo_id,
        recorder_id=body.recorder_id,
        recorder_role=body.recorder_role,
        members=body.members,
        outcome=body.outcome,
        quorum_required=body.quorum_required,
        conditions=body.conditions,
        adverse_reasons=body.adverse_reasons,
        deferral_reasons=body.deferral_reasons,
        representment_requirements=body.representment_requirements,
        note=body.note,
    )
    return decision.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/memos/{memo_id}/committee")
def get_committee_state(credit_file_id: str, memo_id: str) -> dict:
    _file_or_404(credit_file_id)
    state = pipeline.committee_state(credit_file_id, memo_id)
    return {
        "memo_id": state["memo_id"],
        "status": state["status"],
        "decisions": [
            d.model_dump(mode="json") for d in state["decisions"]
        ],
        "latest_decision": (
            state["latest_decision"].model_dump(mode="json")
            if state["latest_decision"] is not None
            else None
        ),
    }


@app.get(
    "/applications/{credit_file_id}/memos/{memo_id}/committee"
    "/decisions/{decision_id}"
)
def get_committee_decision(
    credit_file_id: str, memo_id: str, decision_id: str
) -> dict:
    _file_or_404(credit_file_id)
    decision = pipeline.get_committee_decision(credit_file_id, decision_id)
    if decision.memo_id != memo_id:
        raise NotFoundError(
            f"committee decision {decision_id} not found on memo {memo_id}"
        )
    return decision.model_dump(mode="json")


@app.get(
    "/applications/{credit_file_id}/memos/{memo_id}/committee"
    "/adverse-action-notice"
)
def get_adverse_action_notice(credit_file_id: str, memo_id: str) -> dict:
    _file_or_404(credit_file_id)
    text = pipeline.get_adverse_action_notice(credit_file_id, memo_id)
    decisions = pipeline.list_committee_decisions(credit_file_id, memo_id)
    declines = [d for d in decisions if d.outcome == "decline"]
    return {
        "memo_id": memo_id,
        "decision_id": declines[-1].decision_id if declines else None,
        "text": text,
    }
