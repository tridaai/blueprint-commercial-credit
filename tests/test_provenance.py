"""Law 1 tests: a figure without provenance does not enter the file.

Three lines of defence are exercised: the Pydantic model refuses a
value with no provenance object and a provenance with no line/cell;
the port-contract check rejects a batch containing a value whose
provenance was smuggled past validation; and the reference
extractor's real output is complete by construction.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from commercial_credit.errors import ProvenanceError
from commercial_credit.extraction import OfflineReferenceExtractor, receive_extraction
from commercial_credit.models import (
    DocumentKind,
    ExtractedValue,
    Provenance,
    SourceDocument,
    require_complete_provenance,
)
from commercial_credit.seed import statement_document


def _value(**overrides) -> ExtractedValue:
    base = dict(
        field="revenue",
        value=Decimal("100"),
        period_label="FY2024",
        provenance=Provenance(document_id="doc-1", page=2, line="L1"),
    )
    base.update(overrides)
    return ExtractedValue(**base)


def test_value_without_provenance_is_rejected():
    with pytest.raises((ProvenanceError, ValidationError)):
        ExtractedValue(field="revenue", value=Decimal("1"), period_label="FY2024",
                       provenance=None)


def test_provenance_without_line_or_cell_is_rejected():
    with pytest.raises((ProvenanceError, ValidationError)):
        Provenance(document_id="doc-1", page=1, line=None, cell=None)
    with pytest.raises((ProvenanceError, ValidationError)):
        Provenance(document_id="doc-1", page=1, line="  ", cell="")


def test_cell_coordinate_is_accepted():
    p = Provenance(document_id="doc-9", page=3, cell="B12")
    assert p.cell == "B12"


def test_page_must_be_positive():
    with pytest.raises(ValidationError):
        Provenance(document_id="doc-1", page=0, line="L1")


def test_batch_contract_rejects_smuggled_incomplete_provenance():
    good = _value()
    smuggled = ExtractedValue.model_construct(
        field="revenue",
        value=Decimal("999"),
        unit="USD",
        period_label="FY2024",
        fiscal_year=2024,
        provenance=Provenance.model_construct(
            document_id="", page=1, line=None, cell=None
        ),
    )
    with pytest.raises(ProvenanceError):
        require_complete_provenance([good, smuggled])
    with pytest.raises(ProvenanceError):
        receive_extraction([good, smuggled])


def test_receive_extraction_passes_a_complete_batch():
    values = [_value(), _value(field="ebit", value=Decimal("5"))]
    assert receive_extraction(values) == values


def test_reference_extractor_output_is_complete_and_grounded():
    doc = SourceDocument(
        document_id="doc-seed-2024",
        credit_file_id="cf-test",
        kind=DocumentKind.FINANCIAL_STATEMENT,
        filename="hartline-fy2024-statements.json",
        sha256="0" * 64,
        byte_size=0,
        storage_key="x",
        period_label="FY2024",
    )
    values = OfflineReferenceExtractor().extract(doc, statement_document(2024))
    assert values, "extractor returned nothing for a seeded statement"
    require_complete_provenance(values)  # must not raise
    for v in values:
        assert v.provenance.document_id == "doc-seed-2024"
        assert v.provenance.page in (1, 2)
        assert v.provenance.line and v.provenance.line.startswith("L")
