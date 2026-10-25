"""Domain models for the commercial credit file.

Round 1 scope: the credit file itself — borrower, facility request,
the document checklist generated from the facility type, source
documents with content hashes, and provenance-carrying extracted
values. Spreading, ratios, policy tests and memos are later rounds;
the models here are shaped so those rounds extend, not replace, them.

Money is Decimal everywhere. Floats never carry a money value.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

from .errors import ProvenanceError


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FacilityType(str, Enum):
    TERM_LOAN = "term_loan"
    REVOLVING_LINE = "revolving_line"
    CRE_LOAN = "cre_loan"
    CONSTRUCTION_LOAN = "construction_loan"
    EQUIPMENT_FINANCE = "equipment_finance"
    SBA_7A = "sba_7a"


class BorrowerSegment(str, Enum):
    MANUFACTURING = "manufacturing"
    DISTRIBUTION = "distribution"
    HEALTHCARE = "healthcare"
    HOSPITALITY = "hospitality"
    RETAIL = "retail"
    PROFESSIONAL_SERVICES = "professional_services"
    REAL_ESTATE = "real_estate"
    OTHER = "other"


class DocumentKind(str, Enum):
    FINANCIAL_STATEMENT = "financial_statement"
    TAX_RETURN = "tax_return"
    PERSONAL_FINANCIAL_STATEMENT = "personal_financial_statement"
    RENT_ROLL = "rent_roll"
    APPRAISAL = "appraisal"
    ENVIRONMENTAL_REPORT = "environmental_report"
    LEASES = "leases"
    ORG_DOCUMENT = "org_document"
    DEBT_SCHEDULE = "debt_schedule"
    AR_AGING = "ar_aging"
    INVENTORY_LISTING = "inventory_listing"
    BORROWING_BASE_CERTIFICATE = "borrowing_base_certificate"
    PROJECTIONS = "projections"
    CONSTRUCTION_BUDGET = "construction_budget"
    GC_CONTRACT = "gc_contract"
    PERMITS = "permits"
    EQUIPMENT_INVOICE = "equipment_invoice"
    SBA_FORM = "sba_form"
    INSURANCE_CERTIFICATE = "insurance_certificate"
    OTHER = "other"


class Borrower(BaseModel):
    legal_name: str
    dba: str | None = None
    ein_last4: str = Field(pattern=r"^\d{4}$")
    segment: BorrowerSegment
    state_of_formation: str = Field(min_length=2, max_length=2)
    years_in_business: int = Field(ge=0)
    primary_contact_name: str
    primary_contact_email: str


class Guarantor(BaseModel):
    name: str
    ownership_pct: Decimal = Field(ge=Decimal("0"), le=Decimal("100"))


class FacilityRequest(BaseModel):
    facility_type: FacilityType
    amount: Decimal = Field(gt=Decimal("0"))
    purpose: str
    term_months: int = Field(gt=0)
    collateral_description: str | None = None
    guarantors: list[Guarantor] = Field(default_factory=list)


class ChecklistItem(BaseModel):
    """One required document on a credit file's checklist.

    Generated deterministically from the facility type (checklist.py);
    `requirement_source` names the rule that put it there so an
    examiner can trace the requirement, not just the document.
    """

    item_id: str
    kind: DocumentKind
    label: str
    required: bool = True
    requirement_source: str
    periods_required: int = 1
    satisfied_by_document_id: str | None = None

    @property
    def satisfied(self) -> bool:
        return self.satisfied_by_document_id is not None


class SourceDocument(BaseModel):
    """A borrower document as registered in the credit file.

    Bytes live in object storage behind the document port; this record
    carries the SHA-256 of those bytes, recorded at intake. Any later
    read re-verifies the hash before the document is used.
    """

    document_id: str
    credit_file_id: str
    kind: DocumentKind
    filename: str
    content_type: str = "application/json"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)
    storage_key: str
    period_label: str | None = None
    received_at: datetime = Field(default_factory=utcnow)


class Provenance(BaseModel):
    """Where an extracted value came from: document, page, line/cell.

    All three coordinates are mandatory. A value whose source cannot
    be pointed at this precisely is not evidence and never enters a
    spread (design law 1).
    """

    document_id: str = Field(min_length=1)
    page: int = Field(ge=1)
    line: str | None = None
    cell: str | None = None

    @model_validator(mode="after")
    def _needs_line_or_cell(self) -> "Provenance":
        if not (self.line and self.line.strip()) and not (
            self.cell and self.cell.strip()
        ):
            raise ProvenanceError(
                "provenance requires a line or a cell coordinate "
                f"(document {self.document_id}, page {self.page})"
            )
        return self


class ExtractedValue(BaseModel):
    """One figure read from a borrower document, with its provenance.

    `value` is Decimal — money never travels as a float. The
    extraction port contract (extraction.py) rejects any value whose
    provenance is missing or incomplete; this model's validators are
    the second line of that defence, not the only one.
    """

    field: str = Field(min_length=1)
    value: Decimal
    unit: str = "USD"
    period_label: str
    fiscal_year: int | None = None
    provenance: Provenance

    @field_validator("provenance", mode="before")
    @classmethod
    def _provenance_must_exist(cls, v: object) -> object:
        if v is None:
            raise ProvenanceError("extracted value has no provenance")
        return v


def require_complete_provenance(values: list[ExtractedValue]) -> None:
    """Port-contract check: raise ProvenanceError on the first value
    whose provenance is incomplete. Defence in depth behind the model
    validators — extraction adapters call this on their own output
    before returning, and the intake service calls it again on receipt.
    """
    for v in values:
        p = v.provenance
        if p is None or not p.document_id or p.page < 1 or not (
            (p.line and p.line.strip()) or (p.cell and p.cell.strip())
        ):
            raise ProvenanceError(
                f"extraction value {v.field!r} lacks complete provenance"
            )


class CreditFileStatus(str, Enum):
    INTAKE = "intake"
    COLLECTING_DOCUMENTS = "collecting_documents"
    READY_FOR_SPREADING = "ready_for_spreading"


class CreditFile(BaseModel):
    credit_file_id: str
    borrower: Borrower
    facility: FacilityRequest
    status: CreditFileStatus = CreditFileStatus.INTAKE
    checklist: list[ChecklistItem] = Field(default_factory=list)
    documents: list[SourceDocument] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def checklist_item(self, item_id: str) -> ChecklistItem | None:
        for item in self.checklist:
            if item.item_id == item_id:
                return item
        return None

    def missing_required(self) -> list[ChecklistItem]:
        return [i for i in self.checklist if i.required and not i.satisfied]

    def document(self, document_id: str) -> SourceDocument | None:
        for doc in self.documents:
            if doc.document_id == document_id:
                return doc
        return None


class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    credit_file_id: str
    actor: str
    action: str
    detail: str = ""
    at: datetime = Field(default_factory=utcnow)


class FiscalPeriod(BaseModel):
    label: str
    fiscal_year: int
    period_end: date
    interim: bool = False
