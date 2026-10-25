"""The Aldergrove Bank borrower book, generated.

One discreet note lives in the README; the mechanics live here. The
seed borrower — Hartline Manufacturing, a mid-market metal
fabricator — is generated, not collected: its financial statements
are built from the figures below, rendered into structured documents
(pages of labelled lines) whose ground truth is therefore known by
construction. The evals grade extraction against that ground truth;
later rounds engineer further borrowers to exercise specific policy
tests and traps.
"""

from __future__ import annotations

import json
from decimal import Decimal

from .models import (
    Borrower,
    BorrowerSegment,
    FacilityRequest,
    FacilityType,
    Guarantor,
)

# Hartline Manufacturing, Inc. — figures in USD, by fiscal year.
# Internally consistent: each year's balance sheet balances, and the
# income statement foots. Ground truth for extraction evals.
HARTLINE_FIGURES: dict[int, dict[str, str]] = {
    2022: {
        "cash_and_equivalents": "412000",
        "accounts_receivable": "1185000",
        "inventory": "1640000",
        "total_current_assets": "3237000",
        "net_fixed_assets": "2875000",
        "total_assets": "6112000",
        "accounts_payable": "864000",
        "current_portion_ltd": "310000",
        "total_current_liabilities": "1498000",
        "long_term_debt": "2190000",
        "total_liabilities": "3688000",
        "total_equity": "2424000",
        "revenue": "9845000",
        "cost_of_goods_sold": "6694600",
        "gross_profit": "3150400",
        "operating_expenses": "2389200",
        "ebit": "761200",
        "interest_expense": "196400",
        "net_income": "423100",
    },
    2023: {
        "cash_and_equivalents": "368000",
        "accounts_receivable": "1342000",
        "inventory": "1815000",
        "total_current_assets": "3525000",
        "net_fixed_assets": "3104000",
        "total_assets": "6629000",
        "accounts_payable": "927000",
        "current_portion_ltd": "335000",
        "total_current_liabilities": "1606000",
        "long_term_debt": "2485000",
        "total_liabilities": "4091000",
        "total_equity": "2538000",
        "revenue": "10982000",
        "cost_of_goods_sold": "7577580",
        "gross_profit": "3404420",
        "operating_expenses": "2597400",
        "ebit": "807020",
        "interest_expense": "224900",
        "net_income": "436300",
    },
    2024: {
        "cash_and_equivalents": "521000",
        "accounts_receivable": "1498000",
        "inventory": "1932000",
        "total_current_assets": "3951000",
        "net_fixed_assets": "3498000",
        "total_assets": "7449000",
        "accounts_payable": "1013000",
        "current_portion_ltd": "360000",
        "total_current_liabilities": "1749000",
        "long_term_debt": "2760000",
        "total_liabilities": "4509000",
        "total_equity": "2940000",
        "revenue": "12374000",
        "cost_of_goods_sold": "8661800",
        "gross_profit": "3712200",
        "operating_expenses": "2814600",
        "ebit": "897600",
        "interest_expense": "251700",
        "net_income": "484600",
    },
}

_BALANCE_SHEET_LABELS: list[tuple[str, str]] = [
    ("cash_and_equivalents", "Cash and equivalents"),
    ("accounts_receivable", "Accounts receivable"),
    ("inventory", "Inventory"),
    ("total_current_assets", "Total current assets"),
    ("net_fixed_assets", "Net fixed assets"),
    ("total_assets", "Total assets"),
    ("accounts_payable", "Accounts payable"),
    ("current_portion_ltd", "Current portion of long-term debt"),
    ("total_current_liabilities", "Total current liabilities"),
    ("long_term_debt", "Long-term debt"),
    ("total_liabilities", "Total liabilities"),
    ("total_equity", "Total equity"),
]

_INCOME_LABELS: list[tuple[str, str]] = [
    ("revenue", "Revenue"),
    ("cost_of_goods_sold", "Cost of goods sold"),
    ("gross_profit", "Gross profit"),
    ("operating_expenses", "Operating expenses"),
    ("ebit", "EBIT"),
    ("interest_expense", "Interest expense"),
    ("net_income", "Net income"),
]


def hartline_borrower() -> Borrower:
    return Borrower(
        legal_name="Hartline Manufacturing, Inc.",
        dba="Hartline Fabrication",
        ein_last4="4817",
        segment=BorrowerSegment.MANUFACTURING,
        state_of_formation="OH",
        years_in_business=17,
        primary_contact_name="Dana Hartline",
        primary_contact_email="dana@hartlinefab.example",
    )


def hartline_facility() -> FacilityRequest:
    return FacilityRequest(
        facility_type=FacilityType.TERM_LOAN,
        amount=Decimal("1500000"),
        purpose="Refinance existing term debt and fund a CNC machining line",
        term_months=60,
        collateral_description="All business assets; first lien on new equipment",
        guarantors=[
            Guarantor(name="Dana Hartline", ownership_pct=Decimal("62")),
            Guarantor(name="Marcus Hartline", ownership_pct=Decimal("38")),
        ],
    )


def statement_document(fiscal_year: int) -> bytes:
    """Render one fiscal year's statement as a structured document.

    Page 1 is the balance sheet, page 2 the income statement; each
    line carries its label, value and line number, so extraction
    provenance points at a real coordinate in the stored bytes.
    """
    figures = HARTLINE_FIGURES[fiscal_year]

    def page(number: int, title: str, rows: list[tuple[str, str]]) -> dict:
        return {
            "page": number,
            "title": title,
            "lines": [
                {"line": idx, "label": label, "value": figures[field]}
                for idx, (field, label) in enumerate(rows, start=1)
            ],
        }

    payload = {
        "document_type": "financial_statement",
        "borrower": "Hartline Manufacturing, Inc.",
        "fiscal_year": fiscal_year,
        "period_label": f"FY{fiscal_year}",
        "basis": "audited",
        "pages": [
            page(1, "Balance Sheet", _BALANCE_SHEET_LABELS),
            page(2, "Income Statement", _INCOME_LABELS),
        ],
    }
    return json.dumps(payload, indent=2).encode("utf-8")


def ground_truth() -> dict[tuple[int, str], Decimal]:
    """(fiscal_year, field) -> value, straight from the figures the
    documents were rendered from. Extraction is graded against this."""
    return {
        (year, field): Decimal(value)
        for year, fields in HARTLINE_FIGURES.items()
        for field, value in fields.items()
    }


class ReferenceLosFeed:
    """Reference LOS feed: the sample application, served by ref.

    A deployment implements the LosFeed port against the bank's real
    origination system; this feed exists so intake runs end to end
    offline and the demo has an application to open.
    """

    def fetch_application(
        self, application_ref: str
    ) -> tuple[Borrower, FacilityRequest]:
        if application_ref != "LOS-2026-0117":
            from .errors import NotFoundError

            raise NotFoundError(f"no LOS application {application_ref!r}")
        return hartline_borrower(), hartline_facility()
