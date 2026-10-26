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

from .errors import ProvenanceError, ValidationError


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
    # Which kind of source the value was read from (round 8): the
    # audited statements, the tax return, or the management
    # statements — disagreements.py's vocabulary. The extractor
    # stamps it; the spreading engine falls back to the credit
    # file's document record for values that arrive unstamped.
    # It is provenance metadata: two values for the same line from
    # different source kinds are a disagreement to carry, never
    # two versions of one truth to merge.
    source_type: str | None = None
    # The line label exactly as it appeared in the source document.
    # Mapped values name their canonical field in `field`; a line the
    # spreading taxonomy does not map keeps its label here and travels
    # with field "unmapped:<slug>" so it is carried, never dropped.
    source_label: str | None = None

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


class PolicyHistoryEntry(BaseModel):
    """One line of a credit file's policy history (round 4).

    Evaluating a file against one pinned policy pack version records
    an entry here: the pack, the version, the report the evaluation
    produced, and the as-of date the file facts were read at. A later
    evaluation under a newer pack version appends its own entry —
    history is never rewritten, which is what lets an in-flight file
    keep its v1 report after v2 ships (design law 2).
    """

    policy_pack_id: str
    policy_version: str
    report_id: str
    as_of: date


class CreditFileStatus(str, Enum):
    INTAKE = "intake"
    COLLECTING_DOCUMENTS = "collecting_documents"
    READY_FOR_SPREADING = "ready_for_spreading"


class CreditFile(BaseModel):
    credit_file_id: str
    borrower: Borrower
    facility: FacilityRequest
    status: CreditFileStatus = CreditFileStatus.INTAKE
    # Round 10: the facility's closing date, recorded as a file
    # fact when the loan closes. It is the anchor every covenant
    # test date is computed from — a set built before it exists is
    # created in the named `awaiting_closing_date` state rather
    # than dated by invention. Recorded via the pipeline (named
    # actor, audit event); never edited once set — a corrected
    # closing date is a new recording that supersedes in the audit
    # trail, and covenant sets pin the date they were built from.
    closing_date: date | None = None
    # Round 17: the named analyst the file is assigned to, when it
    # is. Assignment is a recorded act (an audit event names who
    # assigned it and when); retrieval access for assigned-scope
    # roles keys off this field. None = the unassigned pickup
    # queue, visible to assigned-scope roles until someone claims
    # the file. The field changes only through the assign act —
    # never as a side effect of a read.
    assigned_to: str | None = None
    checklist: list[ChecklistItem] = Field(default_factory=list)
    documents: list[SourceDocument] = Field(default_factory=list)
    policy_history: list[PolicyHistoryEntry] = Field(default_factory=list)
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


# ---------------------------------------------------------------------
# Round 2: spreading. The spread is a deterministic function of the
# extractions and the adjustments — it carries no timestamps and no
# random ids, so the same inputs rebuild it byte-identically.
# ---------------------------------------------------------------------


class AdjustmentReasonCode(str, Enum):
    """Why a spread line was adjusted. Coded, never free-form alone:
    the free-text reason rides alongside, and both are required."""

    NON_RECURRING_EXPENSE = "non_recurring_expense"
    NON_RECURRING_GAIN = "non_recurring_gain"
    EXCESS_OWNER_COMPENSATION = "excess_owner_compensation"
    ONE_TIME_ITEM = "one_time_item"
    PRO_FORMA_ADJUSTMENT = "pro_forma_adjustment"
    RECLASSIFICATION = "reclassification"
    OTHER = "other"


class SpreadAdjustment(BaseModel):
    """An adjustment / add-back to one spread line for one period.

    First-class record, not an edit to the extracted figure: the
    unadjusted value stays in the spread untouched and the adjusted
    view is derived. An adjustment must justify itself — the spread
    line it touches, a coded reason AND a free-text reason, the
    document provenance that supports it, and who or what proposed
    it. `require_valid_adjustment` enforces that on receipt, the way
    `require_complete_provenance` guards extractions.
    """

    adjustment_id: str = Field(default_factory=lambda: new_id("adj"))
    credit_file_id: str
    line_key: str = Field(min_length=1)
    fiscal_year: int
    amount: Decimal
    reason_code: AdjustmentReasonCode
    reason: str
    source: Provenance
    proposed_by: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def _reason_must_say_something(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValidationError("an adjustment requires a free-text reason")
        return v


def require_valid_adjustment(adjustment: SpreadAdjustment) -> None:
    """Receiving-side check for adjustments, defence in depth behind
    the model validators (which a model_construct call can bypass).
    Raises ValidationError on the first defect found."""
    if not isinstance(adjustment.source, Provenance):
        raise ValidationError(
            f"adjustment {adjustment.adjustment_id} has no source provenance"
        )
    p = adjustment.source
    if not p.document_id or p.page < 1 or not (
        (p.line and p.line.strip()) or (p.cell and p.cell.strip())
    ):
        raise ValidationError(
            f"adjustment {adjustment.adjustment_id} source provenance is incomplete"
        )
    if not adjustment.reason or not adjustment.reason.strip():
        raise ValidationError(
            f"adjustment {adjustment.adjustment_id} has no free-text reason"
        )
    if adjustment.reason_code is None:
        raise ValidationError(
            f"adjustment {adjustment.adjustment_id} has no reason code"
        )
    if not adjustment.proposed_by or not adjustment.proposed_by.strip():
        raise ValidationError(
            f"adjustment {adjustment.adjustment_id} does not name who proposed it"
        )
    if not adjustment.line_key or not adjustment.line_key.strip():
        raise ValidationError(
            f"adjustment {adjustment.adjustment_id} names no spread line"
        )


class PeriodSource(BaseModel):
    """Which document version one spread period rests on, per
    source type (round 8).

    When a borrower files a corrected statement for a period, the
    new version supersedes the earlier extraction *of the same
    source type* for that period; the spread records the version
    it used and names the superseded documents, whose extracted
    values remain in the store. A document of a different source
    type — the tax return next to the audited statements — never
    supersedes anything: it stands alongside, and where the two
    disagree the spread records a Disagreement (below).
    """

    fiscal_year: int
    period_label: str
    document_id: str
    document_sha256: str
    version: int = Field(ge=1)
    superseded_document_ids: list[str] = Field(default_factory=list)
    source_type: str = "audited_statement"


class DisagreementSource(BaseModel):
    """One source's figure inside a disagreement (round 8).

    The value exactly as that source reported it, where it was
    read from, and its distance from the figure the spread
    carries: `delta_abs` is |value − working value| and
    `delta_rel` that distance over the working value (None when
    the working value is zero, where a relative distance has no
    meaning). The working source itself is listed too, with zero
    deltas — the record shows every figure, not just the losers.
    """

    source_type: str
    document_id: str
    document_sha256: str
    value: Decimal
    provenance: Provenance
    delta_abs: Decimal
    delta_rel: Decimal | None = None


class Disagreement(BaseModel):
    """Two source types reporting different figures for one spread
    line in one period (round 8; design law 5).

    The disagreement is a first-class record on the spread, not a
    footnote: it names the line and the period, carries EVERY
    reporting source's value with its provenance (the figure the
    spread works from and each figure it set aside), and records
    the materiality verdict the disagreement policy reached. The
    values are never averaged and the working value is never
    silently picked: the precedence that chose it is the policy's,
    stated in disagreements.py, and this record is how any
    consumer of the spread — the computation engine, the memo,
    an examiner — sees that the line is contested.
    """

    disagreement_id: str
    credit_file_id: str
    line_key: str
    line_label: str
    fiscal_year: int
    working_source_type: str
    working_value: Decimal
    sources: list[DisagreementSource] = Field(default_factory=list)
    material: bool
    policy_version: str

    def source(self, source_type: str) -> DisagreementSource | None:
        for source in self.sources:
            if source.source_type == source_type:
                return source
        return None


class SpreadLine(BaseModel):
    """One template line across all spread periods.

    `values` is the unadjusted view (exactly what extraction read);
    `adjusted_values` is `values` plus the period's adjustments.
    Both are always present so either view is derivable from the
    spread alone. `changes` is period-over-period presentation data
    on the unadjusted view (None where either side is unreported).
    """

    key: str
    label: str
    section: str
    values: dict[int, Decimal | None]
    adjusted_values: dict[int, Decimal | None]
    changes: dict[int, Decimal | None]


class UnmappedEntry(BaseModel):
    """A statement line the taxonomy does not map, carried as itself.

    Never dropped, never silently folded into a mapped line: the
    entry keeps the label as it appeared in the document, its values
    per period, and where each value was read from.
    """

    field: str
    name: str
    values: dict[int, Decimal | None]
    sources: dict[int, Provenance]


class Spread(BaseModel):
    """A multi-period spread of one credit file.

    `spread_id` is derived from the spread's canonical content, so
    rebuilding from the same extractions and adjustments reproduces
    the same id and the same bytes.
    """

    spread_id: str
    credit_file_id: str
    template_version: str
    periods: list[int]
    period_sources: list[PeriodSource]
    lines: list[SpreadLine]
    unmapped: list[UnmappedEntry] = Field(default_factory=list)
    adjustments: list[SpreadAdjustment] = Field(default_factory=list)
    # Round 8: the disagreements between source types the spread
    # carries, and the version of the disagreement policy that
    # judged them. Part of the spread's content, so the spread id
    # covers them: a rebuild that sees different sources is a
    # different spread, and a consumer can never be handed the
    # lines without the contests recorded on them.
    disagreements: list[Disagreement] = Field(default_factory=list)
    disagreement_policy_version: str = ""


# ---------------------------------------------------------------------
# Round 3: the computation engine. Every figure the engine produces
# is a stored artefact carrying its full derivation: a chain of steps
# whose inputs name spread lines (with their provenance chain),
# supplemental figures (with provenance), earlier figures, or — in a
# global pack — named eliminations. A figure whose required inputs
# are absent is not estimated: it is recorded as not computable,
# naming what is missing.
# ---------------------------------------------------------------------


class StepInput(BaseModel):
    """One input to one computation step.

    `kind` says where the value came from:
      spread_line — a cell of the spread (line, period, view), with
                    the provenance chain of the extraction that fed
                    the cell and the ids of any adjustments folded
                    into an adjusted-view value;
      supplemental — a figure supplied to the computation run itself
                    (e.g. capital lease payments from a debt
                    schedule), carrying its own provenance;
      figure      — the result of another computed figure (this pack,
                    or an entity pack of a global combination);
      step        — the result of an earlier step of the same figure;
      elimination — a named global-cash-flow elimination.
    """

    kind: str
    value: Decimal
    line_key: str | None = None
    fiscal_year: int | None = None
    view: str | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    adjustment_ids: list[str] = Field(default_factory=list)
    figure_key: str | None = None
    pack_id: str | None = None
    entity: str | None = None
    step_key: str | None = None
    elimination_name: str | None = None


class ComputationStep(BaseModel):
    """One recorded arithmetic step of a figure's derivation.

    Results are quantized as they are recorded — money to the cent,
    ratios to 4 decimal places, ROUND_HALF_UP — so the trace replays
    to exactly the figure the pack reports.
    """

    step_key: str
    label: str
    operation: str  # carry | add | subtract | sum | divide
    inputs: list[StepInput]
    result: Decimal
    notes: str = ""


class ComputedFigure(BaseModel):
    """One computed figure: a ratio, a coverage measure, or a
    derived money amount (EBITDA, debt service, global cash flow…).

    `status` is "computed" only when every required input was
    present; otherwise the figure names its missing inputs and
    carries no value. There is no third state — a figure is never
    quietly approximated.
    """

    key: str
    label: str
    unit: str  # "USD" | "ratio"
    fiscal_year: int | None = None
    entity: str = ""
    value: Decimal | None = None
    status: str  # computed | not_computable
    missing_inputs: list[str] = Field(default_factory=list)
    steps: list[ComputationStep] = Field(default_factory=list)
    notes: str = ""
    # Round 8: the ids of the MATERIAL document disagreements this
    # figure rests on, when any input in its derivation — directly,
    # or through a figure it consumes — reads a spread line a
    # disagreement contests. The figure still computes, from the
    # spread's working value; the flag means it can never present
    # as a clean figure: the memo verifier names the disagreement
    # in the figure's trace.
    disagreement_ids: list[str] = Field(default_factory=list)


class CovenantDefinition(BaseModel):
    """A covenant test supplied to the computation run: the metric,
    the operator, the threshold, the period it applies to.

    Definitions are *given* to this round — the covenant store and
    its scheduler are round 10, the policy pack round 4. The engine
    computes headroom against whatever definitions it is handed and
    records them in the pack, so a headroom figure always names the
    definition it was measured against.
    """

    name: str = Field(min_length=1)
    metric_key: str = Field(min_length=1)
    operator: str = Field(pattern=r"^(>=|<=|>|<)$")
    threshold: Decimal
    fiscal_year: int
    require_capital_leases: bool = False


class CovenantResult(BaseModel):
    """Headroom of one metric against one given definition.

    `headroom` is the signed distance from the metric to the safe
    side of the threshold (value − threshold for a floor, threshold
    − value for a ceiling); negative headroom is a breach on the
    historical figures. `headroom_pct` is headroom over |threshold|.
    """

    name: str
    metric_key: str
    fiscal_year: int
    operator: str
    threshold: Decimal
    value: Decimal | None = None
    status: str  # computed | not_computable
    compliant: bool | None = None
    headroom: Decimal | None = None
    headroom_pct: Decimal | None = None
    missing_inputs: list[str] = Field(default_factory=list)


class Elimination(BaseModel):
    """A named elimination in a global (combined) cash-flow view.

    Combining a borrower with its guarantors double-counts whatever
    flows between them — rent the borrower pays a guarantor's
    property company, intercompany debt service. An elimination
    removes a named amount from the combined cash flow or the
    combined debt service, for a stated reason. It is a record, not
    an edit: both entities' own figures stand untouched.
    """

    name: str = Field(min_length=1)
    fiscal_year: int
    amount: Decimal
    target: str = Field(pattern=r"^(cash_flow|debt_service)$")
    reason: str


def require_valid_elimination(elimination: Elimination) -> None:
    """Receiving-side check for eliminations, defence in depth
    behind the model validators. An elimination without a name,
    without a reason, or of a non-positive amount is refused — an
    unexplained removal from a combined view is exactly what the
    record exists to prevent."""
    if not elimination.name or not elimination.name.strip():
        raise ValidationError("an elimination requires a name")
    if not elimination.reason or not elimination.reason.strip():
        raise ValidationError(
            f"elimination {elimination.name!r} requires a stated reason"
        )
    if elimination.amount is None or elimination.amount <= 0:
        raise ValidationError(
            f"elimination {elimination.name!r} must remove a positive amount"
        )
    if elimination.target not in ("cash_flow", "debt_service"):
        raise ValidationError(
            f"elimination {elimination.name!r} targets "
            f"{elimination.target!r}, not cash_flow or debt_service"
        )


class ExclusionRecord(BaseModel):
    """A guarantor named for a global view but left out of it, with
    the reason on the record. Exclusion is only ever explicit: the
    request must opt in, and the pack carries the record forever."""

    entity: str
    reason: str


class ComputationPack(BaseModel):
    """The stored artefact of one computation run.

    Like the spread, the pack is content-addressed: `inputs_hash`
    covers the engine version, the spread it ran over, the view,
    every supplemental figure and covenant definition (and, for a
    global pack, the entity packs combined and the eliminations
    applied), and `pack_id` is derived from it. Recomputing the same
    inputs returns the identical pack — the same id, the same bytes.
    The pack carries no timestamps and no random ids.
    """

    pack_id: str
    credit_file_id: str
    spread_id: str
    entity: str
    scope: str  # entity | global
    engine_version: str
    view: str  # adjusted | unadjusted
    inputs_hash: str
    entities: list[str] = Field(default_factory=list)
    entity_pack_ids: list[str] = Field(default_factory=list)
    supplemental: list[ExtractedValue] = Field(default_factory=list)
    covenant_definitions: list[CovenantDefinition] = Field(default_factory=list)
    eliminations: list[Elimination] = Field(default_factory=list)
    exclusions: list[ExclusionRecord] = Field(default_factory=list)
    figures: list[ComputedFigure] = Field(default_factory=list)
    covenant_results: list[CovenantResult] = Field(default_factory=list)


# ---------------------------------------------------------------------
# Round 4: the policy pack. Lending policy is code, versioned
# (design law 2): a pack is an identified, dated set of coded tests
# over computation-pack figures and credit-file facts. Evaluating a
# file pins ONE pack version and produces a PolicyReport — itself a
# content-addressed artefact — whose every outcome is pass, fail, or
# not_evaluable with the missing input named. A test whose figure
# could not be computed is never a silent pass.
# ---------------------------------------------------------------------


class PolicyTest(BaseModel):
    """One coded lending-policy test.

    `kind` says what the test reads:
      figure    — a ComputedFigure from a computation pack (the
                  borrower's own pack, or the global pack when
                  `scope` is "global"), compared against a threshold
                  with an operator;
      file_fact — a fact of the credit file itself: its checklist,
                  its facility, its documents and their dates.

    Every test names its exact inputs here, carries a severity
    (blocker — the file cannot proceed on a fail — or warning), and
    cites the policy text it encodes.
    """

    test_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = Field(pattern=r"^(figure|file_fact)$")
    severity: str = Field(pattern=r"^(blocker|warning)$")
    citation: str = Field(min_length=1)
    # figure tests
    figure_key: str | None = None
    scope: str = "borrower"  # borrower | global
    operator: str | None = Field(default=None, pattern=r"^(>=|<=|>|<)$")
    threshold: Decimal | None = None
    segment_thresholds: dict[str, Decimal] | None = None
    # file-fact tests
    fact: str | None = None  # required_documents | statement_age | appraisal_age | concentration
    limit: Decimal | None = None
    applies_to_facility_types: list[str] = Field(default_factory=list)
    requires_guarantors: bool = False

    @model_validator(mode="after")
    def _inputs_named(self) -> "PolicyTest":
        if self.kind == "figure":
            if not self.figure_key:
                raise ValidationError(
                    f"policy test {self.test_id!r} is a figure test "
                    "but names no figure"
                )
            if not self.operator:
                raise ValidationError(
                    f"policy test {self.test_id!r} names no operator"
                )
            if self.threshold is None and not self.segment_thresholds:
                raise ValidationError(
                    f"policy test {self.test_id!r} names no threshold "
                    "and no segment threshold table"
                )
        else:
            if not self.fact:
                raise ValidationError(
                    f"policy test {self.test_id!r} is a file-fact test "
                    "but names no fact"
                )
            if self.fact != "required_documents" and self.limit is None:
                raise ValidationError(
                    f"policy test {self.test_id!r} names no limit"
                )
        return self


class PolicyPack(BaseModel):
    """One version of the bank's lending policy, as data + code.

    A pack is identified (pack id + version), dated (effective
    window), and issued with a note. `capital_base` is the bank's
    stated capital base the concentration test measures a single
    borrower's facility against. Packs are immutable once shipped:
    a policy change is a new version, never an edit — earlier
    reports keep the pack they were evaluated under.
    """

    pack_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    effective_from: date
    effective_to: date | None = None
    issuing_note: str = Field(min_length=1)
    capital_base: Decimal = Field(gt=Decimal("0"))
    tests: list[PolicyTest] = Field(min_length=1)


class PolicyInputValue(BaseModel):
    """One input a policy test actually read, on the record.

    `value` is the string form of what was read — Decimal, date,
    count — so the report shows the facts the outcome rests on,
    exactly as read, alongside where they came from.
    """

    input: str
    value: str
    source: str = ""


class PolicyTestOutcome(BaseModel):
    """The outcome of one test against one credit file.

    `outcome` is pass, fail, or not_evaluable. not_evaluable always
    names the missing input — a figure that was not computable, a
    global pack that was never combined, a document that is not on
    file. It is the honest third state law 2 demands: the test did
    not pass, it could not be run.
    """

    test_id: str
    name: str
    severity: str
    citation: str
    outcome: str  # pass | fail | not_evaluable
    operator: str | None = None
    threshold: Decimal | None = None
    fiscal_year: int | None = None
    values_read: list[PolicyInputValue] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    detail: str = ""


class PolicyReport(BaseModel):
    """The stored artefact of one policy evaluation run.

    Content-addressed like the computation pack it reads: the report
    id is derived from the pack version evaluated, the computation
    pack(s) read, the as-of date, and a digest of the credit-file
    facts the tests read. Re-evaluating the same file under the same
    pinned version over unchanged inputs reproduces the identical
    report. The report carries no timestamps of its own.
    """

    report_id: str
    credit_file_id: str
    policy_pack_id: str
    policy_version: str
    computation_pack_id: str
    global_pack_id: str | None = None
    as_of: date
    outcomes: list[PolicyTestOutcome] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)

    def outcome(self, test_id: str) -> PolicyTestOutcome | None:
        for outcome in self.outcomes:
            if outcome.test_id == test_id:
                return outcome
        return None

    def non_pass_outcomes(self) -> list[PolicyTestOutcome]:
        """Every outcome that is not a pass, in pack order.

        These are the memo's policy exceptions (round 5): a fail or
        a not_evaluable is carried into the memo exactly as the
        report records it — never omitted, never softened.
        """
        return [o for o in self.outcomes if o.outcome != "pass"]


class MemoTable(BaseModel):
    """One table in a memo section. Cells are rendered text; any
    figure in a cell is verified exactly like narrative text."""

    caption: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class MemoSection(BaseModel):
    """One section of the draft memo.

    `exception_test_ids` is used only by the policy_exceptions
    section: the structured list of policy test ids the section
    accounts for, so the verifier can check exceptions integrity
    structurally instead of trusting the prose.
    `disagreement_ids` is the round-8 parallel, used only by the
    document_disagreements section: the material disagreement ids
    the section accounts for, checked the same way.
    """

    key: str
    title: str
    paragraphs: list[str] = Field(default_factory=list)
    tables: list[MemoTable] = Field(default_factory=list)
    exception_test_ids: list[str] = Field(default_factory=list)
    disagreement_ids: list[str] = Field(default_factory=list)


class FigureTrace(BaseModel):
    """One figure occurrence in the rendered memo, traced home.

    `text` is the figure exactly as it appears in the rendered
    memo; `value` its canonical decimal string; `source` names the
    artefact and field it was traced to — a computation pack figure,
    a spread line, a policy threshold or outcome value, or a stated
    fact of the credit file.
    """

    text: str
    value: str
    kind: str  # amount | ratio | percent | number
    source: str
    section_key: str


class MemoVerification(BaseModel):
    """The verifier's record, stored with the memo it cleared.

    A memo only exists as a stored draft because this record exists:
    every figure in the rendered text traced, and the exceptions
    section accounting for every non-pass outcome of the pinned
    policy report, by test id.
    """

    composer_profile: str
    figure_count: int
    figures: list[FigureTrace] = Field(default_factory=list)
    exceptions_expected: list[str] = Field(default_factory=list)
    exceptions_listed: list[str] = Field(default_factory=list)
    # Round 8: the material disagreements the pinned spread records
    # (expected) and the ones the memo's disagreements section
    # accounts for (listed) — the same structural discipline the
    # exceptions above have carried since round 5.
    disagreements_expected: list[str] = Field(default_factory=list)
    disagreements_listed: list[str] = Field(default_factory=list)
    verified: bool = True


class MemoCommitteeFlag(BaseModel):
    """An item an officer marks on a memo under review (round 6).

    Two kinds: `committee_attention` — carried onto the
    officer-final memo so the committee sees it — and
    `conflict_of_interest`, by which an officer declares they
    cannot decide this memo; that officer's approval is then
    refused and the memo must be decided by a different officer.
    """

    flag_id: str
    kind: str  # committee_attention | conflict_of_interest
    section_key: str | None = None
    note: str = ""
    officer_id: str
    officer_role: str
    at: datetime = Field(default_factory=utcnow)


class CreditMemo(BaseModel):
    """The credit memo: a stored, content-addressed artefact.

    The header pins everything the memo was composed from — the
    spread, the computation pack, the policy pack and version and
    the report, all by id — so a reader can walk from any figure in
    the memo to the exact artefacts that produced it. A composed
    memo is born a DRAFT: it states facts and policy outcomes, and
    its recommendation and conditions sections are placeholders.

    At the credit officer gate (round 6) a draft is reviewed by a
    named officer. Approval mints a NEW artefact — the
    officer-final memo, status `officer_approved`, naming the
    draft it supersedes and the officer who approved it, carrying
    any committee flags — and a MemoDiff records what the officer
    changed. A draft is never edited in place; its live review
    status (draft / in review / returned / approved) is derived
    from the memo review events, not stored on the artefact.

    Content-addressed like the artefacts it pins: the memo id is
    derived from the pinned ids, the composer profile, the section
    content and the verification record, so identical inputs
    reproduce the identical memo.
    """

    memo_id: str
    credit_file_id: str
    status: str = "draft"
    borrower_name: str
    facility_type: str
    spread_id: str
    computation_pack_id: str
    global_pack_id: str | None = None
    policy_pack_id: str
    policy_version: str
    policy_report_id: str
    as_of: date
    composer_profile: str
    sections: list[MemoSection] = Field(default_factory=list)
    verification: MemoVerification
    created_at: datetime = Field(default_factory=utcnow)
    # Round 6 (officer gate) lineage. A composed draft supersedes
    # nothing; a recomposition after a return names the returned
    # draft it replaces, and an officer-final memo names the draft
    # it was approved from, the approving officer, and the flags
    # the committee must see.
    supersedes_memo_id: str | None = None
    reviewed_by: str | None = None
    committee_flags: list[MemoCommitteeFlag] = Field(default_factory=list)
    # Round 13 (risk rating): the header facts of the rating that
    # stood current when this memo was composed — the assignment
    # it came from, the scorecard and version that produced it,
    # the grade the scorecard assigned and, when a coded officer
    # override had moved it by the time of composition, the final
    # grade and the override's id. The rating is an input the
    # memo states in its header; it decides nothing, and a memo
    # composed before any rating exists carries none of this.
    rating_assignment_id: str | None = None
    rating_scorecard_id: str | None = None
    rating_scorecard_version: str | None = None
    rating_assigned_grade: int | None = None
    rating_assigned_band: str | None = None
    rating_grade: int | None = None
    rating_band: str | None = None
    rating_override_id: str | None = None

    def section(self, key: str) -> MemoSection | None:
        for section in self.sections:
            if section.key == key:
                return section
        return None


# ---------------------------------------------------------------------
# Round 9: committee readiness (design law 7). A credit file is
# committee-ready only when every required input is on file and
# fresh: the policy-cited checklist, the policy pack's statement-age
# and appraisal limits, and the computation pack's not-computable
# figures, consolidated into ONE content-addressed report. A
# readiness entry is a BLOCKER when its input is missing, stale past
# the limit its rule names, or not evaluable for want of a source
# input. Blockers clear only when the input itself refreshes —
# there is deliberately no waiver field anywhere in this section.
# ---------------------------------------------------------------------


class ReadinessEntry(BaseModel):
    """One required input and its state at the evaluation date."""

    input_key: str  # checklist item id, figure:<key>, or policy:<test id>
    label: str
    source: str  # checklist | computation_pack | policy_report
    # present_fresh | present_stale | missing | not_evaluable_input
    state: str
    blocker: bool
    detail: str
    # Freshness evidence, when an age rule applies.
    age_months: int | None = None
    limit_months: int | None = None
    rule_source: str | None = None
    # For not_evaluable_input: the source inputs the figure or test
    # is waiting on, named — never paraphrased downstream.
    missing_inputs: list[str] = Field(default_factory=list)
    related_test_ids: list[str] = Field(default_factory=list)
    document_id: str | None = None


class ReadinessReport(BaseModel):
    """The consolidated committee-readiness report for one file.

    Content-addressed like the other artefacts: the id derives from
    the file, the evaluation date, the pinned artefact ids, the
    policy + readiness-policy versions, and the entries themselves,
    so the same artefacts at the same date always produce the same
    report — and a report minted earlier can never bless a file
    whose inputs have since gone stale, because readiness is
    recomputed at the date of the action it gates.
    """

    report_id: str
    credit_file_id: str
    as_of: date
    policy_pack_id: str
    policy_version: str
    readiness_policy_id: str
    readiness_policy_version: str
    policy_report_id: str | None = None
    computation_pack_id: str | None = None
    global_pack_id: str | None = None
    entries: list[ReadinessEntry] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)

    @property
    def blockers(self) -> list[ReadinessEntry]:
        return [e for e in self.entries if e.blocker]

    @property
    def blocker_count(self) -> int:
        return len(self.blockers)

    @property
    def ready(self) -> bool:
        return not self.blockers


# ---------------------------------------------------------------------
# Round 6: the credit officer gate. A draft memo is reviewed by a
# named credit officer, who approves it (as-is, or with edits that
# pass the figure verifier again), returns it for rework with
# coded reasons, or flags items on it. Every action is a stored,
# append-only review event naming the actor; approval mints a new
# memo artefact plus a diff — the record of what the machine
# proposed and what the human changed (design law 8).
# ---------------------------------------------------------------------


class MemoReviewEvent(BaseModel):
    """One recorded action at the officer gate.

    Actions: submitted_for_review, approved_as_is,
    approved_with_edits, returned_for_rework, flagged, recomposed.
    The stream of these events for a memo is the authority for its
    review status — the artefact itself is never mutated. Returns
    carry coded reasons from the versioned vocabulary
    (officer_gate.RETURN_REASON_CODES); `flag` carries the full
    MemoCommitteeFlag when the action is `flagged`.

    Round 7 adds two committee actions to the same stream —
    committee_convened and committee_decided — so a memo's full
    status (officer gate, then committee) derives from one ordered
    event record. A committee_decided event carries its outcome in
    `committee_outcome`; the decision itself is a CommitteeDecision.
    """

    event_id: str = Field(default_factory=lambda: new_id("rev"))
    credit_file_id: str
    memo_id: str
    action: str
    actor_id: str
    actor_role: str
    resulting_memo_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    reason_vocabulary: str | None = None
    flag: MemoCommitteeFlag | None = None
    committee_outcome: str | None = None
    # Round 9: an approval event also carries the readiness report
    # computed over the memo's pinned artefacts at approval time, so
    # the committee block is visible on the record, never a surprise
    # discovered at convene.
    readiness_report_id: str | None = None
    readiness_blocker_count: int | None = None
    note: str = ""
    at: datetime = Field(default_factory=utcnow)


class SectionChange(BaseModel):
    """One section whose text differs between draft and final."""

    section_key: str
    paragraphs_before: list[str] = Field(default_factory=list)
    paragraphs_after: list[str] = Field(default_factory=list)
    tables_changed: bool = False


class FigureChange(BaseModel):
    """One figure occurrence that differs between draft and final.

    `old_trace` / `new_trace` name the artefact + field the
    figure traced to on that side — the figure's trace status.
    An added figure has no old side; a removed figure has no new
    side; an altered figure carries both.
    """

    section_key: str
    kind: str  # added | removed | altered
    old_text: str | None = None
    old_value: str | None = None
    old_trace: str | None = None
    new_text: str | None = None
    new_value: str | None = None
    new_trace: str | None = None


class MemoDiff(BaseModel):
    """The draft-vs-final record of an officer approval (law 8).

    Content-addressed like the memos it connects: the diff id
    derives from the two memo ids, the approving officer, the
    action and the changes themselves. An approval as-is produces
    a diff with no section or figure changes — the record exists
    either way, so 'the officer changed nothing' is itself
    recorded, not merely the absence of a record.
    """

    diff_id: str
    credit_file_id: str
    from_memo_id: str
    to_memo_id: str
    officer_id: str
    officer_role: str
    action: str  # approved_as_is | approved_with_edits
    section_changes: list[SectionChange] = Field(default_factory=list)
    figure_changes: list[FigureChange] = Field(default_factory=list)
    # Round 9: the readiness report pinned to this approval, with its
    # blocker count — the same stamp the approval event carries.
    readiness_report_id: str | None = None
    readiness_blocker_count: int | None = None
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------
# Round 7: the committee gate. The officer-final memo goes before a
# credit committee of named members, which approves, approves with
# structured conditions, declines, or defers. A decision exists only
# as a stored record naming its members and its quorum; a decline
# exists only with coded adverse-action reasons, each mechanically
# tied to the evidence (design law 3). Covenants are round 10 —
# conditions recorded here are structured records, not yet
# scheduled.
# ---------------------------------------------------------------------


class CommitteeMember(BaseModel):
    """One named member of the deciding committee, with their role.

    A committee decision is a record of named people: a member
    without an id or a role is nobody, and the gate records nobody.
    """

    member_id: str
    member_role: str


class CommitteeCondition(BaseModel):
    """One condition of a conditional approval, as a structured
    record (round 10 will schedule the financial ones as covenants).

    Types: financial_covenant (a metric, an operator and a
    threshold — the threshold must trace into the decided memo's
    pinned artefacts, and `threshold_trace` records where it
    traced to), reporting_requirement (what must be reported, on
    what cadence), documentation (a checklist item that must be
    satisfied), other (only with a note). `due_cadence` is data —
    how often or by when the condition bites — never a schedule.
    """

    condition_id: str
    type: str  # financial_covenant | reporting_requirement | documentation | other
    metric_key: str | None = None
    operator: str | None = None
    threshold: Decimal | None = None
    threshold_trace: str | None = None
    report_what: str | None = None
    checklist_item_id: str | None = None
    due_cadence: str = ""
    note: str = ""


class AdverseActionReason(BaseModel):
    """One coded reason for a decline (design law 3; ECOA/Reg B).

    The code comes from the versioned vocabulary
    (committee.ADVERSE_REASON_CODES). A code alone is an assertion;
    the reason stands only with its TIE — the mechanical link to
    the evidence the code describes, exactly one of:

      policy_test — a test of the memo's pinned policy report, by
                    test id, whose recorded outcome is fail or
                    not_evaluable as the code requires (a passed
                    test can never be cited);
      figure      — a figure occurrence in the decided memo's
                    verification record, by its text, value and
                    trace source;
      file_fact   — a required checklist item that is missing from
                    the credit file, by item id.

    There is no free-text reason: a decline whose reasons are not
    coded and tied is refused before it is recorded, so the
    borrower-facing notice can be generated from the codes and
    their ties alone — never hand-typed.
    """

    code: str
    tie_kind: str  # policy_test | figure | file_fact
    test_id: str | None = None
    figure_text: str | None = None
    figure_value: str | None = None
    figure_source: str | None = None
    checklist_item_id: str | None = None


class CommitteeDecision(BaseModel):
    """The committee's decision on one officer-final memo.

    Content-addressed like the artefacts it decides between: the
    decision id derives from the memo decided, the outcome, the
    members, the conditions, the reasons and the decision's place
    in the memo's decision sequence. A memo may carry more than
    one decision only across a deferral — a deferred decision is
    superseded by the decision taken when the memo is re-presented;
    an approve, conditional approve or decline is final.

    `outcome`: approve | approve_with_conditions | decline | defer.
    Every outcome names the members who decided and the quorum the
    decision required; the recorder — who entered the decision —
    is one of those members, recorded by name and role.
    """

    decision_id: str
    credit_file_id: str
    memo_id: str
    outcome: str  # approve | approve_with_conditions | decline | defer
    quorum_required: int
    members: list[CommitteeMember] = Field(default_factory=list)
    recorded_by: str
    recorded_by_role: str
    conditions: list[CommitteeCondition] = Field(default_factory=list)
    adverse_reasons: list[AdverseActionReason] = Field(default_factory=list)
    reason_vocabulary: str | None = None
    deferral_reasons: list[str] = Field(default_factory=list)
    deferral_vocabulary: str | None = None
    representment_requirements: list[str] = Field(default_factory=list)
    note: str = ""
    decided_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------
# Round 10: covenants — clocks + math (design law 6). An approval
# (plain or conditional) creates a CovenantSet for the credit file:
# the financial covenants the committee imposed as conditions
# (round 7 — thresholds already traced) plus the policy pack's
# standard covenant template for the facility type, as data. Every
# covenant carries a schedule computed in code from the facility's
# closing date: test dates over the facility's life, each test
# instance derived with its state and the inputs it needs.
#
# Integrity rule of this section: a covenant is NEVER edited in
# place. Its threshold, operator, frequency and grace live on a
# versioned record; changing any of them is an amendment — a NEW
# Covenant record (version + 1) that supersedes the old one, with
# a named author, a coded reason from the versioned vocabulary in
# covenants.py, and the committee decision that authorised it.
# The store enforces the same rule the model states: there is no
# update path for a covenant set, only append.
# ---------------------------------------------------------------------

# Covenant sources: where a covenant in a set came from.
COVENANT_SOURCE_CONDITION = "committee_condition"
COVENANT_SOURCE_TEMPLATE = "policy_template"

# Test frequencies, as data on the covenant.
FREQUENCY_QUARTERLY = "quarterly"
FREQUENCY_SEMIANNUAL = "semiannual"
FREQUENCY_ANNUAL = "annual"

# What a scheduled item is: a financial covenant test, or a dated
# reporting deliverable (a round 7 reporting condition).
ITEM_COVENANT_TEST = "covenant_test"
ITEM_REPORTING_DELIVERABLE = "reporting_deliverable"

# Derived schedule states (never stored on an artefact).
SCHEDULE_SCHEDULED = "scheduled"
SCHEDULE_DUE_SOON = "due_soon"
SCHEDULE_DUE = "due"
SCHEDULE_AWAITING_INPUTS = "awaiting_inputs"
SCHEDULE_OVERDUE = "overdue"


class Covenant(BaseModel):
    """One financial covenant of a covenant set: a metric, an
    operator, a threshold, a test schedule, and a grace period.

    `metric_key` must be a figure key of the computation engine's
    vocabulary — a covenant over a figure the engine cannot
    produce is refused at set creation, because it could never be
    tested. `basis` says how the tested period is chosen:
    `fiscal_period_end` tests the fiscal period ending on the
    test date; `point_in_time` tests the borrower's position at
    the test date itself (the latest spread period at that date).

    `origin` names where the covenant came from: a committee
    condition (by id) or a policy template entry (by id). The
    version lineage fields implement the amendment rule: version 1
    is born with the set; an amendment mints version n+1 naming
    the covenant it supersedes, its author, its coded reason, and
    the committee decision that authorised the change.
    """

    covenant_id: str
    set_id: str
    credit_file_id: str
    name: str = Field(min_length=1)
    metric_key: str = Field(min_length=1)
    operator: str = Field(pattern=r"^(>=|<=|>|<)$")
    threshold: Decimal
    threshold_trace: str | None = None
    frequency: str  # quarterly | semiannual | annual
    basis: str = "fiscal_period_end"  # fiscal_period_end | point_in_time
    grace_days: int = Field(default=0, ge=0)
    source: str  # committee_condition | policy_template
    origin_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    supersedes_covenant_id: str | None = None
    amended_by: str | None = None
    amendment_reason_code: str | None = None
    amendment_decision_id: str | None = None
    amendment_note: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ReportingDeliverable(BaseModel):
    """A dated reporting obligation on the same schedule as the
    covenant tests: a round 7 reporting_requirement condition
    (what must be reported, on the cadence it names), or the
    standard annual financial-statements deliverable the covenant
    template carries for every facility. Round 11 tracks receipt;
    this round schedules it and names what satisfies it."""

    deliverable_id: str
    set_id: str
    credit_file_id: str
    name: str = Field(min_length=1)
    report_what: str = Field(min_length=1)
    frequency: str  # quarterly | semiannual | annual
    source: str  # committee_condition | policy_template
    origin_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)


class CovenantSet(BaseModel):
    """The covenant set of one approved credit file.

    Content-addressed like the artefacts it derives from: the set
    id derives from the credit file, the decision that created it,
    the policy pack version it pins, the facility facts it reads
    (closing date, term, fiscal year end) and the covenant and
    deliverable records it holds at creation. Amendments do NOT
    rewrite the set: an amendment is its own Covenant record
    (version n+1) stored against the set, and the set's current
    covenants are derived — the latest version of each lineage.

    `state` is `active` when the closing date is on file and the
    schedule is fully computable; `awaiting_closing_date` when it
    is not — a named incomplete state, never a silently guessed
    anchor. The set pins the policy pack version and the decision
    id it came from, so a later policy refresh or a later decision
    never silently re-judges this facility's covenants (law 2's
    discipline, carried into law 6).
    """

    set_id: str
    credit_file_id: str
    decision_id: str
    memo_id: str
    policy_pack_id: str
    policy_version: str
    state: str  # active | awaiting_closing_date
    closing_date: date | None = None
    term_months: int = Field(gt=0)
    fiscal_year_end_month: int = Field(default=12, ge=1, le=12)
    fiscal_year_end_day: int = Field(default=31, ge=1, le=31)
    covenants: list[Covenant] = Field(default_factory=list)
    deliverables: list[ReportingDeliverable] = Field(default_factory=list)
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)


class CovenantAmendment(BaseModel):
    """The stored record of one covenant amendment: the new
    Covenant version it minted, and the version it superseded.

    Kept as its own artefact so the amendment history of a set
    reads without reconstructing it from the covenant records —
    and so the store has an append-only home for the act itself.
    """

    amendment_id: str
    credit_file_id: str
    set_id: str
    superseded_covenant_id: str
    covenant: Covenant
    author_id: str
    author_role: str
    reason_code: str
    decision_id: str
    note: str = ""
    at: datetime = Field(default_factory=utcnow)


class CovenantTestInstance(BaseModel):
    """One scheduled item, derived — never stored as an artefact.

    A test instance is a pure function of its covenant (or
    deliverable), the set's anchor facts, the credit file's
    current periods, and the as_of date: the same inputs always
    derive the same instance, id included. The scheduler tick does
    not create instances; it records the moment a derived instance
    is first SEEN standing at due or overdue, on the credit file's
    audit trail. Re-running the tick can therefore never duplicate
    an instance or an audit entry.

    `state` is one of the SCHEDULE_* states. An instance whose
    period's spread figures are not yet on file stands at
    `awaiting_inputs` and names them — the exact spread inputs the
    round 11 re-spread will need — from the moment its period ends
    until they arrive; its deadline state (due / overdue) resumes
    the moment they do.
    """

    instance_id: str
    credit_file_id: str
    set_id: str
    kind: str  # covenant_test | reporting_deliverable
    covenant_id: str | None = None
    deliverable_id: str | None = None
    name: str
    metric_key: str | None = None
    operator: str | None = None
    threshold: Decimal | None = None
    test_date: date
    period_label: str
    fiscal_year: int | None = None
    state: str
    days_until_due: int
    required_inputs: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------
# Round 11: covenant test results and breach cases. Round 10 derived
# the schedule; this round executes the tests on it. A test re-spreads
# the period through the existing spreading engine and recomputes the
# covenant's metric through the existing computation engine — the
# figure comes out of a stored computation pack, pinned by id — and
# the comparison against the covenant's threshold is recorded as an
# artefact. A failure opens a breach case: the bank's workflow object
# for the breach, with a cure clock and coded resolutions. Detection
# is the system's job; waiver and enforcement are the bank's, and
# they happen here only as recorded acts with named authority.
# ---------------------------------------------------------------------

# The honest outcomes of one covenant test. There is no fifth state:
# a test whose figure cannot be computed is not_evaluable and names
# what is missing; a test whose statements have not arrived stands
# at awaiting_inputs — blocked on the borrower, never silently
# passed over, and never read as a pass.
TEST_PASS = "pass"
TEST_FAIL = "fail"
TEST_NOT_EVALUABLE = "not_evaluable"
TEST_AWAITING_INPUTS = "awaiting_inputs"

# The derived standing of a recorded result against the file's
# CURRENT spread: current, or superseded by a restatement of the
# period it tested — the original record stands either way.
RESULT_CURRENT = "current"
RESULT_SUPERSEDED_BY_RESTATEMENT = "superseded_by_restatement"


class TestedPeriodSource(BaseModel):
    """One source document version a test result rests on: the
    period source the spread recorded for the tested fiscal year
    (per source type), pinned into the result so a later
    restatement of that period is detectable by comparison — the
    result names the exact document version it was computed from.
    """

    fiscal_year: int
    source_type: str
    document_id: str
    document_sha256: str
    version: int


class CovenantTestResult(BaseModel):
    """The recorded outcome of one covenant test instance.

    Content-addressed like the packs it derives from: the result
    id covers the instance, the covenant version tested, the
    spread and computation pack the figure came out of, and the
    outcome — so re-running the same test over the same inputs
    reproduces the same artefact and stores nothing twice. The
    figure is the engine's figure (from the pinned pack's own
    covenant result), never recomputed here: this record compares
    and reports, it does not calculate.

    A re-test — after a restatement, or after an awaiting_inputs
    result's statements arrive — is a new result naming the one
    it supersedes; the superseded result stays on record forever.
    """

    result_id: str
    credit_file_id: str
    set_id: str
    instance_id: str
    covenant_id: str
    covenant_version: int
    covenant_name: str
    metric_key: str
    operator: str
    threshold: Decimal
    test_date: date
    period_label: str
    fiscal_year: int | None = None
    spread_id: str | None = None
    computation_pack_id: str | None = None
    figure_value: Decimal | None = None
    headroom: Decimal | None = None
    outcome: str  # pass | fail | not_evaluable | awaiting_inputs
    missing_inputs: list[str] = Field(default_factory=list)
    period_sources: list[TestedPeriodSource] = Field(default_factory=list)
    supersedes_result_id: str | None = None
    is_retest: bool = False
    as_of: date


# Breach case states. A breach opens on a failing test and moves
# only by recorded acts: review, escalation, and the three
# resolutions — cured (a later passing re-test of the same
# covenant), waived (only on a committee decision's authority),
# or the default path. Cured, waived and defaulted are terminal.
BREACH_OPEN = "open"
BREACH_UNDER_REVIEW = "under_review"
BREACH_ESCALATED = "escalated"
BREACH_CURED = "cured"
BREACH_WAIVED = "waived"
BREACH_DEFAULTED = "defaulted_path"
BREACH_TERMINAL_STATES = frozenset({
    BREACH_CURED, BREACH_WAIVED, BREACH_DEFAULTED,
})


class BreachEvent(BaseModel):
    """One recorded act on a breach case: who did what, under
    which coded reason, naming the artefact that authorised it
    where one did (the passing re-test for a cure, the committee
    decision for a waiver). The case's history is its events;
    nothing about a breach is ever edited away."""

    action: str  # opened | under_review | escalated | cured | waived | defaulted
    actor_id: str
    actor_role: str = ""
    reason_code: str | None = None
    note: str = ""
    test_result_id: str | None = None
    decision_id: str | None = None
    at: datetime = Field(default_factory=utcnow)


class BreachCase(BaseModel):
    """A covenant breach, as a case: the bank's workflow object
    for one failing test.

    Opened automatically by the failing test result that raised
    it — one breach per failing result, the id derived from the
    result — and closed only by a coded resolution act (cure,
    waiver, default path). `cure_deadline` is the test date plus
    the covenant's cure (grace) period from the set: the clock
    the resolutions run against. Ageing is derived at read time,
    never stored."""

    breach_id: str
    credit_file_id: str
    borrower_name: str
    set_id: str
    covenant_id: str
    covenant_version: int
    covenant_name: str
    metric_key: str
    operator: str
    threshold: Decimal
    figure_value: Decimal | None = None
    test_result_id: str
    instance_id: str
    test_date: date
    period_label: str
    cure_deadline: date
    state: str = BREACH_OPEN
    events: list[BreachEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------
# Round 12: annual review packs. An approved facility owes the bank
# an annual review — a fresh look at the credit on fresh figures,
# taken by a named credit officer, recorded with a coded stance.
# The review instance (below, in reviews.py) is derived from the
# covenant set's anchor like the round 10 schedule; what is stored
# is the pack assembled for the review, the officer's outcome, and
# — when the stance escalates — the referral handed to the
# committee machinery. A review may also stand `completed`, the
# derived state of an instance an outcome has closed.
# ---------------------------------------------------------------------

REVIEW_COMPLETED = "completed"


class PolicyOutcomeDifference(BaseModel):
    """One policy test whose outcome differs between the version
    the credit was decided under and the version current at the
    review. The difference is surfaced, never reconciled: the
    review reads both reports and names the tests that moved."""

    test_id: str
    pinned_outcome: str
    current_outcome: str


class ReviewBlocker(BaseModel):
    """One stale or missing required input, carried into the pack
    from the round 9 readiness report. A pack with blockers is
    produced — the review still happens — but it is marked
    incomplete and names every blocker: absence is stated, never
    papered over (design law 7, carried into the review)."""

    input_key: str
    label: str
    state: str  # present_stale | missing | not_evaluable_input
    detail: str


class ReviewSummaryVerification(BaseModel):
    """The figure verifier's record over a review summary — the
    round 5 discipline applied to the review's prose: every figure
    in the rendered summary traced into the pack's pinned inputs,
    and every locked fact paragraph present verbatim."""

    composer_profile: str
    figure_count: int
    figures: list[FigureTrace] = Field(default_factory=list)
    locked_facts_expected: int = 0
    locked_facts_present: int = 0
    verified: bool = True


class ReviewSummary(BaseModel):
    """The officer-facing summary of an annual review pack.

    Two kinds of paragraph, kept distinct on the artefact:
    `locked_facts` are rendered by code from the pack's pinned
    artefacts — the covenant record, the breaches, the policy
    position under both pack versions, the material disagreements,
    the incompleteness statement — and may not be reworded by
    anyone, composer or officer; `paragraphs` are the composer's
    narrative, drafted through the model port and passed through
    the figure verifier before the pack exists. A summary whose
    prose softens a locked fact (a breach described away) is
    refused by the verifier, not edited into shape."""

    composer_profile: str
    paragraphs: list[str] = Field(default_factory=list)
    locked_facts: list[str] = Field(default_factory=list)
    verification: ReviewSummaryVerification


class AnnualReviewPack(BaseModel):
    """The pack assembled for one annual review instance.

    Content-addressed like the artefacts it pins: the pack id
    derives from the review instance, the as-of date, every pinned
    artefact id (spread, computation pack, both policy reports,
    the readiness report) and the verified summary — assembling
    the same review over the same inputs reproduces the identical
    pack. The pack is evidence for the review, assembled in code
    from stored artefacts only; the officer's stance is a separate
    artefact (AnnualReviewOutcome) that names the pack it read."""

    pack_id: str
    credit_file_id: str
    set_id: str
    review_number: int
    anchor_date: date
    period_start: date
    due_date: date
    as_of: date
    borrower_name: str
    memo_id: str
    spread_id: str
    computation_pack_id: str
    latest_period: int | None = None
    periods_covered: list[int] = Field(default_factory=list)
    pinned_policy_pack_id: str
    pinned_policy_version: str
    pinned_policy_report_id: str
    current_policy_version: str
    current_policy_report_id: str
    policy_differences: list[PolicyOutcomeDifference] = Field(
        default_factory=list
    )
    readiness_report_id: str
    complete: bool
    blockers: list[ReviewBlocker] = Field(default_factory=list)
    test_result_ids: list[str] = Field(default_factory=list)
    breach_ids: list[str] = Field(default_factory=list)
    open_breach_ids: list[str] = Field(default_factory=list)
    material_disagreement_ids: list[str] = Field(default_factory=list)
    # Round 13: the risk rating that stood current at assembly —
    # the assignment, its scorecard, the assigned grade and the
    # final grade after any override, and the full factor
    # breakdown. An input the review reads; never a stance it
    # takes. A pack assembled before any rating exists carries
    # none of this.
    rating_assignment_id: str | None = None
    rating_scorecard_id: str | None = None
    rating_scorecard_version: str | None = None
    rating_assigned_grade: int | None = None
    rating_assigned_band: str | None = None
    rating_grade: int | None = None
    rating_band: str | None = None
    rating_override_id: str | None = None
    rating_factors: list["RatingFactorScore"] = Field(default_factory=list)
    summary: ReviewSummary
    created_at: datetime = Field(default_factory=utcnow)


class AnnualReviewOutcome(BaseModel):
    """A named credit officer's recorded stance on one annual
    review — the act that closes the review instance.

    The stance is one of a versioned coded vocabulary (reviews.py):
    affirm, affirm_with_conditions (the conditions are structured
    CommitteeCondition records, validated and traced by the round
    7 machinery), place_on_watchlist, escalate_to_committee (which
    mints the CommitteeReferral the outcome names). Reasons are
    coded from the same vocabulary — a free-text stance is
    impossible by construction. The reviewer is a named officer in
    a credit-officer role; when the reviewer is the officer who
    approved the decided memo at the round 6 gate, a second named
    officer must countersign — the memo's approver does not review
    their own credit alone."""

    outcome_id: str
    credit_file_id: str
    set_id: str
    review_number: int
    due_date: date
    pack_id: str
    stance: str  # affirm | affirm_with_conditions | place_on_watchlist | escalate_to_committee
    reason_codes: list[str] = Field(default_factory=list)
    reason_vocabulary: str
    reviewer_id: str
    reviewer_role: str
    countersigner_id: str | None = None
    countersigner_role: str | None = None
    conditions: list[CommitteeCondition] = Field(default_factory=list)
    referral_id: str | None = None
    note: str = ""
    decided_at: datetime = Field(default_factory=utcnow)


class CommitteeReferral(BaseModel):
    """The recorded hand-off an escalating review outcome creates:
    the pack, the review and the coded reasons, addressed to the
    credit committee.

    The referral is a surface, not a second committee: the round
    7 machinery (convene, decide) remains the only place a
    committee decision is made, and it acts on the memo this
    referral names. The referral records that the review asked —
    with the evidence pack pinned — and stands `open` until the
    bank's own process answers it; this blueprint records no
    committee outcome on a referral's behalf."""

    referral_id: str
    credit_file_id: str
    set_id: str
    review_number: int
    outcome_id: str
    pack_id: str
    memo_id: str
    reason_codes: list[str] = Field(default_factory=list)
    reason_vocabulary: str
    referred_by: str
    referred_by_role: str
    note: str = ""
    status: str = "open"
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------
# Round 13: the risk-rating scorecard. The bank's obligor rating
# in a real deployment comes out of the bank's own rating model,
# governed as a model (SR 11-7-style: inventory, validation,
# ongoing monitoring). This blueprint ships a CODED STAND-IN for
# that model — a versioned scorecard definition held as data,
# whose factors read only figures the computation engine produced
# and coded facts of the file, so every grade it assigns can be
# walked back factor by factor to a computation step or a named
# record. The stand-in's honesty rule is the same as the rest of
# the system's: it is an INPUT. A rating is carried into the memo
# header and the annual review pack; no gate reads it, no
# decision path consults it, and nothing in this system approves
# or declines on its authority.
# ---------------------------------------------------------------------


class ScorecardBand(BaseModel):
    """One band of one scorecard factor: the inputs that land in
    it, and the factor score it earns (1 strongest, 10 weakest —
    the same direction as the grade scale).

    A numeric band matches a figure value inside [at_least,
    at_most] (either bound may be open). A categorical band
    matches the category string a file-fact factor derives. A
    band flagged `is_missing_data` is the factor's named answer
    to an input that could not be computed: the assignment
    records the band and the missing inputs — the value is never
    zero-filled or quietly dropped from the factor set."""

    band_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    score: int = Field(ge=1, le=10)
    at_least: Decimal | None = None
    at_most: Decimal | None = None
    category: str | None = None
    is_missing_data: bool = False


class ScorecardFactorDef(BaseModel):
    """One factor of the scorecard, as definition data.

    `kind` says where the factor's input lives:
      figure    — a ComputedFigure of the borrower's computation
                  pack, read at the pack's latest period (or, for
                  the trend factor, the change across its periods);
                  `figure_key` names it;
      file_fact — a coded fact of the credit file itself, derived
                  in code from stored records (covenant test
                  results, breach cases, the spread's period
                  sources and disagreements); `fact` names it.

    The bands are listed strongest first; the first band whose
    bounds (or category) match the input is the band hit. The
    factor set of a scorecard version is fixed: every assignment
    under that version scores every factor, with a missing input
    landing in the factor's named missing-data band."""

    factor_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = Field(pattern=r"^(figure|file_fact)$")
    figure_key: str | None = None
    # latest — the figure at the pack's latest period;
    # period_change — the figure at the latest period less the
    # figure at the earliest (the trend factor's input).
    figure_mode: str = "latest"  # latest | period_change
    fact: str | None = None  # covenant_compliance | breach_history | statement_quality
    weight: Decimal = Field(gt=Decimal("0"))
    bands: list[ScorecardBand] = Field(min_length=1)


class ScorecardGradeBand(BaseModel):
    """One named stretch of the 1–10 grade scale."""

    grade_from: int = Field(ge=1, le=10)
    grade_to: int = Field(ge=1, le=10)
    label: str = Field(min_length=1)


class ScorecardDefinition(BaseModel):
    """One version of the coded scorecard: an identified, dated
    set of factors and the grade scale their weighted composite
    maps onto. Immutable once shipped — a change to a factor, a
    band or a weight is a new version, and assignments made under
    the earlier version keep it (history is never re-based)."""

    scorecard_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    note: str = Field(min_length=1)
    factors: list[ScorecardFactorDef] = Field(min_length=1)
    grade_bands: list[ScorecardGradeBand] = Field(min_length=1)


class RatingFactorScore(BaseModel):
    """One factor's scored contribution to one rating assignment.

    The trace is the point: `input_value` (or `input_category`
    for a file-fact factor) is what the factor read; `trace`
    names the artefact and field it came from — for a figure
    factor, the computation pack, the figure key and period, and
    `derivation_steps` carries the step keys of that figure's
    recorded derivation in the pack, so the walk from grade to
    arithmetic is mechanical. A factor whose input could not be
    computed carries no value: it names the missing inputs and
    lands in its missing-data band."""

    factor_id: str
    factor_name: str
    kind: str  # figure | file_fact
    weight: Decimal
    input_value: Decimal | None = None
    input_category: str | None = None
    figure_key: str | None = None
    fiscal_year: int | None = None
    trace: str = ""
    derivation_steps: list[str] = Field(default_factory=list)
    band_id: str
    band_label: str
    score: int = Field(ge=1, le=10)
    weighted_contribution: Decimal
    missing_inputs: list[str] = Field(default_factory=list)


class RatingAssignment(BaseModel):
    """One assignment of a risk rating to one credit file.

    Content-addressed like the artefacts it reads: the assignment
    id derives from the scorecard id + version, the computation
    pack and spread it read, the as-of date (breach recency is
    read at a date, so the date is part of the artefact), and
    every factor's scored result — recomputing over the same
    inputs at the same date reproduces the identical assignment.
    The composite is the weighted sum of the factor scores,
    recorded to two decimals; the grade is the composite rounded
    half-up to the 1–10 scale; the band is the grade's name in
    the scorecard's own scale. An assignment is never edited: an
    officer who disagrees records a RatingOverride beside it,
    and both grades stay on the record."""

    assignment_id: str
    credit_file_id: str
    borrower_name: str
    scorecard_id: str
    scorecard_version: str
    as_of: date
    spread_id: str
    computation_pack_id: str
    latest_period: int | None = None
    factors: list[RatingFactorScore] = Field(default_factory=list)
    composite_score: Decimal
    assigned_grade: int = Field(ge=1, le=10)
    assigned_band: str
    created_at: datetime = Field(default_factory=utcnow)


class RatingOverride(BaseModel):
    """A named credit officer's coded move of a rating's final
    grade — the only way a grade changes after assignment.

    Bounded by construction: the new grade stands at most one
    grade from the ASSIGNED grade, the reason is a code from the
    versioned vocabulary (scorecard.OVERRIDE_REASON_CODES), and a
    second officer — a different person, in an officer role —
    countersigns. The override is its own artefact beside the
    assignment, never an edit to it: the assignment keeps the
    grade the scorecard gave, the override chain records each
    move and the grade it moved from, and every surface that
    shows the rating shows both grades."""

    override_id: str
    assignment_id: str
    credit_file_id: str
    assigned_grade: int = Field(ge=1, le=10)
    previous_grade: int = Field(ge=1, le=10)
    new_grade: int = Field(ge=1, le=10)
    reason_code: str
    reason_vocabulary: str
    officer_id: str
    officer_role: str
    countersigner_id: str
    countersigner_role: str
    supersedes_override_id: str | None = None
    note: str = ""
    at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------
# Round 15: the model gateway — profiles, prices, and the cost
# ledger. Profiles are governed configuration data, never code
# branches; every call through the gateway is priced from the
# versioned price table and appended to the ledger, offline calls
# at zero included, so the ledger is the complete record of what
# drafting and extraction cost, file by file.
# ---------------------------------------------------------------------


class ProviderKind(str, Enum):
    """How a model profile's endpoint speaks.

    ``offline`` is the deterministic composer — no model call at
    all. ``anthropic`` is the hosted-frontier shape (Messages
    API). ``openai_compatible`` is the shape an in-boundary
    open-weight server (vLLM, NIM, a private gateway) exposes.
    The kind decides the adapter; the profile decides everything
    else, as data.
    """

    OFFLINE = "offline"
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai-compatible"


class ModelProfileDefinition(BaseModel):
    """One model profile, as governed configuration data.

    A deployment adds a profile by adding a definition (and a
    price entry), never by patching the gateway. Endpoint and key
    are named as *environment variables*, never as values — the
    definition is safe to commit; the secret never is.
    ``endpoint_default`` is the public default base URL for a
    hosted provider (a URL is not a secret); an in-boundary
    endpoint has no default, so a deployment that has not set one
    is unconfigured by construction.
    """

    profile_id: str = Field(min_length=1)
    provider_kind: ProviderKind
    model: str = Field(min_length=1)
    endpoint_env: str | None = Field(
        default=None,
        description="Environment variable naming the endpoint "
        "base URL. Required for live profiles.",
    )
    endpoint_default: str | None = Field(
        default=None,
        description="Fallback base URL when the env var is unset "
        "(hosted providers' public endpoints only).",
    )
    api_key_env: str | None = Field(
        default=None,
        description="Environment variable naming the API key. "
        "The value is read at wiring time and never stored on "
        "the definition, a ledger entry, or a log line.",
    )
    key_required: bool = Field(
        default=True,
        description="True when the endpoint refuses keyless "
        "calls (hosted providers). An in-boundary server inside "
        "the bank's own network may set this False — the "
        "endpoint itself stays required.",
    )
    timeout_seconds: float = Field(default=60.0, gt=0)


class ModelPrice(BaseModel):
    """One model's prices in the governed price table.

    Prices are reference data with a basis, maintained by the
    deployment — the table is versioned as a whole, and every
    ledger entry pins the table version it was priced under, so
    a later price change never rewrites what a past call cost.
    """

    model: str = Field(min_length=1)
    input_per_mtok_usd: Decimal = Field(
        description="USD per 1M input tokens, as an exact Decimal."
    )
    output_per_mtok_usd: Decimal = Field(
        description="USD per 1M output tokens, as an exact Decimal."
    )
    basis: str = Field(
        default="",
        description="Where the price comes from (provider list "
        "price, internal serving-cost allocation, zero for the "
        "offline composer) — stated, never implied.",
    )


class PriceTable(BaseModel):
    """The versioned price table: every model a profile may name,
    priced. A profile whose model has no entry here cannot be
    loaded — an unpriceable call is a configuration error, refused
    at load time, not discovered on an invoice."""

    version: str = Field(min_length=1)
    as_of: date
    entries: list[ModelPrice] = Field(min_length=1)


class ModelCallEntry(BaseModel):
    """One model call, priced, in the cost ledger (round 15).

    EVERY call through the gateway appends one of these —
    including offline calls at zero — so the ledger is the
    complete record of what the drafting cost, file by file. The
    entry carries the unit prices it was priced with (not just
    the table version), so the cost is re-checkable from the
    entry alone: ``cost = (tokens_in * input_price + tokens_out
    * output_price) / 1_000_000``, exact Decimal arithmetic. A
    call whose output a verifier later refuses is ledgered all
    the same — it cost what it cost.

    ``token_basis`` states where the token counts came from:
    ``reported`` (the provider's usage block) or the adapter's
    stated estimate basis when a provider reported none.
    ``called_at`` comes from the gateway's injected clock, so
    tests and evals price a call at a known instant.
    """

    entry_id: str = Field(default_factory=lambda: new_id("led"))
    credit_file_id: str | None = Field(
        default=None,
        description="The credit file the call served; None is "
        "the system scope (a call not made for any one file).",
    )
    purpose: str = Field(
        description="memo_draft | review_summary | "
        "extraction_assist | … — what the call was for.",
    )
    profile_id: str
    model: str
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    token_basis: str = Field(
        description="reported | estimated:chars/4 — the stated "
        "basis of the token counts.",
    )
    price_table_version: str
    input_per_mtok_usd: Decimal
    output_per_mtok_usd: Decimal
    cost_usd: Decimal = Field(
        description="The call's priced cost, exact Decimal USD."
    )
    latency_ms: int = Field(ge=0)
    called_at: datetime = Field(default_factory=utcnow)


class ModelBudgetEvent(BaseModel):
    """A budget refusal, recorded (round 15).

    When a file's priced spend reaches the deployment's per-file
    cap, further live-profile calls for that file are refused
    before any transport is touched. The refusal is a record,
    not a log line: this artefact in the ledger store, and an
    audit event on the file's own trail.
    """

    event_id: str = Field(default_factory=lambda: new_id("bud"))
    credit_file_id: str
    profile_id: str
    model: str
    purpose: str
    cap_usd: Decimal = Field(
        description="The per-file cap in force, exact Decimal USD."
    )
    spent_usd: Decimal = Field(
        description="The file's priced spend at refusal time."
    )
    recorded_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------
# Round 17: quarantine + access control. Borrower documents are
# untrusted text: an instruction planted in a statement's notes is
# recorded, fenced at every model surface, and never obeyed — and
# retrieval of the file's artefacts is governed by one role policy,
# with every refusal recorded.
# ---------------------------------------------------------------------


class QuarantineEvent(BaseModel):
    """One instruction-pattern match in one document field.

    Recorded at ingestion by the deterministic scanner
    (quarantine.py) under a versioned pattern pack. The document
    itself is never rejected and never altered — the event is the
    record that a span of its text matched an instruction pattern:
    the document, the field the text lives in, the pattern, and
    the exact span. Everywhere that text later reaches a model,
    the span is fenced; a figure that appears only inside such
    spans grounds nothing in a memo. `figures` carries the
    money-like numeric tokens found inside the matched span, so
    the figure discipline never has to re-derive them.
    """

    event_id: str = Field(default_factory=lambda: new_id("qev"))
    credit_file_id: str
    document_id: str
    field: str  # JSON path of the string field, or "filename" / "text"
    pattern_id: str
    pattern_family: str
    pack_version: str
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)
    matched_text: str
    figures: list[Decimal] = Field(default_factory=list)
    recorded_at: datetime = Field(default_factory=utcnow)


class QuarantineReview(BaseModel):
    """A named officer's review of one quarantine event.

    The review is an audit act, recorded; it changes nothing
    about fencing. Fences derive from the stored scan, not from
    anyone's judgement that a span looks harmless — a reviewed
    span stays fenced, and its figures stay ungrounded.
    """

    review_id: str = Field(default_factory=lambda: new_id("qrv"))
    event_id: str
    credit_file_id: str
    reviewer: str
    note: str = ""
    reviewed_at: datetime = Field(default_factory=utcnow)


class AccessDenial(BaseModel):
    """One refused access, recorded (round 17).

    Every refusal the access policy produces — a hidden file
    answered 404, a family answered 403, a read-only role's
    write refused before gate logic — lands here as well as on
    the file's own audit trail (when the file exists). The
    denial list is how an examiner reconciles what was asked
    for against what was refused; it is append-only.
    """

    denial_id: str = Field(default_factory=lambda: new_id("den"))
    credit_file_id: str | None = None
    caller: str
    role: str
    family: str  # artefact family, or "write" for a refused write
    reason_code: str
    surface: str  # the route or surface that produced the refusal
    at: datetime = Field(default_factory=utcnow)
