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
    CreditFile,
    ExtractedValue,
    FacilityRequest,
    SourceDocument,
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

    def ping(self) -> bool: ...
