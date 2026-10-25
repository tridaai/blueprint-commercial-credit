"""Round 3: the computation engine, graded against hand computation.

The fixtures below are spreads built directly in the test, and every
expected figure was computed by hand from the fixture figures (the
arithmetic is shown in comments) — never by calling the engine and
writing down what it said. Money is exact to the cent; ratios are
exact to 4 decimal places, ROUND_HALF_UP, per the engine's stated
rounding rules.
"""

import base64
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.computations import (
    compute_entity_pack,
    compute_global_pack,
    verify_trace,
)
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.errors import ValidationError
from commercial_credit.models import (
    AdjustmentReasonCode,
    CovenantDefinition,
    DocumentKind,
    Elimination,
    ExtractedValue,
    PeriodSource,
    Provenance,
    Spread,
    SpreadAdjustment,
    SpreadLine,
    UnmappedEntry,
)
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import ReferenceLosFeed, statement_document
from commercial_credit.spreading import TEMPLATE_LINES
from commercial_credit.store import InMemoryCreditStore

D = Decimal

SECTION_PAGE = {"balance_sheet": 1, "income_statement": 2, "cash_flow_statement": 3}


def build_fixture_spread(
    credit_file_id: str,
    doc_prefix: str,
    figures: dict[int, dict[str, str]],
    *,
    adjustments: list[SpreadAdjustment] | None = None,
    unmapped: list[tuple[str, str, dict[int, str]]] | None = None,
) -> tuple[Spread, list[ExtractedValue]]:
    """A Spread + its extractions, built by hand from raw figures.

    `figures` maps fiscal year -> {template field: value string}.
    Fields absent from a year are unreported (None cells), exactly
    as a statement that does not carry the line would spread.
    """
    adjustments = adjustments or []
    periods = sorted(figures)
    lines: list[SpreadLine] = []
    extractions: list[ExtractedValue] = []
    line_no: dict[str, int] = {}
    for template in TEMPLATE_LINES:
        line_no[template.key] = line_no.get(template.section, 0) + 1
        line_no[template.section] = line_no[template.key]
        raw: dict[int, Decimal | None] = {}
        adjusted: dict[int, Decimal | None] = {}
        for year in periods:
            value = figures[year].get(template.field)
            raw[year] = D(value) if value is not None else None
            delta = sum(
                (a.amount for a in adjustments
                 if a.line_key == template.key and a.fiscal_year == year),
                D("0"),
            )
            adjusted[year] = (
                (raw[year] if raw[year] is not None else D("0")) + delta
                if raw[year] is not None or delta != 0
                else None
            )
            if value is not None:
                extractions.append(ExtractedValue(
                    field=template.field,
                    value=D(value),
                    period_label=f"FY{year}",
                    fiscal_year=year,
                    provenance=Provenance(
                        document_id=f"{doc_prefix}-{year}",
                        page=SECTION_PAGE[template.section],
                        line=f"L{line_no[template.key]}",
                    ),
                ))
        lines.append(SpreadLine(
            key=template.key, label=template.label, section=template.section,
            values=raw, adjusted_values=adjusted,
            changes={year: None for year in periods},
        ))
    unmapped_entries: list[UnmappedEntry] = []
    for field, name, values_by_year in unmapped or []:
        unmapped_entries.append(UnmappedEntry(
            field=field,
            name=name,
            values={year: D(values_by_year[year]) if year in values_by_year else None
                    for year in periods},
            sources={
                year: Provenance(document_id=f"{doc_prefix}-{year}", page=1,
                                 line="L13")
                for year in values_by_year
            },
        ))
    spread = Spread(
        spread_id=f"spread-fixture-{doc_prefix}",
        credit_file_id=credit_file_id,
        template_version="aldergrove-spread-v1",
        periods=periods,
        period_sources=[
            PeriodSource(
                fiscal_year=year,
                period_label=f"FY{year}",
                document_id=f"{doc_prefix}-{year}",
                document_sha256="ab" * 32,
                version=1,
            )
            for year in periods
        ],
        lines=lines,
        unmapped=unmapped_entries,
        adjustments=adjustments,
    )
    return spread, extractions


# ---------------------------------------------------------------------
# Fixture A — a two-year borrower with a full cash-flow statement, an
# R2 add-back to FY2024 EBIT, and a provenanced capital-lease figure.
#
# Hand computation for FY2024 (adjusted view):
#   EBITDA        = (500,000 + 50,000 add-back) + 120,000 D&A = 670,000
#   debt service  = 200,000 CPLTD(FY2023) + 90,000 interest
#                   + 10,000 capital leases              = 300,000
#   DSCR          = 670,000 / 300,000                     = 2.2333
#   FCCR          = (670,000 − 200,000 capex) / 300,000  = 1.5667
#   current ratio = 900,000 / 600,000                    = 1.5000
#   quick ratio   = (150,000 + 400,000) / 600,000        = 0.9167
#   working cap.  = 900,000 − 600,000                    = 300,000
#   funded debt   = 220,000 + 780,000                    = 1,000,000
#   debt/EBITDA   = 1,000,000 / 670,000                  = 1.4925
#   liabs / TNW   = 1,380,000 / 620,000 (no intangibles) = 2.2258
#   gross margin  = 1,200,000 / 3,000,000                = 0.4000
#   op. margin    = 550,000 / 3,000,000                  = 0.1833
#   EBITDA margin = 670,000 / 3,000,000                  = 0.2233
#   ROA           = 260,000 / 2,000,000                  = 0.1300
#   ROE           = 260,000 / 620,000                    = 0.4194
#   FCF           = 560,000 OCF − 200,000 capex          = 360,000
#   cash conv.    = 560,000 / 670,000                    = 0.8358
# ---------------------------------------------------------------------

FIXTURE_A_FIGURES: dict[int, dict[str, str]] = {
    2023: {
        "cash_and_equivalents": "120000", "accounts_receivable": "380000",
        "inventory": "330000", "total_current_assets": "830000",
        "net_fixed_assets": "1050000", "total_assets": "1880000",
        "accounts_payable": "360000", "current_portion_ltd": "200000",
        "total_current_liabilities": "560000", "long_term_debt": "800000",
        "total_liabilities": "1360000", "total_equity": "520000",
        "revenue": "2700000", "cost_of_goods_sold": "1650000",
        "gross_profit": "1050000", "operating_expenses": "620000",
        "ebit": "430000", "interest_expense": "84000",
        "net_income": "226000", "depreciation_and_amortization": "110000",
        "net_cash_from_operations": "505000", "capital_expenditures": "180000",
    },
    2024: {
        "cash_and_equivalents": "150000", "accounts_receivable": "400000",
        "inventory": "350000", "total_current_assets": "900000",
        "net_fixed_assets": "1100000", "total_assets": "2000000",
        "accounts_payable": "380000", "current_portion_ltd": "220000",
        "total_current_liabilities": "600000", "long_term_debt": "780000",
        "total_liabilities": "1380000", "total_equity": "620000",
        "revenue": "3000000", "cost_of_goods_sold": "1800000",
        "gross_profit": "1200000", "operating_expenses": "700000",
        "ebit": "500000", "interest_expense": "90000",
        "net_income": "260000", "depreciation_and_amortization": "120000",
        "net_cash_from_operations": "560000", "capital_expenditures": "200000",
    },
}

FIXTURE_A_ADJUSTMENT = SpreadAdjustment(
    adjustment_id="adj-fixture-a-relocation",
    credit_file_id="cf-fixture-a",
    line_key="ebit",
    fiscal_year=2024,
    amount=D("50000"),
    reason_code=AdjustmentReasonCode.NON_RECURRING_EXPENSE,
    reason="One-time plant relocation cost inside operating expenses",
    source=Provenance(document_id="doc-a-2024", page=2, line="L5"),
    proposed_by="credit-officer:a.reyes",
)

FIXTURE_A_LEASES = ExtractedValue(
    field="capital_lease_payments",
    value=D("10000"),
    period_label="FY2024",
    fiscal_year=2024,
    provenance=Provenance(document_id="doc-a-debt-schedule", page=1, line="L3"),
)


def _fixture_a_pack(view: str = "adjusted", **kwargs):
    spread, extractions = build_fixture_spread(
        "cf-fixture-a", "doc-a", FIXTURE_A_FIGURES,
        adjustments=[FIXTURE_A_ADJUSTMENT],
    )
    return compute_entity_pack(
        credit_file_id="cf-fixture-a",
        entity="Fixture Borrower A",
        spread=spread,
        extractions=extractions,
        supplemental=[FIXTURE_A_LEASES],
        view=view,
        **kwargs,
    ), spread


def _figure(pack, key: str, year: int):
    return next(
        f for f in pack.figures if f.key == key and f.fiscal_year == year
    )


EXPECTED_A_2024 = {
    "ebitda": D("670000.00"),
    "total_debt": D("1000000.00"),
    "debt_service": D("300000.00"),
    "working_capital": D("300000.00"),
    "current_ratio": D("1.5000"),
    "quick_ratio": D("0.9167"),
    "tangible_net_worth": D("620000"),
    "debt_to_ebitda": D("1.4925"),
    "debt_to_tangible_net_worth": D("2.2258"),
    "dscr": D("2.2333"),
    "fixed_charge_coverage": D("1.5667"),
    "gross_margin": D("0.4000"),
    "operating_margin": D("0.1833"),
    "ebitda_margin": D("0.2233"),
    "return_on_assets": D("0.1300"),
    "return_on_equity": D("0.4194"),
    "operating_cash_flow": D("560000"),
    "free_cash_flow": D("360000.00"),
    "cash_conversion": D("0.8358"),
}


def test_fixture_a_figures_match_hand_computation():
    pack, _ = _fixture_a_pack()
    for key, expected in EXPECTED_A_2024.items():
        fig = _figure(pack, key, 2024)
        assert fig.status == "computed", (key, fig.missing_inputs)
        assert fig.value == expected, (key, fig.value, expected)


def test_fixture_a_unadjusted_view_ignores_the_add_back():
    pack, _ = _fixture_a_pack(view="unadjusted")
    # EBITDA = 500,000 + 120,000 = 620,000; DSCR = 620,000/300,000.
    assert _figure(pack, "ebitda", 2024).value == D("620000.00")
    assert _figure(pack, "dscr", 2024).value == D("2.0667")
    assert _figure(pack, "operating_margin", 2024).value == D("0.1667")


def test_fixture_a_first_period_has_no_debt_service():
    pack, _ = _fixture_a_pack()
    # FY2023 EBITDA = 430,000 + 110,000 = 540,000 (hand-computed).
    assert _figure(pack, "ebitda", 2023).value == D("540000.00")
    dscr = _figure(pack, "dscr", 2023)
    assert dscr.status == "not_computable"
    assert any("no period before FY2023" in m for m in dscr.missing_inputs)


def test_fixture_a_derivation_traces_are_complete():
    pack, spread = _fixture_a_pack()
    report = verify_trace(pack, spread=spread)
    assert report.figures_checked > 0
    assert report.completeness == 1.0, report.gaps


def test_trace_detects_a_tampered_step_result():
    pack, spread = _fixture_a_pack()
    dscr = _figure(pack, "dscr", 2024)
    tampered_step = dscr.steps[0].model_copy(update={"result": D("9.9999")})
    tampered_fig = dscr.model_copy(update={"steps": [tampered_step]})
    tampered_pack = pack.model_copy(update={
        "figures": [tampered_fig if f is dscr else f for f in pack.figures],
    })
    report = verify_trace(tampered_pack, spread=spread)
    assert report.completeness < 1.0
    assert any("dscr" in gap for gap in report.gaps)


def test_trace_fails_without_extractions_behind_the_spread():
    spread, _ = build_fixture_spread(
        "cf-fixture-a", "doc-a", FIXTURE_A_FIGURES,
        adjustments=[FIXTURE_A_ADJUSTMENT],
    )
    pack = compute_entity_pack(
        credit_file_id="cf-fixture-a", entity="Fixture Borrower A",
        spread=spread, supplemental=[FIXTURE_A_LEASES],
    )
    report = verify_trace(pack, spread=spread)
    assert report.completeness < 1.0
    assert any("no provenance chain" in gap for gap in report.gaps)


def test_determinism_same_inputs_same_pack():
    pack1, _ = _fixture_a_pack()
    pack2, _ = _fixture_a_pack()
    assert pack1.pack_id == pack2.pack_id
    assert pack1.model_dump_json() == pack2.model_dump_json()
    other, _ = _fixture_a_pack(view="unadjusted")
    assert other.pack_id != pack1.pack_id


def test_tangible_net_worth_deducts_reported_goodwill():
    spread, extractions = build_fixture_spread(
        "cf-fixture-a", "doc-a", FIXTURE_A_FIGURES,
        adjustments=[FIXTURE_A_ADJUSTMENT],
        unmapped=[("unmapped:goodwill", "Goodwill", {2024: "100000"})],
    )
    pack = compute_entity_pack(
        credit_file_id="cf-fixture-a", entity="Fixture Borrower A",
        spread=spread, extractions=extractions,
        supplemental=[FIXTURE_A_LEASES],
    )
    # TNW = 620,000 − 100,000 goodwill = 520,000;
    # liabilities / TNW = 1,380,000 / 520,000 = 2.6538.
    assert _figure(pack, "tangible_net_worth", 2024).value == D("520000.00")
    assert _figure(pack, "debt_to_tangible_net_worth", 2024).value == D("2.6538")
    report = verify_trace(pack, spread=spread)
    assert report.completeness == 1.0, report.gaps


# ---------------------------------------------------------------------
# Covenant headroom against given definitions
# ---------------------------------------------------------------------


def test_covenant_headroom_against_hand_computed_values():
    definitions = [
        CovenantDefinition(name="Min DSCR", metric_key="dscr", operator=">=",
                           threshold=D("1.25"), fiscal_year=2024),
        CovenantDefinition(name="Max leverage", metric_key="debt_to_ebitda",
                           operator="<=", threshold=D("3.00"), fiscal_year=2024),
        CovenantDefinition(name="Min DSCR (tight)", metric_key="dscr",
                           operator=">=", threshold=D("2.50"), fiscal_year=2024),
        CovenantDefinition(name="DSCR incl. leases", metric_key="dscr",
                           operator=">=", threshold=D("2.00"), fiscal_year=2024,
                           require_capital_leases=True),
        CovenantDefinition(name="DSCR incl. leases 2023", metric_key="dscr",
                           operator=">=", threshold=D("1.10"), fiscal_year=2023,
                           require_capital_leases=True),
        CovenantDefinition(name="Min DSCR 2023", metric_key="dscr",
                           operator=">=", threshold=D("1.25"), fiscal_year=2023),
    ]
    pack, _ = _fixture_a_pack(covenant_definitions=definitions)
    results = {r.name: r for r in pack.covenant_results}

    # headroom = 2.2333 − 1.25 = 0.9833; pct = 0.9833 / 1.25 = 0.7866.
    r = results["Min DSCR"]
    assert r.status == "computed" and r.compliant is True
    assert r.value == D("2.2333") and r.headroom == D("0.9833")
    assert r.headroom_pct == D("0.7866")

    # headroom = 3.00 − 1.4925 = 1.5075; pct = 1.5075 / 3 = 0.5025.
    r = results["Max leverage"]
    assert r.status == "computed" and r.compliant is True
    assert r.headroom == D("1.5075") and r.headroom_pct == D("0.5025")

    # Breach: headroom = 2.2333 − 2.50 = −0.2667.
    r = results["Min DSCR (tight)"]
    assert r.status == "computed" and r.compliant is False
    assert r.headroom == D("-0.2667") and r.headroom_pct == D("-0.1067")

    # Lease figure was supplied for 2024: the requirement is met.
    r = results["DSCR incl. leases"]
    assert r.status == "computed" and r.compliant is True
    assert r.headroom == D("0.2333")

    # No lease figure for 2023: the definition cannot be answered.
    r = results["DSCR incl. leases 2023"]
    assert r.status == "not_computable"
    assert any("capital_lease_payments" in m for m in r.missing_inputs)

    # FY2023 DSCR itself is not computable (no prior period).
    r = results["Min DSCR 2023"]
    assert r.status == "not_computable" and r.value is None


# ---------------------------------------------------------------------
# Global cash flow: borrower + guarantor, eliminations, exclusions.
#
# Hand computation for FY2024:
#   Borrower B: EBITDA = 400,000 + 80,000 = 480,000;
#               debt service = 100,000 CPLTD(FY2023) + 60,000 = 160,000.
#   Guarantor G: EBITDA = 150,000 + 20,000 = 170,000;
#               debt service = 40,000 + 30,000 = 70,000.
#   Combined cash flow = 480,000 + 170,000 − 50,000 rent elimination
#                      = 600,000.
#   Combined debt service = 160,000 + 70,000 − 20,000 intercompany
#                      elimination = 210,000.
#   Global DSCR = 600,000 / 210,000 = 2.8571.
# ---------------------------------------------------------------------

FIXTURE_B_FIGURES: dict[int, dict[str, str]] = {
    2023: {"current_portion_ltd": "100000"},
    2024: {
        "ebit": "400000", "depreciation_and_amortization": "80000",
        "interest_expense": "60000", "current_portion_ltd": "120000",
    },
}

FIXTURE_G_FIGURES: dict[int, dict[str, str]] = {
    2023: {"current_portion_ltd": "40000"},
    2024: {
        "ebit": "150000", "depreciation_and_amortization": "20000",
        "interest_expense": "30000", "current_portion_ltd": "55000",
    },
}

GLOBAL_ELIMINATIONS = [
    Elimination(
        name="intercompany_rent", fiscal_year=2024, amount=D("50000"),
        target="cash_flow",
        reason="Rent the borrower pays the guarantor's property company "
               "is income in the guarantor's figures; a naive sum counts "
               "the same cash twice",
    ),
    Elimination(
        name="intercompany_loan_service", fiscal_year=2024, amount=D("20000"),
        target="debt_service",
        reason="Debt service on the intercompany note is owed inside "
               "the combined group, not to an outside lender",
    ),
]


def _entity_pack(credit_file_id: str, entity: str, doc_prefix: str, figures):
    spread, extractions = build_fixture_spread(credit_file_id, doc_prefix, figures)
    return compute_entity_pack(
        credit_file_id=credit_file_id, entity=entity,
        spread=spread, extractions=extractions,
    )


def test_global_dscr_with_eliminations_matches_hand_computation():
    borrower = _entity_pack("cf-fixture-b", "Borrower B", "doc-b", FIXTURE_B_FIGURES)
    guarantor = _entity_pack("cf-fixture-b", "Guarantor G", "doc-g", FIXTURE_G_FIGURES)
    assert _figure(borrower, "dscr", 2024).value == D("3.0000")

    pack = compute_global_pack(
        credit_file_id="cf-fixture-b",
        borrower_pack=borrower,
        entity_packs={"Guarantor G": guarantor},
        eliminations=GLOBAL_ELIMINATIONS,
        expected_guarantors=["Guarantor G"],
    )
    assert _figure(pack, "global_cash_flow", 2024).value == D("600000.00")
    assert _figure(pack, "global_debt_service", 2024).value == D("210000.00")
    assert _figure(pack, "global_dscr", 2024).value == D("2.8571")
    assert pack.entities == ["Borrower B", "Guarantor G"]
    assert pack.exclusions == []

    report = verify_trace(
        pack, packs={borrower.pack_id: borrower, guarantor.pack_id: guarantor}
    )
    assert report.completeness == 1.0, report.gaps


def test_elimination_without_a_reason_is_rejected():
    borrower = _entity_pack("cf-fixture-b", "Borrower B", "doc-b", FIXTURE_B_FIGURES)
    unexplained = Elimination.model_construct(
        name="quiet_removal", fiscal_year=2024, amount=D("1000"),
        target="cash_flow", reason="   ",
    )
    with pytest.raises(ValidationError):
        compute_global_pack(
            credit_file_id="cf-fixture-b", borrower_pack=borrower,
            eliminations=[unexplained],
        )
    negative = Elimination.model_construct(
        name="negative_removal", fiscal_year=2024, amount=D("-5"),
        target="cash_flow", reason="stated, but the amount is impossible",
    )
    with pytest.raises(ValidationError):
        compute_global_pack(
            credit_file_id="cf-fixture-b", borrower_pack=borrower,
            eliminations=[negative],
        )


def test_guarantor_without_a_spread_is_refused_or_excluded_on_record():
    borrower = _entity_pack("cf-fixture-b", "Borrower B", "doc-b", FIXTURE_B_FIGURES)
    with pytest.raises(ValidationError):
        compute_global_pack(
            credit_file_id="cf-fixture-b", borrower_pack=borrower,
            expected_guarantors=["Ghost Guarantor"],
        )
    pack = compute_global_pack(
        credit_file_id="cf-fixture-b", borrower_pack=borrower,
        expected_guarantors=["Ghost Guarantor"],
        exclude_missing_guarantors=True,
    )
    assert [e.entity for e in pack.exclusions] == ["Ghost Guarantor"]
    # Borrower alone: global DSCR is its own 480,000 / 160,000 = 3.0000.
    assert _figure(pack, "global_dscr", 2024).value == D("3.0000")


# ---------------------------------------------------------------------
# Missing and zero inputs: named, never zero-filled
# ---------------------------------------------------------------------


def test_zero_revenue_and_zero_liabilities_are_named_not_divided():
    figures = {
        2024: {
            "revenue": "0", "gross_profit": "0", "ebit": "0",
            "net_income": "0", "total_current_assets": "100000",
            "total_current_liabilities": "0", "total_assets": "100000",
            "total_equity": "100000",
        }
    }
    spread, extractions = build_fixture_spread("cf-fixture-c", "doc-c", figures)
    pack = compute_entity_pack(
        credit_file_id="cf-fixture-c", entity="Fixture Borrower C",
        spread=spread, extractions=extractions,
    )
    for key in ("gross_margin", "operating_margin"):
        fig = _figure(pack, key, 2024)
        assert fig.status == "not_computable", key
        assert any("revenue" in m and "zero" in m for m in fig.missing_inputs), key
    cr = _figure(pack, "current_ratio", 2024)
    assert cr.status == "not_computable"
    assert any("zero" in m for m in cr.missing_inputs)


def test_supplemental_figure_without_provenance_is_rejected():
    spread, extractions = build_fixture_spread(
        "cf-fixture-a", "doc-a", FIXTURE_A_FIGURES,
        adjustments=[FIXTURE_A_ADJUSTMENT],
    )
    smuggled = ExtractedValue.model_construct(
        field="capital_lease_payments", value=D("1"), unit="USD",
        period_label="FY2024", fiscal_year=2024,
        provenance=Provenance.model_construct(
            document_id="", page=1, line=None, cell=None
        ),
    )
    with pytest.raises(Exception):
        compute_entity_pack(
            credit_file_id="cf-fixture-a", entity="Fixture Borrower A",
            spread=spread, extractions=extractions, supplemental=[smuggled],
        )


# ---------------------------------------------------------------------
# Pipeline + store + API: packs as stored artefacts
# ---------------------------------------------------------------------


def _seed_pipeline() -> tuple[CreditPipeline, str]:
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
    return pipeline, credit_file.credit_file_id


def test_pipeline_pack_is_stored_and_recompute_returns_the_same_artefact():
    pipeline, fid = _seed_pipeline()
    pack = pipeline.compute_metrics(fid)
    assert pipeline.store.get_computation_pack(pack.pack_id) is not None
    again = pipeline.compute_metrics(fid)
    assert again.pack_id == pack.pack_id
    assert again.model_dump_json() == pack.model_dump_json()
    assert [
        p.pack_id for p in pipeline.store.list_computation_packs(fid)
    ] == [pack.pack_id]
    actions = [e.action for e in pipeline.store.list_audit(fid)]
    assert actions.count("computation_pack_stored") == 1


def test_hartline_pack_spot_checks_against_hand_computation():
    pipeline, fid = _seed_pipeline()
    pack = pipeline.compute_metrics(fid)
    # FY2024, from the seed figures, by hand:
    #   current ratio  = 3,951,000 / 1,749,000 = 2.2590
    #   quick ratio    = (521,000 + 1,498,000) / 1,749,000 = 1.1544
    #   working capital = 3,951,000 − 1,749,000 = 2,202,000
    #   funded debt    = 360,000 + 2,760,000 = 3,120,000
    #   debt service   = 335,000 CPLTD(FY2023) + 251,700 = 586,700
    #   gross margin   = 3,712,200 / 12,374,000 = 0.3000
    assert _figure(pack, "current_ratio", 2024).value == D("2.2590")
    assert _figure(pack, "quick_ratio", 2024).value == D("1.1544")
    assert _figure(pack, "working_capital", 2024).value == D("2202000.00")
    assert _figure(pack, "total_debt", 2024).value == D("3120000.00")
    assert _figure(pack, "debt_service", 2024).value == D("586700.00")
    assert _figure(pack, "gross_margin", 2024).value == D("0.3000")
    # The seed statements carry no cash-flow page: EBITDA and the
    # cash-flow figures are honestly not computable, naming D&A.
    ebitda = _figure(pack, "ebitda", 2024)
    assert ebitda.status == "not_computable"
    assert any("depreciation_and_amortization" in m for m in ebitda.missing_inputs)
    spread = pipeline.get_spread(fid)
    report = verify_trace(pack, spread=spread)
    assert report.completeness == 1.0, report.gaps


@pytest.fixture()
def client():
    pipeline = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app)


def test_api_computation_flow(client):
    fid = client.post(
        "/applications/from-los", json={"application_ref": "LOS-2026-0117"}
    ).json()["credit_file_id"]
    for year in (2022, 2023, 2024):
        resp = client.post(
            f"/applications/{fid}/documents",
            json={
                "kind": "financial_statement",
                "filename": f"hartline-fy{year}.json",
                "content_b64": base64
                .b64encode(statement_document(year))
                .decode("ascii"),
                "period_label": f"FY{year}",
            },
        )
        assert resp.status_code == 201
    assert client.post(f"/applications/{fid}/extract").status_code == 200
    pack = client.get(f"/applications/{fid}/computations")
    assert pack.status_code == 200
    body = pack.json()
    assert body["scope"] == "entity" and body["engine_version"]
    cr = next(
        f for f in body["figures"]
        if f["key"] == "current_ratio" and f["fiscal_year"] == 2024
    )
    assert D(str(cr["value"])) == D("2.2590")

    posted = client.post(
        f"/applications/{fid}/computations",
        json={
            "covenant_definitions": [
                {"name": "Min current ratio", "metric_key": "current_ratio",
                 "operator": ">=", "threshold": "1.10", "fiscal_year": 2024}
            ]
        },
    )
    assert posted.status_code == 201
    result = posted.json()["covenant_results"][0]
    assert result["compliant"] is True
    assert D(str(result["headroom"])) == D("1.1590")

    fetched = client.get(f"/applications/{fid}/computations/{body['pack_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["pack_id"] == body["pack_id"]
    missing = client.get(f"/applications/{fid}/computations/pack-nope")
    assert missing.status_code == 404


def test_api_global_refuses_a_guarantor_with_no_spread(client):
    fid = client.post(
        "/applications/from-los", json={"application_ref": "LOS-2026-0117"}
    ).json()["credit_file_id"]
    resp = client.post(
        f"/applications/{fid}/computations/global",
        json={"expected_guarantors": ["Nobody Here"]},
    )
    assert resp.status_code == 422
    excluded = client.post(
        f"/applications/{fid}/computations/global",
        json={
            "expected_guarantors": ["Nobody Here"],
            "exclude_missing_guarantors": True,
        },
    )
    assert excluded.status_code == 201
    assert excluded.json()["exclusions"][0]["entity"] == "Nobody Here"
