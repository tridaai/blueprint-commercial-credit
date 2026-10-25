#!/usr/bin/env python3
"""Eval gate for the Commercial Credit Memo & Covenant Monitoring Agent.

Round 1 counters — the ones this round's machinery can be graded on:

  provenance_completeness   every extracted value carries complete
                            provenance (document, page, line/cell).
                            REQUIRED: 100%. A single untraceable
                            figure fails the gate (design law 1).
  extraction_correctness    extracted values equal the ground truth
                            the documents were generated from.
  checklist_coverage        every facility type generates its full
                            rule-cited checklist, incl. its
                            facility-specific items.
  provenance_rejection      a batch containing a value with
                            incomplete provenance is rejected whole.

Later rounds add counters (untraceable memo figure, stale-input
committee-ready, decline without coded reasons) as their machinery
lands. Exit code 0 = GATE: PASS.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commercial_credit.checklist import generate_checklist  # noqa: E402
from commercial_credit.documents import LocalDocumentStore  # noqa: E402
from commercial_credit.errors import ProvenanceError  # noqa: E402
from commercial_credit.extraction import receive_extraction  # noqa: E402
from commercial_credit.models import (  # noqa: E402
    DocumentKind,
    ExtractedValue,
    FacilityRequest,
    FacilityType,
    Provenance,
)
from commercial_credit.pipeline import CreditPipeline  # noqa: E402
from commercial_credit.seed import (  # noqa: E402
    ReferenceLosFeed,
    ground_truth,
    statement_document,
)
from commercial_credit.store import InMemoryCreditStore  # noqa: E402

EXPECTED_SPECIFICS: dict[FacilityType, set[DocumentKind]] = {
    FacilityType.TERM_LOAN: {DocumentKind.PROJECTIONS},
    FacilityType.REVOLVING_LINE: {
        DocumentKind.AR_AGING,
        DocumentKind.BORROWING_BASE_CERTIFICATE,
    },
    FacilityType.CRE_LOAN: {DocumentKind.APPRAISAL, DocumentKind.RENT_ROLL},
    FacilityType.CONSTRUCTION_LOAN: {DocumentKind.CONSTRUCTION_BUDGET, DocumentKind.PERMITS},
    FacilityType.EQUIPMENT_FINANCE: {DocumentKind.EQUIPMENT_INVOICE},
    FacilityType.SBA_7A: {DocumentKind.SBA_FORM},
}


def main() -> int:
    failures: list[str] = []

    # -- extraction over the seed borrower --------------------------
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
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

    complete = 0
    for v in values:
        p = v.provenance
        if p.document_id and p.page >= 1 and ((p.line or "").strip() or (p.cell or "").strip()):
            complete += 1
    completeness = (complete / len(values)) if values else 0.0
    print(f"provenance_completeness: {complete}/{len(values)} = {completeness:.3f} (required 1.000)")
    if completeness != 1.0 or not values:
        failures.append("provenance completeness below 100%")

    truth = ground_truth()
    correct = sum(1 for v in values if truth.get((v.fiscal_year, v.field)) == v.value)
    print(f"extraction_correctness: {correct}/{len(values)}")
    if correct != len(values):
        failures.append("extracted values diverge from ground truth")

    # -- checklist coverage ------------------------------------------
    covered = 0
    for ft, specifics in EXPECTED_SPECIFICS.items():
        items = generate_checklist(
            FacilityRequest(facility_type=ft, amount=Decimal("1"), purpose="eval", term_months=12)
        )
        kinds = {i.kind for i in items}
        if specifics <= kinds and all(i.requirement_source.startswith("policy:") for i in items):
            covered += 1
        else:
            failures.append(f"checklist for {ft.value} missing {specifics - kinds}")
    print(f"checklist_coverage: {covered}/{len(EXPECTED_SPECIFICS)} facility types")

    # -- provenance rejection (negative control) ----------------------
    smuggled = ExtractedValue.model_construct(
        field="revenue", value=Decimal("1"), unit="USD", period_label="FY2024",
        fiscal_year=2024,
        provenance=Provenance.model_construct(document_id="", page=1, line=None, cell=None),
    )
    try:
        receive_extraction([smuggled])
        print("provenance_rejection: FAIL (batch accepted)")
        failures.append("incomplete-provenance batch was accepted")
    except ProvenanceError:
        print("provenance_rejection: rejected as required")

    if failures:
        print("GATE: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
