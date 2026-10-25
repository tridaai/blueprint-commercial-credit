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
    AuditEvent,
    Borrower,
    ComputationPack,
    CreditFile,
    CreditMemo,
    ExtractedValue,
    FacilityRequest,
    MemoDiff,
    MemoReviewEvent,
    PolicyReport,
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

    def ping(self) -> bool: ...
