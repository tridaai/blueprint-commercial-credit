"""Document checklist generation, by facility type.

The checklist is deterministic and rule-cited: every item names the
rule that required it (`requirement_source`). Later rounds hang the
staleness/missing-input blocks (design law 7) off these items; this
round generates them and tracks satisfaction as documents arrive.
"""

from __future__ import annotations

from .models import ChecklistItem, DocumentKind, FacilityRequest, FacilityType

# (kind, label, periods_required, requirement_source)
_BASE: list[tuple[DocumentKind, str, int, str]] = [
    (DocumentKind.FINANCIAL_STATEMENT, "Business financial statements (fiscal year-end)", 3, "policy:core-file/three-years-financials"),
    (DocumentKind.FINANCIAL_STATEMENT, "Interim financial statements (most recent quarter)", 1, "policy:core-file/interim-financials"),
    (DocumentKind.TAX_RETURN, "Business tax returns", 3, "policy:core-file/three-years-tax-returns"),
    (DocumentKind.TAX_RETURN, "Guarantor personal tax returns", 2, "policy:core-file/guarantor-tax-returns"),
    (DocumentKind.PERSONAL_FINANCIAL_STATEMENT, "Guarantor personal financial statements (signed)", 1, "policy:core-file/guarantor-pfs"),
    (DocumentKind.ORG_DOCUMENT, "Formation / authority documents", 1, "policy:core-file/authority-documents"),
    (DocumentKind.DEBT_SCHEDULE, "Business debt schedule", 1, "policy:core-file/debt-schedule"),
    (DocumentKind.INSURANCE_CERTIFICATE, "Insurance certificates (hazard / liability)", 1, "policy:core-file/insurance"),
]

_BY_FACILITY: dict[FacilityType, list[tuple[DocumentKind, str, int, str]]] = {
    FacilityType.TERM_LOAN: [
        (DocumentKind.PROJECTIONS, "Management projections (term of facility)", 1, "policy:term-loan/projections"),
    ],
    FacilityType.REVOLVING_LINE: [
        (DocumentKind.AR_AGING, "Accounts receivable aging (most recent month-end)", 1, "policy:revolver/ar-aging"),
        (DocumentKind.INVENTORY_LISTING, "Inventory listing (most recent month-end)", 1, "policy:revolver/inventory-listing"),
        (DocumentKind.BORROWING_BASE_CERTIFICATE, "Borrowing base certificate (most recent)", 1, "policy:revolver/borrowing-base"),
    ],
    FacilityType.CRE_LOAN: [
        (DocumentKind.APPRAISAL, "Appraisal (MAI, within policy staleness window)", 1, "policy:cre/appraisal"),
        (DocumentKind.RENT_ROLL, "Rent roll (current)", 1, "policy:cre/rent-roll"),
        (DocumentKind.LEASES, "Leases for major tenants", 1, "policy:cre/major-leases"),
        (DocumentKind.ENVIRONMENTAL_REPORT, "Environmental report (Phase I)", 1, "policy:cre/environmental-phase-1"),
    ],
    FacilityType.CONSTRUCTION_LOAN: [
        (DocumentKind.CONSTRUCTION_BUDGET, "Construction budget and plans", 1, "policy:construction/budget-plans"),
        (DocumentKind.GC_CONTRACT, "General contractor contract", 1, "policy:construction/gc-contract"),
        (DocumentKind.PERMITS, "Permits / zoning evidence", 1, "policy:construction/permits"),
        (DocumentKind.APPRAISAL, "Appraisal (as-completed value)", 1, "policy:construction/appraisal-as-completed"),
    ],
    FacilityType.EQUIPMENT_FINANCE: [
        (DocumentKind.EQUIPMENT_INVOICE, "Equipment invoice / vendor quote", 1, "policy:equipment/invoice-quote"),
    ],
    FacilityType.SBA_7A: [
        (DocumentKind.SBA_FORM, "SBA Form 1919 (borrower information)", 1, "policy:sba-7a/form-1919"),
        (DocumentKind.SBA_FORM, "SBA Form 912 (statement of personal history), per owner ≥20%", 1, "policy:sba-7a/form-912"),
        (DocumentKind.PROJECTIONS, "Management projections (first two years)", 1, "policy:sba-7a/projections"),
    ],
}


def generate_checklist(facility: FacilityRequest) -> list[ChecklistItem]:
    """Build the checklist for a facility request. Pure function of the
    facility type: the same request always yields the same checklist,
    which the tests and evals rely on."""
    rows = list(_BASE) + list(_BY_FACILITY.get(facility.facility_type, []))
    items: list[ChecklistItem] = []
    for idx, (kind, label, periods, source) in enumerate(rows, start=1):
        items.append(
            ChecklistItem(
                item_id=f"chk-{idx:02d}",
                kind=kind,
                label=label,
                required=True,
                requirement_source=source,
                periods_required=periods,
            )
        )
    return items


def match_checklist_item(
    items: list[ChecklistItem], kind: DocumentKind
) -> ChecklistItem | None:
    """First unsatisfied item of this kind, if any. A document of a
    kind the checklist never asked for is still filed — it just does
    not satisfy anything."""
    for item in items:
        if item.kind == kind and not item.satisfied:
            return item
    return None
