"""Traced demo: open the sample application, file three years of
statements, run extraction, and print what the credit file holds —
every figure with the page and line it came from. Fully offline.
"""

from __future__ import annotations

from .documents import LocalDocumentStore
from .models import DocumentKind
from .pipeline import CreditPipeline
from .seed import ReferenceLosFeed, statement_document
from .store import InMemoryCreditStore


def main() -> int:
    pipeline = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    borrower, facility = ReferenceLosFeed().fetch_application("LOS-2026-0117")
    docs = [
        {
            "kind": DocumentKind.FINANCIAL_STATEMENT.value,
            "filename": f"hartline-fy{year}-statements.json",
            "data_hex": statement_document(year).hex(),
            "period_label": f"FY{year}",
        }
        for year in (2022, 2023, 2024)
    ]
    credit_file = pipeline.open_application(borrower, facility, documents=docs)
    values = pipeline.store.get_extractions(credit_file.credit_file_id)
    print(f"Credit file {credit_file.credit_file_id} — {borrower.legal_name}")
    print(f"Facility: {facility.facility_type.value} ${facility.amount:,} / {facility.term_months} months")
    print(f"Checklist: {len(credit_file.checklist)} items, "
          f"{len(credit_file.missing_required())} still outstanding")
    print(f"Documents filed: {len(credit_file.documents)} (SHA-256 recorded at intake)")
    print(f"Extracted values: {len(values)} — every one with provenance, e.g.:")
    for v in values[:3]:
        p = v.provenance
        print(f"  {v.period_label} {v.field} = {v.value} "
              f"[{p.document_id} p.{p.page} {p.line}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
