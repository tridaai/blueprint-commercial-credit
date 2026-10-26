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
conditions, and the generated adverse-action notice. Round 8 adds
the disagreements view: the spread's disagreement records between
source types — list and detail — with the spread itself carrying
the flags. Round 9 adds committee readiness: the per-file report
and its outstanding-items rendering. Round 10 adds covenants:
recording the facility closing date, the covenant sets approvals
create, the derived schedule (test dates, states, named required
inputs) at a requested as_of, the portfolio due-work queue, the
scheduler tick, and covenant amendment by supersession. Round
11 adds covenant test execution and breach cases. Round 12 adds
annual reviews: the derived review obligations per covenant set,
pack assembly (verified summary included), the due-work queue,
the scheduler opening due reviews, and the officer's coded
outcome — with its committee referral when the stance escalates.
Round 13 adds the risk rating: assignment from the coded
scorecard, its factor-traced record, history across scorecard
versions, and the bounded officer override. Round 14 adds the
officer console: the static page at /console and the two read
aggregates it renders (the pipeline queue and the per-file
workspace) plus the coded vocabularies its forms offer. The
console adds no write semantics — every action it offers posts
to an existing gated route above. Round 15 adds the model
gateway reads: the governed profile list (variable names and
configured flags only — key values never appear), a file's
priced call ledger and cost rollup with its budget position,
and the portfolio's daily spend by profile. A budget refusal
surfaces as 429, distinct from the 422 domain refusals: the
request was well-formed, the file's priced budget is spent.
Every request carries a request id into the structured logs.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from datetime import date
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import load_settings
from .console import queue_view, vocabularies_view, workspace_view
from .console_page import CONSOLE_HTML
from .documents import read_document_verified, register_document
from .errors import (
    CreditError,
    ModelBudgetExceededError,
    NotFoundError,
    ValidationError,
)
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


@app.exception_handler(ModelBudgetExceededError)
async def model_budget_error_handler(
    request: Request, exc: ModelBudgetExceededError
) -> JSONResponse:
    # Round 15: a budget refusal is not a malformed request — the
    # file's priced model budget is spent. 429 keeps that distinct
    # from the 422 domain refusals above (the same mapping the AML
    # blueprint's gateway uses).
    return JSONResponse(status_code=429, content={"detail": str(exc)})


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


@app.get("/applications/{credit_file_id}/disagreements")
def list_disagreements(credit_file_id: str, material: bool | None = None) -> list[dict]:
    """The disagreements the file's spread records between its
    source types (round 8): every source's figure with its
    provenance, the deltas, and the materiality verdict. The
    as-reported view — nothing here is the spread's working figure
    alone, and nothing is averaged."""
    _file_or_404(credit_file_id)
    return [
        d.model_dump(mode="json")
        for d in pipeline.list_disagreements(credit_file_id, material=material)
    ]


@app.get("/applications/{credit_file_id}/disagreements/{disagreement_id}")
def get_disagreement(credit_file_id: str, disagreement_id: str) -> dict:
    _file_or_404(credit_file_id)
    record = pipeline.get_disagreement(credit_file_id, disagreement_id)
    return record.model_dump(mode="json")


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


# -- committee readiness (round 9) -----------------------------------
# Distinct from GET /readiness, the health probe: these routes serve
# the per-file ReadinessReport — computed fresh at the requested
# as_of, or fetched by id exactly as stored.


@app.get("/applications/{credit_file_id}/readiness-report")
def get_current_readiness_report(
    credit_file_id: str, as_of: date | None = None
) -> dict:
    _file_or_404(credit_file_id)
    report = pipeline.compute_readiness(credit_file_id, as_of=as_of)
    return report.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/readiness-reports")
def list_readiness_reports(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        r.model_dump(mode="json")
        for r in pipeline.list_readiness_reports(credit_file_id)
    ]


@app.get("/applications/{credit_file_id}/readiness-reports/{report_id}")
def get_readiness_report(credit_file_id: str, report_id: str) -> dict:
    _file_or_404(credit_file_id)
    report = pipeline.get_readiness_report(credit_file_id, report_id)
    return report.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/outstanding-items")
def get_outstanding_items(
    credit_file_id: str, as_of: date | None = None
) -> dict:
    _file_or_404(credit_file_id)
    report = pipeline.compute_readiness(credit_file_id, as_of=as_of)
    text = pipeline.outstanding_items(credit_file_id, as_of=as_of)
    return {
        "credit_file_id": credit_file_id,
        "readiness_report_id": report.report_id,
        "as_of": report.as_of.isoformat(),
        "text": text,
    }


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
    as_of: date | None = None


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
        as_of=body.as_of,
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
    as_of: date | None = None


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
        credit_file_id,
        memo_id,
        actor=body.actor,
        actor_role=body.actor_role,
        as_of=body.as_of,
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


# -- covenants (round 10) ---------------------------------------------
# The closing record, the sets approvals create, the derived
# schedule, the due-work queue, the scheduler tick, and amendment by
# supersession. Nothing here edits a covenant: the amendment route
# records a new version beside the old, exactly as the pipeline does.


class RecordClosingRequest(BaseModel):
    closing_date: date
    actor: str
    actor_role: str


@app.post("/applications/{credit_file_id}/closing", status_code=201)
def record_closing(credit_file_id: str, body: RecordClosingRequest) -> dict:
    _file_or_404(credit_file_id)
    credit_file = pipeline.record_closing(
        credit_file_id,
        closing_date=body.closing_date,
        actor=body.actor,
        actor_role=body.actor_role,
    )
    return credit_file.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/covenant-sets")
def list_covenant_sets(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        s.model_dump(mode="json")
        for s in pipeline.list_covenant_sets(credit_file_id)
    ]


@app.get("/applications/{credit_file_id}/covenant-sets/{set_id}")
def get_covenant_set(credit_file_id: str, set_id: str) -> dict:
    _file_or_404(credit_file_id)
    return pipeline.get_covenant_set(
        credit_file_id, set_id
    ).model_dump(mode="json")


def _schedule_view(schedule: dict) -> dict:
    return {
        "credit_file_id": schedule["credit_file_id"],
        "borrower": schedule["borrower"],
        "as_of": schedule["as_of"].isoformat(),
        "sets": [
            {
                "covenant_set": view["covenant_set"].model_dump(mode="json"),
                "current_covenants": [
                    c.model_dump(mode="json")
                    for c in view["current_covenants"]
                ],
                "instances": [
                    i.model_dump(mode="json") for i in view["instances"]
                ],
            }
            for view in schedule["sets"]
        ],
    }


@app.get("/applications/{credit_file_id}/covenant-schedule")
def get_covenant_schedule(
    credit_file_id: str, as_of: date, set_id: str | None = None
) -> dict:
    _file_or_404(credit_file_id)
    schedule = pipeline.covenant_schedule(
        credit_file_id, as_of=as_of, set_id=set_id
    )
    return _schedule_view(schedule)


@app.get("/applications/{credit_file_id}/covenant-amendments")
def list_covenant_amendments(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        a.model_dump(mode="json")
        for a in pipeline.list_covenant_amendments(credit_file_id)
    ]


class AmendCovenantRequest(BaseModel):
    author_id: str
    author_role: str
    reason_code: str
    decision_id: str
    threshold: Decimal | None = None
    operator: str | None = None
    frequency: str | None = None
    grace_days: int | None = None
    note: str = ""


@app.post(
    "/applications/{credit_file_id}/covenants/{covenant_id}/amendments",
    status_code=201,
)
def amend_covenant(
    credit_file_id: str, covenant_id: str, body: AmendCovenantRequest
) -> dict:
    _file_or_404(credit_file_id)
    record = pipeline.amend_covenant(
        credit_file_id,
        covenant_id,
        author_id=body.author_id,
        author_role=body.author_role,
        reason_code=body.reason_code,
        decision_id=body.decision_id,
        threshold=body.threshold,
        operator=body.operator,
        frequency=body.frequency,
        grace_days=body.grace_days,
        note=body.note,
    )
    return record.model_dump(mode="json")


def _review_instance_view(instance) -> dict:  # noqa: ANN001, ANN202
    """One derived annual review instance, as the API carries it."""
    return {
        "credit_file_id": instance.credit_file_id,
        "set_id": instance.set_id,
        "review_number": instance.review_number,
        "anchor_date": instance.anchor_date.isoformat(),
        "period_start": instance.period_start.isoformat(),
        "due_date": instance.due_date.isoformat(),
        "state": instance.state,
        "days_until_due": instance.days_until_due,
        "outcome_id": instance.outcome_id,
        "pack_id": instance.pack_id,
    }


@app.get("/covenant-due-work")
def get_covenant_due_work(as_of: date) -> list[dict]:
    out: list[dict] = []
    for entry in pipeline.covenant_due_work(as_of=as_of):
        item = {
            "borrower": entry["borrower"],
            "credit_file_id": entry["credit_file_id"],
            "kind": entry.get("kind", "covenant_schedule"),
        }
        if entry.get("instance") is not None:
            item["instance"] = entry["instance"].model_dump(mode="json")
        if entry.get("review_instance") is not None:
            item["review_instance"] = _review_instance_view(
                entry["review_instance"]
            )
        out.append(item)
    return out


class CovenantTickRequest(BaseModel):
    as_of: date
    credit_file_id: str | None = None


@app.post("/covenant-scheduler/tick")
def tick_covenants(body: CovenantTickRequest) -> dict:
    result = pipeline.tick_covenants(
        as_of=body.as_of, credit_file_id=body.credit_file_id
    )
    return {
        "as_of": result["as_of"].isoformat(),
        "sets_checked": result["sets_checked"],
        "crossings_recorded": [
            {
                **crossing,
                "test_date": crossing["test_date"].isoformat(),
            }
            for crossing in result["crossings_recorded"]
        ],
        "tests_recorded": [
            {
                "credit_file_id": entry["credit_file_id"],
                "borrower": entry["borrower"],
                "result": entry["result"].model_dump(mode="json"),
            }
            for entry in result["tests_recorded"]
        ],
        "breaches_opened": [
            {
                "credit_file_id": entry["credit_file_id"],
                "borrower": entry["borrower"],
                "breach": entry["breach"].model_dump(mode="json"),
            }
            for entry in result["breaches_opened"]
        ],
        "reviews_opened": [
            {
                "credit_file_id": entry["credit_file_id"],
                "borrower": entry["borrower"],
                "set_id": entry["set_id"],
                "review_number": entry["review_number"],
                "due_date": entry["due_date"].isoformat(),
                "pack_id": entry["pack_id"],
                "complete": entry["complete"],
            }
            for entry in result["reviews_opened"]
        ],
    }


# ------------------------------------------------------------------
# Round 11: covenant test execution and breach cases. The tick
# above also runs due tests; these endpoints run them per file,
# read the recorded results with their restatement standing, and
# work the breach queue — review, escalation, cure, waiver,
# default path — each a coded act by a named officer.
# ------------------------------------------------------------------


class RunCovenantTestsRequest(BaseModel):
    as_of: date


@app.post("/applications/{credit_file_id}/covenant-tests/run")
def run_covenant_tests(
    credit_file_id: str, body: RunCovenantTestsRequest
) -> dict:
    _file_or_404(credit_file_id)
    ran = pipeline.run_due_covenant_tests(
        credit_file_id, as_of=body.as_of
    )
    return {
        "credit_file_id": credit_file_id,
        "as_of": body.as_of.isoformat(),
        "results": [
            entry["result"].model_dump(mode="json")
            for entry in ran["results"]
        ],
        "breaches_opened": [
            entry["breach"].model_dump(mode="json")
            for entry in ran["breaches"]
        ],
    }


class RunCovenantTestRequest(BaseModel):
    as_of: date


@app.post(
    "/applications/{credit_file_id}/covenant-tests/{instance_id}/run"
)
def run_covenant_test(
    credit_file_id: str, instance_id: str, body: RunCovenantTestRequest
) -> dict:
    _file_or_404(credit_file_id)
    result = pipeline.run_covenant_test(
        credit_file_id, instance_id, as_of=body.as_of
    )
    return result.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/covenant-test-results")
def list_covenant_test_results(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return pipeline.covenant_test_results(credit_file_id)


@app.get(
    "/applications/{credit_file_id}/covenants/{covenant_id}/history"
)
def get_covenant_history(
    credit_file_id: str, covenant_id: str, as_of: date
) -> dict:
    _file_or_404(credit_file_id)
    return pipeline.covenant_history(
        credit_file_id, covenant_id, as_of=as_of
    )


@app.get("/breach-cases")
def list_breach_cases(
    as_of: date, credit_file_id: str | None = None
) -> list[dict]:
    if credit_file_id is not None:
        _file_or_404(credit_file_id)
    return pipeline.list_breach_cases(
        as_of=as_of, credit_file_id=credit_file_id
    )


@app.get("/applications/{credit_file_id}/breach-cases")
def list_file_breach_cases(
    credit_file_id: str, as_of: date
) -> list[dict]:
    _file_or_404(credit_file_id)
    return pipeline.list_breach_cases(
        as_of=as_of, credit_file_id=credit_file_id
    )


@app.get("/applications/{credit_file_id}/breach-cases/{breach_id}")
def get_breach_case(
    credit_file_id: str, breach_id: str, as_of: date
) -> dict:
    _file_or_404(credit_file_id)
    return pipeline.get_breach_case(
        credit_file_id, breach_id, as_of=as_of
    )


class BreachReviewRequest(BaseModel):
    actor_id: str
    actor_role: str
    note: str = ""


@app.post(
    "/applications/{credit_file_id}/breach-cases/{breach_id}/review",
    status_code=201,
)
def review_breach(
    credit_file_id: str, breach_id: str, body: BreachReviewRequest
) -> dict:
    _file_or_404(credit_file_id)
    breach = pipeline.review_breach(
        credit_file_id, breach_id,
        actor_id=body.actor_id, actor_role=body.actor_role,
        note=body.note,
    )
    return breach.model_dump(mode="json")


class BreachEscalateRequest(BaseModel):
    actor_id: str
    actor_role: str
    reason_code: str
    note: str = ""


@app.post(
    "/applications/{credit_file_id}/breach-cases/{breach_id}/escalate",
    status_code=201,
)
def escalate_breach(
    credit_file_id: str, breach_id: str, body: BreachEscalateRequest
) -> dict:
    _file_or_404(credit_file_id)
    breach = pipeline.escalate_breach(
        credit_file_id, breach_id,
        actor_id=body.actor_id, actor_role=body.actor_role,
        reason_code=body.reason_code, note=body.note,
    )
    return breach.model_dump(mode="json")


class BreachCureRequest(BaseModel):
    actor_id: str
    actor_role: str
    cure_result_id: str
    note: str = ""


@app.post(
    "/applications/{credit_file_id}/breach-cases/{breach_id}/cure",
    status_code=201,
)
def cure_breach(
    credit_file_id: str, breach_id: str, body: BreachCureRequest
) -> dict:
    _file_or_404(credit_file_id)
    breach = pipeline.cure_breach(
        credit_file_id, breach_id,
        actor_id=body.actor_id, actor_role=body.actor_role,
        cure_result_id=body.cure_result_id, note=body.note,
    )
    return breach.model_dump(mode="json")


class BreachWaiveRequest(BaseModel):
    actor_id: str
    actor_role: str
    decision_id: str
    note: str = ""


@app.post(
    "/applications/{credit_file_id}/breach-cases/{breach_id}/waive",
    status_code=201,
)
def waive_breach(
    credit_file_id: str, breach_id: str, body: BreachWaiveRequest
) -> dict:
    _file_or_404(credit_file_id)
    breach = pipeline.waive_breach(
        credit_file_id, breach_id,
        actor_id=body.actor_id, actor_role=body.actor_role,
        decision_id=body.decision_id, note=body.note,
    )
    return breach.model_dump(mode="json")


class BreachDefaultRequest(BaseModel):
    actor_id: str
    actor_role: str
    reason_code: str
    note: str = ""


@app.post(
    "/applications/{credit_file_id}/breach-cases/{breach_id}/default",
    status_code=201,
)
def default_breach(
    credit_file_id: str, breach_id: str, body: BreachDefaultRequest
) -> dict:
    _file_or_404(credit_file_id)
    breach = pipeline.default_breach(
        credit_file_id, breach_id,
        actor_id=body.actor_id, actor_role=body.actor_role,
        reason_code=body.reason_code, note=body.note,
    )
    return breach.model_dump(mode="json")


# ------------------------------------------------------------------
# Round 12: annual reviews. The obligations derive from each
# covenant set's anchor; the pack assembles in code from stored
# artefacts (its verified summary rides inside it); the outcome
# is a named officer's coded stance that closes the instance —
# and, when it escalates, the referral it created is readable
# here. The scheduler tick above also opens due reviews.
# ------------------------------------------------------------------


@app.get("/applications/{credit_file_id}/annual-reviews")
def get_annual_review_schedule(
    credit_file_id: str, as_of: date, set_id: str | None = None
) -> dict:
    _file_or_404(credit_file_id)
    schedule = pipeline.annual_review_schedule(
        credit_file_id, as_of=as_of, set_id=set_id
    )
    return {
        "credit_file_id": schedule["credit_file_id"],
        "borrower": schedule["borrower"],
        "as_of": schedule["as_of"].isoformat(),
        "sets": [
            {
                "covenant_set": view["covenant_set"].model_dump(
                    mode="json"
                ),
                "anchor_date": view["anchor_date"].isoformat(),
                "anchor_basis": view["anchor_basis"],
                "instances": [
                    _review_instance_view(instance)
                    for instance in view["instances"]
                ],
            }
            for view in schedule["sets"]
        ],
    }


class AssembleReviewPackRequest(BaseModel):
    as_of: date
    set_id: str | None = None
    profile: str = "offline"


@app.post(
    "/applications/{credit_file_id}/annual-reviews/{review_number}/pack",
    status_code=201,
)
def assemble_review_pack(
    credit_file_id: str, review_number: int,
    body: AssembleReviewPackRequest,
) -> dict:
    _file_or_404(credit_file_id)
    pack = pipeline.assemble_review_pack(
        credit_file_id,
        review_number=review_number,
        set_id=body.set_id,
        as_of=body.as_of,
        profile=body.profile,
    )
    return pack.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/annual-review-packs")
def list_review_packs(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        pack.model_dump(mode="json")
        for pack in pipeline.list_review_packs(credit_file_id)
    ]


@app.get(
    "/applications/{credit_file_id}/annual-review-packs/{pack_id}"
)
def get_review_pack(credit_file_id: str, pack_id: str) -> dict:
    _file_or_404(credit_file_id)
    return pipeline.get_review_pack(
        credit_file_id, pack_id
    ).model_dump(mode="json")


class RecordReviewOutcomeRequest(BaseModel):
    stance: str
    reason_codes: list[str]
    reviewer_id: str
    reviewer_role: str
    pack_id: str | None = None
    set_id: str | None = None
    as_of: date | None = None
    countersigner_id: str | None = None
    countersigner_role: str | None = None
    conditions: list[CommitteeCondition] = []
    note: str = ""


@app.post(
    "/applications/{credit_file_id}/annual-reviews/{review_number}/outcome",
    status_code=201,
)
def record_review_outcome(
    credit_file_id: str, review_number: int,
    body: RecordReviewOutcomeRequest,
) -> dict:
    _file_or_404(credit_file_id)
    outcome = pipeline.record_review_outcome(
        credit_file_id,
        review_number=review_number,
        stance=body.stance,
        reason_codes=body.reason_codes,
        reviewer_id=body.reviewer_id,
        reviewer_role=body.reviewer_role,
        pack_id=body.pack_id,
        set_id=body.set_id,
        as_of=body.as_of,
        countersigner_id=body.countersigner_id,
        countersigner_role=body.countersigner_role,
        conditions=body.conditions,
        note=body.note,
    )
    return outcome.model_dump(mode="json")


@app.get("/applications/{credit_file_id}/annual-review-outcomes")
def list_review_outcomes(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        outcome.model_dump(mode="json")
        for outcome in pipeline.list_review_outcomes(credit_file_id)
    ]


@app.get("/applications/{credit_file_id}/committee-referrals")
def list_committee_referrals(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        referral.model_dump(mode="json")
        for referral in pipeline.list_committee_referrals(credit_file_id)
    ]


@app.get("/annual-review-due-work")
def get_annual_review_due_work(as_of: date) -> list[dict]:
    return [
        {
            "borrower": entry["borrower"],
            "credit_file_id": entry["credit_file_id"],
            "review_instance": _review_instance_view(entry["instance"]),
        }
        for entry in pipeline.review_due_work(as_of=as_of)
    ]


class OpenDueReviewsRequest(BaseModel):
    as_of: date
    credit_file_id: str | None = None


# ------------------------------------------------------------------
# Round 13: risk ratings. The coded scorecard assigns over the
# file's current computation pack; a named officer may override
# the final grade under the scorecard's discipline (at most one
# grade from the assigned grade, coded reason, countersigned);
# the history view labels cross-version comparisons honestly.
# The rating is an input to the memo and the review pack — no
# endpoint here decides anything, and no gate reads these.
# ------------------------------------------------------------------


def _rating_view_json(view: dict) -> dict:
    return {
        "assignment": view["assignment"].model_dump(mode="json"),
        "assigned_grade": view["assigned_grade"],
        "assigned_band": view["assigned_band"],
        "final_grade": view["final_grade"],
        "final_band": view["final_band"],
        "override": (
            view["override"].model_dump(mode="json")
            if view["override"] else None
        ),
        "overrides": [
            o.model_dump(mode="json") for o in view["overrides"]
        ],
    }


class AssignRatingRequest(BaseModel):
    as_of: date
    scorecard_version: str | None = None


@app.post(
    "/applications/{credit_file_id}/risk-rating", status_code=201
)
def assign_rating(
    credit_file_id: str, body: AssignRatingRequest
) -> dict:
    _file_or_404(credit_file_id)
    assignment = pipeline.assign_rating(
        credit_file_id,
        as_of=body.as_of,
        scorecard_version=body.scorecard_version,
    )
    return _rating_view_json(pipeline.rating_view(assignment))


@app.get("/applications/{credit_file_id}/risk-ratings")
def list_ratings(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return [
        _rating_view_json(pipeline.rating_view(assignment))
        for assignment in pipeline.list_rating_assignments(
            credit_file_id
        )
    ]


@app.get(
    "/applications/{credit_file_id}/risk-ratings/{assignment_id}"
)
def get_rating(credit_file_id: str, assignment_id: str) -> dict:
    _file_or_404(credit_file_id)
    assignment = pipeline.get_rating_assignment(
        credit_file_id, assignment_id
    )
    return _rating_view_json(pipeline.rating_view(assignment))


@app.get("/applications/{credit_file_id}/risk-rating-history")
def get_rating_history(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    history = []
    for view in pipeline.rating_history(credit_file_id):
        entry = _rating_view_json(view)
        entry["scorecard_version"] = view["scorecard_version"]
        entry["cross_version"] = view["cross_version"]
        entry["comparison_basis"] = view["comparison_basis"]
        history.append(entry)
    return history


class OverrideRatingRequest(BaseModel):
    officer_id: str
    officer_role: str
    new_grade: int
    reason_code: str
    countersigner_id: str | None = None
    countersigner_role: str | None = None
    note: str = ""


@app.post(
    "/applications/{credit_file_id}/risk-ratings/{assignment_id}"
    "/override",
    status_code=201,
)
def override_rating(
    credit_file_id: str, assignment_id: str,
    body: OverrideRatingRequest,
) -> dict:
    _file_or_404(credit_file_id)
    override = pipeline.override_rating(
        credit_file_id,
        assignment_id,
        officer_id=body.officer_id,
        officer_role=body.officer_role,
        new_grade=body.new_grade,
        reason_code=body.reason_code,
        countersigner_id=body.countersigner_id,
        countersigner_role=body.countersigner_role,
        note=body.note,
    )
    return override.model_dump(mode="json")


@app.post("/annual-reviews/open-due")
def open_due_reviews(body: OpenDueReviewsRequest) -> dict:
    if body.credit_file_id is not None:
        _file_or_404(body.credit_file_id)
    result = pipeline.open_due_reviews(
        as_of=body.as_of, credit_file_id=body.credit_file_id
    )
    return {
        "as_of": result["as_of"].isoformat(),
        "reviews_opened": [
            {
                "credit_file_id": entry["credit_file_id"],
                "borrower": entry["borrower"],
                "set_id": entry["set_id"],
                "review_number": entry["review_number"],
                "due_date": entry["due_date"].isoformat(),
                "pack_id": entry["pack_id"],
                "complete": entry["complete"],
            }
            for entry in result["reviews_opened"]
        ],
    }


# ------------------------------------------------------------------
# Round 14: the officer console. A single static page and the
# read aggregates it renders. The page's actions all post to the
# gated routes above — there is deliberately no console write
# route in this section, and the round 14 scan grades that:
# every path the page calls must already exist in the route
# table as a read aggregate here or a gated route from an
# earlier round.
# ------------------------------------------------------------------


@app.get("/console", response_class=HTMLResponse)
def console_page() -> str:
    return CONSOLE_HTML


@app.get("/console/vocabularies")
def console_vocabularies() -> dict:
    return vocabularies_view()


@app.get("/console/queue")
def console_queue(as_of: date) -> list[dict]:
    return queue_view(pipeline, as_of=as_of)


@app.get("/console/workspace/{credit_file_id}")
def console_workspace(credit_file_id: str, as_of: date) -> dict:
    _file_or_404(credit_file_id)
    return workspace_view(pipeline, credit_file_id, as_of=as_of)


# ------------------------------------------------------------------
# Round 15: the model gateway. Reads only — the profiles the
# deployment holds (variable names and configured flags, never
# key values), a file's priced call ledger, its cost rollup with
# the budget position, and the portfolio's daily spend. Calls
# happen through the gated drafting routes above; a file whose
# priced budget is spent gets 429 from those routes, and the
# refusal itself is readable here (ledger + audit trail).
# ------------------------------------------------------------------


@app.get("/model/profiles")
def list_model_profiles() -> list[dict]:
    return pipeline.model_profiles()


@app.get("/model-costs/daily")
def get_portfolio_model_costs() -> list[dict]:
    return pipeline.portfolio_model_costs()


@app.get("/applications/{credit_file_id}/model-ledger")
def get_file_model_ledger(credit_file_id: str) -> list[dict]:
    _file_or_404(credit_file_id)
    return pipeline.file_model_ledger(credit_file_id)


@app.get("/applications/{credit_file_id}/model-costs")
def get_file_model_costs(credit_file_id: str) -> dict:
    _file_or_404(credit_file_id)
    return pipeline.file_model_costs(credit_file_id)
