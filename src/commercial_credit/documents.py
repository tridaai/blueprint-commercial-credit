"""Document intake: bytes in, hash recorded, bytes verified on use.

A borrower document enters the credit file exactly once, through
`register_document`: the bytes go to the document store, their
SHA-256 is recorded on the credit file, and the document is matched
against the checklist. Every later read goes through
`read_document_verified`, which re-hashes what came back and refuses
bytes that no longer match the intake record — a swapped or corrupted
document fails loudly here instead of quietly changing a spread.
"""

from __future__ import annotations

import hashlib

from .checklist import match_checklist_item
from .errors import DocumentIntegrityError, NotFoundError
from .models import (
    AuditEvent,
    CreditFile,
    DocumentKind,
    SourceDocument,
    new_id,
)
from .ports import CreditFileStore, DocumentStore
from .quarantine import build_events, scan_document


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalDocumentStore:
    """In-filesystem document store (tests + single-node runs).

    Implements the same DocumentStore port as the S3 store; the hash
    discipline lives in this module, not in either store, so swapping
    stores cannot weaken it.
    """

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._objects[key] = bytes(data)

    def get(self, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError as exc:
            raise NotFoundError(f"no stored object at {key}") from exc

    def exists(self, key: str) -> bool:
        return key in self._objects


class S3DocumentStore:
    """S3-compatible document store via boto3 (AWS S3, SeaweedFS, …)."""

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        import boto3  # local import: doubles/tests never need boto3 wired

        self._bucket = bucket
        self._client = boto3.client(
            "s3", endpoint_url=endpoint_url, region_name=region
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=key)
        return resp["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False


def register_document(
    store: CreditFileStore,
    documents: DocumentStore,
    credit_file: CreditFile,
    *,
    kind: DocumentKind,
    filename: str,
    data: bytes,
    period_label: str | None = None,
    content_type: str = "application/json",
) -> SourceDocument:
    """Store bytes, record their hash, satisfy the checklist."""
    document_id = new_id("doc")
    storage_key = f"{credit_file.credit_file_id}/{document_id}/{filename}"
    documents.put(storage_key, data, content_type)
    doc = SourceDocument(
        document_id=document_id,
        credit_file_id=credit_file.credit_file_id,
        kind=kind,
        filename=filename,
        content_type=content_type,
        sha256=sha256_hex(data),
        byte_size=len(data),
        storage_key=storage_key,
        period_label=period_label,
    )
    credit_file.documents.append(doc)
    item = match_checklist_item(credit_file.checklist, kind)
    if item is not None:
        item.satisfied_by_document_id = document_id
    remaining = credit_file.missing_required()
    from .models import CreditFileStatus

    credit_file.status = (
        CreditFileStatus.READY_FOR_SPREADING
        if not remaining
        else CreditFileStatus.COLLECTING_DOCUMENTS
    )
    store.save_credit_file(credit_file)
    # Round 17: the document's text is untrusted. The scanner
    # runs over it here, at ingestion — every match is recorded
    # as a quarantine event on the file and an audit entry names
    # the count. The document is never rejected and never
    # altered: a planted instruction becomes part of the file's
    # record, fenced wherever the text later reaches a model.
    events = build_events(
        scan_document(data, filename=filename),
        credit_file_id=credit_file.credit_file_id,
        document_id=document_id,
    )
    for event in events:
        store.save_quarantine_event(event)
    if events:
        store.append_audit(
            AuditEvent(
                credit_file_id=credit_file.credit_file_id,
                actor="document-intake-scan",
                action="document_quarantined",
                detail=(
                    f"{len(events)} instruction-pattern match(es) in "
                    f"{filename}: "
                    + ", ".join(
                        f"{e.pattern_id} at {e.field}"
                        for e in events
                    )
                ),
            )
        )
    return doc


def read_document_verified(
    documents: DocumentStore, doc: SourceDocument
) -> bytes:
    """Read a document back and re-verify its SHA-256 against intake."""
    data = documents.get(doc.storage_key)
    actual = sha256_hex(data)
    if actual != doc.sha256:
        raise DocumentIntegrityError(
            f"document {doc.document_id} failed hash verification: "
            f"recorded {doc.sha256}, stored bytes hash to {actual}"
        )
    return data
