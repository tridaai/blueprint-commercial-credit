import hashlib

import pytest

from commercial_credit.documents import (
    LocalDocumentStore,
    read_document_verified,
    register_document,
    sha256_hex,
)
from commercial_credit.errors import DocumentIntegrityError
from commercial_credit.models import DocumentKind
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import ReferenceLosFeed, statement_document
from commercial_credit.store import InMemoryCreditStore


def _open_file():
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    borrower, facility = ReferenceLosFeed().fetch_application("LOS-2026-0117")
    credit_file = pipeline.open_application(borrower, facility)
    return pipeline, credit_file


def test_register_records_sha256_of_the_bytes():
    pipeline, credit_file = _open_file()
    data = statement_document(2024)
    doc = register_document(
        pipeline.store, pipeline.documents, credit_file,
        kind=DocumentKind.FINANCIAL_STATEMENT,
        filename="fy2024.json", data=data, period_label="FY2024",
    )
    assert doc.sha256 == hashlib.sha256(data).hexdigest()
    assert doc.byte_size == len(data)
    assert sha256_hex(data) == doc.sha256


def test_registered_document_reads_back_verified():
    pipeline, credit_file = _open_file()
    data = statement_document(2023)
    doc = register_document(
        pipeline.store, pipeline.documents, credit_file,
        kind=DocumentKind.FINANCIAL_STATEMENT, filename="fy2023.json", data=data,
    )
    assert read_document_verified(pipeline.documents, doc) == data


def test_tampered_bytes_fail_verification():
    pipeline, credit_file = _open_file()
    doc = register_document(
        pipeline.store, pipeline.documents, credit_file,
        kind=DocumentKind.FINANCIAL_STATEMENT, filename="fy2022.json",
        data=statement_document(2022),
    )
    # Simulate a swapped/corrupted object behind the same key.
    pipeline.documents.put(doc.storage_key, b'{"tampered": true}', "application/json")
    with pytest.raises(DocumentIntegrityError):
        read_document_verified(pipeline.documents, doc)


def test_document_satisfies_its_checklist_item():
    pipeline, credit_file = _open_file()
    before = len(credit_file.missing_required())
    doc = register_document(
        pipeline.store, pipeline.documents, credit_file,
        kind=DocumentKind.DEBT_SCHEDULE, filename="debt-schedule.json", data=b"{}",
    )
    item = credit_file.checklist_item(
        next(i.item_id for i in credit_file.checklist if i.kind == DocumentKind.DEBT_SCHEDULE)
    )
    assert item is not None and item.satisfied_by_document_id == doc.document_id
    saved = pipeline.store.get_credit_file(credit_file.credit_file_id)
    assert saved is not None
    assert len(saved.missing_required()) == before - 1


def test_unlisted_document_kind_is_filed_without_satisfying():
    pipeline, credit_file = _open_file()
    before = len(credit_file.missing_required())
    register_document(
        pipeline.store, pipeline.documents, credit_file,
        kind=DocumentKind.OTHER, filename="misc.json", data=b"{}",
    )
    assert len(credit_file.missing_required()) == before
    assert len(credit_file.documents) == 1
