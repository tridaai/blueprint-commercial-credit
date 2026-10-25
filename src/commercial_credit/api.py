"""HTTP API for the Commercial Credit Memo & Covenant Monitoring Agent.

Round 1 surface: health/readiness, application intake (LOS reference
or inline borrower + facility), the generated checklist, document
registration with hash recording, verified reads, the extraction
pass, and the audit trail. Every request carries a request id into
the structured logs.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import load_settings
from .documents import read_document_verified, register_document
from .errors import CreditError, NotFoundError, ValidationError
from .logging_setup import configure_logging, request_id_var
from .models import Borrower, DocumentKind, FacilityRequest
from .pipeline import CreditPipeline
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
