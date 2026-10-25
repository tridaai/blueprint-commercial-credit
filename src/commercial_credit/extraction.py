"""Extraction: values out of borrower documents, provenance or nothing.

The generated borrower documents (seed.py) are structured: each is a
JSON document of pages, each page a list of labelled lines with known
values. That makes the ground truth known by construction, and lets
the reference extractor below do honest work — it reads the stored
bytes, finds each field on its page and line, and returns the value
*with the coordinates it found it at*.

A production deployment swaps the Extractor implementation for a
document-AI adapter behind the same port. The contract does not
move: provenance on every value, enforced by `receive_extraction`,
which the pipeline calls on every batch whatever the source.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from .errors import ProvenanceError
from .models import (
    ExtractedValue,
    Provenance,
    SourceDocument,
    require_complete_provenance,
)

# Statement line labels the spreading template consumes, mapped to the
# canonical field names extractions carry. Round 2's spreading engine
# keys off these field names; extraction is where they are born.
STATEMENT_FIELDS: dict[str, str] = {
    "Cash and equivalents": "cash_and_equivalents",
    "Accounts receivable": "accounts_receivable",
    "Inventory": "inventory",
    "Total current assets": "total_current_assets",
    "Net fixed assets": "net_fixed_assets",
    "Total assets": "total_assets",
    "Accounts payable": "accounts_payable",
    "Current portion of long-term debt": "current_portion_ltd",
    "Total current liabilities": "total_current_liabilities",
    "Long-term debt": "long_term_debt",
    "Total liabilities": "total_liabilities",
    "Total equity": "total_equity",
    "Revenue": "revenue",
    "Cost of goods sold": "cost_of_goods_sold",
    "Gross profit": "gross_profit",
    "Operating expenses": "operating_expenses",
    "EBIT": "ebit",
    "Interest expense": "interest_expense",
    "Net income": "net_income",
}

class OfflineReferenceExtractor:
    """Reference extractor over the generated structured documents.

    Deterministic, no model: it parses the stored document and returns
    every recognised statement line with the page and line it sits on.
    It exists so the pipeline, the provenance contract and the evals
    run end to end offline; it makes no claim about scanned PDFs.
    """

    def extract(self, document: SourceDocument, content: bytes) -> list[ExtractedValue]:
        try:
            payload = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
        if payload.get("document_type") != "financial_statement":
            return []
        period_label = payload.get("period_label") or document.period_label or ""
        fiscal_year = payload.get("fiscal_year")
        values: list[ExtractedValue] = []
        for page in payload.get("pages", []):
            page_no = int(page["page"])
            for line in page.get("lines", []):
                label = line.get("label")
                field = STATEMENT_FIELDS.get(label or "")
                if field is None:
                    continue
                try:
                    amount = Decimal(str(line["value"]))
                except (InvalidOperation, KeyError, TypeError):
                    continue
                values.append(
                    ExtractedValue(
                        field=field,
                        value=amount,
                        period_label=period_label,
                        fiscal_year=fiscal_year,
                        provenance=Provenance(
                            document_id=document.document_id,
                            page=page_no,
                            line=f"L{int(line['line'])}",
                        ),
                    )
                )
        require_complete_provenance(values)
        return values


def receive_extraction(values: list[ExtractedValue]) -> list[ExtractedValue]:
    """The receiving side of the extraction port contract.

    Whatever produced the batch — reference extractor today, a
    document-AI adapter in a deployment — its output passes through
    here before anything is stored. One value without complete
    provenance rejects the whole batch: a partial spread that quietly
    dropped its untraceable figures would be worse than no spread.
    """
    if not values:
        return values
    try:
        require_complete_provenance(values)
    except ProvenanceError:
        raise
    return values
