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
    """Which document version one spread period rests on.

    When a borrower files a corrected statement for a period, the
    new version supersedes the earlier extraction for that period;
    the spread records the version it used and names the superseded
    documents, whose extracted values remain in the store.
    """

    fiscal_year: int
    period_label: str
    document_id: str
    document_sha256: str
    version: int = Field(ge=1)
    superseded_document_ids: list[str] = Field(default_factory=list)


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
    """

    key: str
    title: str
    paragraphs: list[str] = Field(default_factory=list)
    tables: list[MemoTable] = Field(default_factory=list)
    exception_test_ids: list[str] = Field(default_factory=list)


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

    def section(self, key: str) -> MemoSection | None:
        for section in self.sections:
            if section.key == key:
                return section
        return None


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
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)
