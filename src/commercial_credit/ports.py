"""Ports: the interfaces the credit pipeline depends on.

Everything external sits behind one of these protocols — the loan
origination system (LOS) the bank actually runs, the object store
that holds borrower documents, the extraction engine, and the credit
file store itself. Round 1 ships reference implementations and test
doubles for each; a deployment swaps the implementation, never the
contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    AnnualReviewOutcome,
    AnnualReviewPack,
    AuditEvent,
    Borrower,
    BreachCase,
    CommitteeDecision,
    CommitteeReferral,
    ComputationPack,
    CovenantAmendment,
    CovenantSet,
    CovenantTestResult,
    CreditFile,
    CreditMemo,
    ExtractedValue,
    FacilityRequest,
    MemoDiff,
    MemoReviewEvent,
    ModelBudgetEvent,
    ModelCallEntry,
    PolicyReport,
    RatingAssignment,
    RatingOverride,
    ReadinessReport,
    SourceDocument,
    Spread,
    SpreadAdjustment,
)


@runtime_checkable
class LosFeed(Protocol):
    """Inbound applications from the bank's loan origination system."""

    def fetch_application(self, application_ref: str) -> tuple[Borrower, FacilityRequest]:
        """Return the borrower + facility request for an LOS reference."""
        ...


@runtime_checkable
class DocumentStore(Protocol):
    """S3-compatible object storage for source document bytes."""

    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


@runtime_checkable
class Extractor(Protocol):
    """Reads figures out of a stored borrower document.

    CONTRACT: every returned ExtractedValue carries complete
    provenance (document id, page, line or cell). An implementation
    that cannot point at where a value came from must not return it;
    returning a value without provenance is a contract breach and the
    receiving service rejects the whole batch (extraction.py).
    """

    def extract(self, document: SourceDocument, content: bytes) -> list[ExtractedValue]:
        ...


@runtime_checkable
class CreditFileStore(Protocol):
    """The system of record for credit files, extractions and audit."""

    def save_credit_file(self, credit_file: CreditFile) -> None: ...

    def get_credit_file(self, credit_file_id: str) -> CreditFile | None: ...

    def list_credit_files(self) -> list[CreditFile]: ...

    def save_extractions(
        self, credit_file_id: str, values: list[ExtractedValue]
    ) -> None: ...

    def get_extractions(self, credit_file_id: str) -> list[ExtractedValue]: ...

    def append_audit(self, event: AuditEvent) -> None: ...

    def list_audit(self, credit_file_id: str) -> list[AuditEvent]: ...

    def save_adjustment(self, adjustment: SpreadAdjustment) -> None: ...

    def list_adjustments(self, credit_file_id: str) -> list[SpreadAdjustment]: ...

    def save_spread(self, spread: Spread) -> None: ...

    def get_latest_spread(self, credit_file_id: str) -> Spread | None: ...

    def list_spreads(self, credit_file_id: str) -> list[Spread]: ...

    def save_computation_pack(self, pack: ComputationPack) -> None: ...

    def get_computation_pack(self, pack_id: str) -> ComputationPack | None: ...

    def list_computation_packs(
        self, credit_file_id: str
    ) -> list[ComputationPack]: ...

    def save_policy_report(self, report: PolicyReport) -> None: ...

    def get_policy_report(self, report_id: str) -> PolicyReport | None: ...

    def list_policy_reports(
        self, credit_file_id: str
    ) -> list[PolicyReport]: ...

    def save_readiness_report(self, report: ReadinessReport) -> None: ...

    def get_readiness_report(
        self, report_id: str
    ) -> ReadinessReport | None: ...

    def list_readiness_reports(
        self, credit_file_id: str
    ) -> list[ReadinessReport]: ...

    def save_memo(self, memo: CreditMemo) -> None: ...

    def get_memo(self, memo_id: str) -> CreditMemo | None: ...

    def list_memos(self, credit_file_id: str) -> list[CreditMemo]: ...

    def save_memo_review_event(self, event: MemoReviewEvent) -> None: ...

    def list_memo_review_events(
        self, credit_file_id: str, memo_id: str | None = None
    ) -> list[MemoReviewEvent]: ...

    def save_memo_diff(self, diff: MemoDiff) -> None: ...

    def get_memo_diff(self, diff_id: str) -> MemoDiff | None: ...

    def get_memo_diff_by_final(self, memo_id: str) -> MemoDiff | None: ...

    def list_memo_diffs(self, credit_file_id: str) -> list[MemoDiff]: ...

    def save_committee_decision(self, decision: CommitteeDecision) -> None: ...

    def get_committee_decision(
        self, decision_id: str
    ) -> CommitteeDecision | None: ...

    def list_committee_decisions(
        self, credit_file_id: str, memo_id: str | None = None
    ) -> list[CommitteeDecision]: ...

    # -- Covenant sets and amendments (round 10) --------------------
    # A set is created once, by one decision, and never updated:
    # save is insert-once. Amendments are append-only artefacts
    # beside the set. There is deliberately no update path for
    # either: a covenant's terms change by supersession, in the
    # open, or not at all.

    def save_covenant_set(self, covenant_set: CovenantSet) -> None: ...

    def get_covenant_set(self, set_id: str) -> CovenantSet | None: ...

    def list_covenant_sets(
        self, credit_file_id: str
    ) -> list[CovenantSet]: ...

    def save_covenant_amendment(
        self, amendment: CovenantAmendment
    ) -> None: ...

    def list_covenant_amendments(
        self, credit_file_id: str
    ) -> list[CovenantAmendment]: ...

    # -- Covenant test results and breach cases (round 11) --------
    # A test result is content-addressed and insert-once, like the
    # packs it pins: re-running the same test over the same inputs
    # stores nothing twice. A breach case is a living record — it
    # is upserted as its recorded acts accumulate, the way the
    # credit file itself is; its event history inside the payload
    # is append-only, and there is no delete path for either.

    def save_covenant_test_result(
        self, result: CovenantTestResult
    ) -> None: ...

    def get_covenant_test_result(
        self, result_id: str
    ) -> CovenantTestResult | None: ...

    def list_covenant_test_results(
        self, credit_file_id: str
    ) -> list[CovenantTestResult]: ...

    def save_review_pack(self, pack: AnnualReviewPack) -> None: ...

    def get_review_pack(
        self, pack_id: str
    ) -> AnnualReviewPack | None: ...

    def list_review_packs(
        self, credit_file_id: str
    ) -> list[AnnualReviewPack]: ...

    def save_review_outcome(self, outcome: AnnualReviewOutcome) -> None: ...

    def get_review_outcome(
        self, outcome_id: str
    ) -> AnnualReviewOutcome | None: ...

    def list_review_outcomes(
        self, credit_file_id: str
    ) -> list[AnnualReviewOutcome]: ...

    def save_committee_referral(
        self, referral: CommitteeReferral
    ) -> None: ...

    def get_committee_referral(
        self, referral_id: str
    ) -> CommitteeReferral | None: ...

    def list_committee_referrals(
        self, credit_file_id: str
    ) -> list[CommitteeReferral]: ...

    # -- Risk ratings (round 13) -----------------------------------
    # An assignment is content-addressed and insert-once, like
    # the packs it reads: recomputing over the same inputs stores
    # nothing twice, and a scorecard version change appends a new
    # assignment — history is never rewritten. An override is
    # likewise insert-once beside its assignment; there is no
    # update path for either, because a grade changes by a new
    # recorded act or not at all.

    def save_rating_assignment(
        self, assignment: RatingAssignment
    ) -> None: ...

    def get_rating_assignment(
        self, assignment_id: str
    ) -> RatingAssignment | None: ...

    def list_rating_assignments(
        self, credit_file_id: str
    ) -> list[RatingAssignment]: ...

    def save_rating_override(
        self, override: RatingOverride
    ) -> None: ...

    def get_rating_override(
        self, override_id: str
    ) -> RatingOverride | None: ...

    def list_rating_overrides(
        self, credit_file_id: str
    ) -> list[RatingOverride]: ...

    def save_breach_case(self, breach: BreachCase) -> None: ...

    def get_breach_case(self, breach_id: str) -> BreachCase | None: ...

    def list_breach_cases(
        self, credit_file_id: str | None = None
    ) -> list[BreachCase]: ...

    # -- Model cost ledger (round 15) ------------------------------
    # The gateway's append-only record of model calls and budget
    # refusals. Entries are never updated or deleted: a later
    # price-table version prices future calls, it never reprices
    # recorded ones. `list_call_entries(None)` reads every entry
    # (the portfolio rollup); a file id reads that file's entries.

    def add_call_entry(self, entry: ModelCallEntry) -> None: ...

    def list_call_entries(
        self, credit_file_id: str | None
    ) -> list[ModelCallEntry]: ...

    def add_budget_event(self, event: ModelBudgetEvent) -> None: ...

    def list_budget_events(
        self, credit_file_id: str
    ) -> list[ModelBudgetEvent]: ...

    def ping(self) -> bool: ...


@runtime_checkable
class ModelLedgerStore(Protocol):
    """The gateway's ledger: where priced model calls and budget
    refusals are recorded.

    A separate, narrower port than the credit file store — the
    gateway depends on this and nothing wider. In the shipped
    wiring the credit file store implements both; the gateway
    would make unrecorded calls without it, so it is required at
    construction, never optional.
    """

    def add_call_entry(self, entry: ModelCallEntry) -> None: ...

    def list_call_entries(
        self, credit_file_id: str | None
    ) -> list[ModelCallEntry]: ...

    def add_budget_event(self, event: ModelBudgetEvent) -> None: ...

    def list_budget_events(
        self, credit_file_id: str
    ) -> list[ModelBudgetEvent]: ...
