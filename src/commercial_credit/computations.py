"""The computation engine: ratios, coverage and global cash flow,
computed in code from the spread — never by a model (design law 1).

Everything here is a pure function of its inputs. Given a `Spread`
(round 2), the extractions behind it, any supplemental figures the
run is handed, and any covenant definitions it is handed, the engine
produces a `ComputationPack`: a stored, content-addressed artefact
in which every figure carries its derivation as a chain of recorded
steps. A step's inputs name spread lines (with the provenance chain
of the extraction that fed the cell, plus any adjustments folded
in), supplemental figures (with their provenance), earlier figures,
or — in a global pack — named eliminations. `verify_trace` walks
those chains mechanically: a figure is traceable only if every leaf
resolves to a provenanced input and every step's arithmetic
re-performs to its recorded result.

Definitions (spelled out, because a ratio nobody defined is a
number nobody can defend):

  EBITDA            operating income (the spread's EBIT line) plus
                    depreciation & amortisation, both on the pack's
                    view (adjusted by default — the adjustments are
                    the R2 records, named in the trace).
  Funded debt       current portion of long-term debt + long-term
                    debt, as reported at the period's balance date.
  Debt service      for period P: scheduled principal + interest +
                    capital lease payments. Scheduled principal is
                    the current portion of long-term debt reported
                    at the END OF THE PRIOR PERIOD — the principal
                    the balance sheet said would fall due during P.
                    Interest is P's interest expense. Capital lease
                    payments enter only when the run is handed a
                    provenanced supplemental figure for them.
  DSCR              EBITDA / debt service.
  Fixed-charge      (EBITDA − capital expenditures − cash income
  coverage          taxes − owner distributions) / debt service.
                    Cash taxes and distributions enter only when
                    supplied as supplemental figures.
  Current ratio     total current assets / total current
                    liabilities. Quick ratio: (cash + accounts
                    receivable) / total current liabilities.
                    Working capital: TCA − TCL.
  Tangible          total equity less intangible assets as reported
  net worth         in the spread (a mapped line if the taxonomy
                    grows one, named unmapped entries such as
                    goodwill, or a supplemental figure). When the
                    statements report no intangibles, TNW equals
                    reported equity and the trace says so.
  Debt / TNW        total liabilities / tangible net worth.
  Margins           gross profit, EBIT and EBITDA, each over
                    revenue. ROA / ROE: net income over period-end
                    total assets / total equity (period-end, not
                    averaged — the spread is a balance-date record).
  Operating CF      the spread's net cash from operations, carried
                    with its provenance. Free CF: OCF − capex.
                    Cash conversion: OCF / EBITDA.
  Global cash flow  the sum of the combined entities' EBITDA, less
                    named cash-flow eliminations; global debt
                    service likewise; global DSCR their quotient.

Two honesty rules, applied uniformly:

  * A required input that is absent makes the figure *not
    computable*, naming the input. Nothing is zero-filled, ever.
  * An optional component (capital leases, cash taxes,
    distributions, intangibles) that the statements do not report
    is recorded as "not reported" in the figure's notes and left
    out — the artefact always shows which components entered.

Rounding, applied as each step is recorded so the trace replays
exactly: money to the cent, ratios to 4 decimal places, both
ROUND_HALF_UP.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field as dc_field
from decimal import ROUND_HALF_UP, Decimal

from .errors import ValidationError
from .models import (
    ComputationPack,
    ComputationStep,
    ComputedFigure,
    CovenantDefinition,
    CovenantResult,
    Elimination,
    ExclusionRecord,
    ExtractedValue,
    Provenance,
    Spread,
    StepInput,
    require_complete_provenance,
    require_valid_elimination,
)
from .spreading import period_year_of

ENGINE_VERSION = "aldergrove-compute-v1"

CENT = Decimal("0.01")
RATIO_PLACES = Decimal("0.0001")

CAPITAL_LEASE_FIELD = "capital_lease_payments"
CASH_TAXES_FIELD = "cash_income_taxes_paid"
DISTRIBUTIONS_FIELD = "owner_distributions"
INTANGIBLE_FIELD = "intangible_assets"

# Unmapped spread entries that count as intangible assets when a
# borrower's statement reports them outside the mapped taxonomy.
INTANGIBLE_UNMAPPED_FIELDS = {
    "unmapped:goodwill",
    "unmapped:intangible_assets",
    "unmapped:other_intangible_assets",
}


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def ratio(value: Decimal) -> Decimal:
    return value.quantize(RATIO_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------
# Provenance: which extraction fed each spread cell
# ---------------------------------------------------------------------


def _provenance_index(
    spread: Spread, extractions: list[ExtractedValue]
) -> dict[tuple[str, int], ExtractedValue]:
    """(field, fiscal year) -> the extracted value the spread rests
    on: the first occurrence, in the period's current document, in
    page/line order — the same selection the spread builder makes."""
    doc_by_year = {ps.fiscal_year: ps.document_id for ps in spread.period_sources}
    index: dict[tuple[str, int], ExtractedValue] = {}
    for v in extractions:
        year = period_year_of(v)
        if doc_by_year.get(year) != v.provenance.document_id:
            continue
        key = (v.field, year)
        current = index.get(key)
        if current is None or _coord(v) < _coord(current):
            index[key] = v
    return index


def _coord(v: ExtractedValue) -> tuple[int, str, str]:
    return (v.provenance.page, v.provenance.line or "", v.provenance.cell or "")


@dataclass
class _Ctx:
    spread: Spread
    view: str
    entity: str
    prov: dict[tuple[str, int], ExtractedValue]
    supplemental: dict[tuple[str, int], ExtractedValue]
    lines: dict[str, object] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        self.lines = {line.key: line for line in self.spread.lines}


def _spread_input(ctx: _Ctx, line_key: str, year: int) -> StepInput | None:
    line = ctx.lines.get(line_key)
    if line is None:
        return None
    values = line.adjusted_values if ctx.view == "adjusted" else line.values
    value = values.get(year)
    if value is None:
        return None
    chain: list[Provenance] = []
    source = ctx.prov.get((line_key, year))
    if source is not None:
        chain.append(source.provenance)
    adjustment_ids: list[str] = []
    if ctx.view == "adjusted":
        for adj in ctx.spread.adjustments:
            if adj.line_key == line_key and adj.fiscal_year == year:
                adjustment_ids.append(adj.adjustment_id)
                chain.append(adj.source)
    return StepInput(
        kind="spread_line",
        line_key=line_key,
        fiscal_year=year,
        view=ctx.view,
        value=value,
        provenance=chain,
        adjustment_ids=adjustment_ids,
    )


def _supplemental_input(ctx: _Ctx, field: str, year: int) -> StepInput | None:
    v = ctx.supplemental.get((field, year))
    if v is None:
        return None
    return StepInput(
        kind="supplemental",
        line_key=field,
        fiscal_year=year,
        value=v.value,
        provenance=[v.provenance],
    )


def _figure_input(fig: ComputedFigure, pack_id: str | None = None) -> StepInput:
    return StepInput(
        kind="figure",
        figure_key=fig.key,
        fiscal_year=fig.fiscal_year,
        entity=fig.entity,
        pack_id=pack_id,
        value=fig.value if fig.value is not None else Decimal("0"),
    )


def _step_input(step: ComputationStep) -> StepInput:
    return StepInput(kind="step", step_key=step.step_key, value=step.result)


def _missing(line_key: str, year: int) -> str:
    return f"{line_key} (FY{year})"


def _not_computable(
    fig: ComputedFigure, missing: list[str], notes: str = ""
) -> ComputedFigure:
    fig.status = "not_computable"
    fig.value = None
    fig.missing_inputs = missing
    fig.steps = []
    if notes:
        fig.notes = notes
    return fig


def _propagated(fig: ComputedFigure, dep: ComputedFigure) -> ComputedFigure:
    return _not_computable(
        fig, [f"{dep.key}: {m}" for m in dep.missing_inputs]
    )


def _base(key: str, label: str, unit: str, year: int, entity: str) -> ComputedFigure:
    return ComputedFigure(
        key=key, label=label, unit=unit, fiscal_year=year, entity=entity,
        status="computed",
    )


def _finish(fig: ComputedFigure, steps: list[ComputationStep], notes: str = "") -> ComputedFigure:
    fig.steps = steps
    fig.value = steps[-1].result
    if notes:
        fig.notes = notes
    return fig


def _sum_step(key: str, label: str, inputs: list[StepInput]) -> ComputationStep:
    return ComputationStep(
        step_key=key, label=label, operation="sum", inputs=inputs,
        result=money(sum((i.value for i in inputs), Decimal("0"))),
    )


def _subtract_step(key: str, label: str, inputs: list[StepInput]) -> ComputationStep:
    result = inputs[0].value
    for i in inputs[1:]:
        result -= i.value
    return ComputationStep(
        step_key=key, label=label, operation="subtract", inputs=inputs,
        result=money(result),
    )


def _divide_step(key: str, label: str, num: StepInput, den: StepInput) -> ComputationStep:
    return ComputationStep(
        step_key=key, label=label, operation="divide", inputs=[num, den],
        result=ratio(num.value / den.value),
    )


def _carry_step(key: str, label: str, inp: StepInput) -> ComputationStep:
    return ComputationStep(
        step_key=key, label=label, operation="carry", inputs=[inp],
        result=inp.value,
    )


# ---------------------------------------------------------------------
# Per-period figures
# ---------------------------------------------------------------------


def _compute_year(ctx: _Ctx, year: int, prior_year: int | None) -> list[ComputedFigure]:
    entity = ctx.entity
    figures: dict[str, ComputedFigure] = {}
    ordered: list[ComputedFigure] = []

    def keep(fig: ComputedFigure) -> ComputedFigure:
        figures[fig.key] = fig
        ordered.append(fig)
        return fig

    # -- EBITDA -------------------------------------------------------
    fig = _base("ebitda", "EBITDA", "USD", year, entity)
    ebit_in = _spread_input(ctx, "ebit", year)
    da_in = _spread_input(ctx, "depreciation_and_amortization", year)
    missing = []
    if ebit_in is None:
        missing.append(_missing("ebit", year))
    if da_in is None:
        missing.append(_missing("depreciation_and_amortization", year))
    if missing:
        keep(_not_computable(fig, missing))
    else:
        s1 = _carry_step("operating_income", "Operating income (EBIT), as spread", ebit_in)
        s2 = ComputationStep(
            step_key="add_back_da",
            label="Add back depreciation & amortisation",
            operation="add",
            inputs=[_step_input(s1), da_in],
            result=money(s1.result + da_in.value),
        )
        adj_note = ""
        applied = ebit_in.adjustment_ids + da_in.adjustment_ids
        if applied:
            adj_note = f" R2 adjustments applied: {', '.join(applied)}."
        keep(_finish(fig, [s1, s2],
                     f"EBITDA = operating income + depreciation & amortisation, "
                     f"on the {ctx.view} view.{adj_note}"))
    ebitda = figures["ebitda"]

    # -- Funded debt ----------------------------------------------------
    fig = _base("total_debt", "Total debt (funded)", "USD", year, entity)
    cpltd_in = _spread_input(ctx, "current_portion_ltd", year)
    ltd_in = _spread_input(ctx, "long_term_debt", year)
    missing = []
    if cpltd_in is None:
        missing.append(_missing("current_portion_ltd", year))
    if ltd_in is None:
        missing.append(_missing("long_term_debt", year))
    if missing:
        keep(_not_computable(fig, missing))
    else:
        keep(_finish(fig, [_sum_step(
            "funded_debt", "Current portion of LTD + long-term debt",
            [cpltd_in, ltd_in])],
            "Funded debt as reported at the period's balance date."))
    total_debt = figures["total_debt"]

    # -- Debt service ---------------------------------------------------
    fig = _base("debt_service", "Debt service", "USD", year, entity)
    missing = []
    principal_in = None
    if prior_year is None:
        missing.append(
            f"scheduled principal: the spread has no period before FY{year} "
            "to read current_portion_ltd from"
        )
    else:
        principal_in = _spread_input(ctx, "current_portion_ltd", prior_year)
        if principal_in is None:
            missing.append(_missing("current_portion_ltd", prior_year))
    interest_in = _spread_input(ctx, "interest_expense", year)
    if interest_in is None:
        missing.append(_missing("interest_expense", year))
    if missing:
        keep(_not_computable(fig, missing))
    else:
        components = [principal_in, interest_in]
        lease_in = _supplemental_input(ctx, CAPITAL_LEASE_FIELD, year)
        if lease_in is not None:
            components.append(lease_in)
        note = (
            f"Debt service = scheduled principal (current_portion_ltd at the "
            f"end of FY{prior_year}) + interest expense (FY{year})"
        )
        note += (
            " + capital lease payments (supplemental figure)."
            if lease_in is not None
            else "; capital lease payments not reported, so none are included."
        )
        keep(_finish(fig, [_sum_step("debt_service", "Scheduled principal + interest"
                                     + (" + capital leases" if lease_in is not None else ""),
                                     components)], note))
    debt_service = figures["debt_service"]

    # -- Liquidity --------------------------------------------------------
    tca_in = _spread_input(ctx, "total_current_assets", year)
    tcl_in = _spread_input(ctx, "total_current_liabilities", year)

    fig = _base("working_capital", "Working capital", "USD", year, entity)
    missing = []
    if tca_in is None:
        missing.append(_missing("total_current_assets", year))
    if tcl_in is None:
        missing.append(_missing("total_current_liabilities", year))
    if missing:
        keep(_not_computable(fig, missing))
    else:
        keep(_finish(fig, [_subtract_step(
            "working_capital", "Total current assets − total current liabilities",
            [tca_in, tcl_in])]))

    fig = _base("current_ratio", "Current ratio", "ratio", year, entity)
    if tca_in is None or tcl_in is None:
        keep(_not_computable(fig, [
            _missing(k, year)
            for k, v in (("total_current_assets", tca_in),
                         ("total_current_liabilities", tcl_in)) if v is None
        ]))
    elif tcl_in.value == 0:
        keep(_not_computable(fig, [f"total_current_liabilities (FY{year}) is zero"]))
    else:
        keep(_finish(fig, [_divide_step(
            "current_ratio", "Total current assets / total current liabilities",
            tca_in, tcl_in)]))

    fig = _base("quick_ratio", "Quick ratio", "ratio", year, entity)
    cash_in = _spread_input(ctx, "cash_and_equivalents", year)
    ar_in = _spread_input(ctx, "accounts_receivable", year)
    missing = []
    if cash_in is None:
        missing.append(_missing("cash_and_equivalents", year))
    if ar_in is None:
        missing.append(_missing("accounts_receivable", year))
    if tcl_in is None:
        missing.append(_missing("total_current_liabilities", year))
    if missing:
        keep(_not_computable(fig, missing))
    elif tcl_in.value == 0:
        keep(_not_computable(fig, [f"total_current_liabilities (FY{year}) is zero"]))
    else:
        s1 = _sum_step("quick_assets", "Cash + accounts receivable", [cash_in, ar_in])
        keep(_finish(fig, [s1, _divide_step(
            "quick_ratio", "Quick assets / total current liabilities",
            _step_input(s1), tcl_in)],
            "Quick assets are cash and receivables only; inventory is excluded."))

    # -- Tangible net worth --------------------------------------------
    fig = _base("tangible_net_worth", "Tangible net worth", "USD", year, entity)
    equity_in = _spread_input(ctx, "total_equity", year)
    if equity_in is None:
        keep(_not_computable(fig, [_missing("total_equity", year)]))
    else:
        intangible_inputs: list[StepInput] = []
        deducted: list[str] = []
        for entry in ctx.spread.unmapped:
            if entry.field in INTANGIBLE_UNMAPPED_FIELDS and entry.values.get(year) is not None:
                provenance = [entry.sources[year]] if year in entry.sources else []
                intangible_inputs.append(StepInput(
                    kind="spread_line", line_key=entry.field, fiscal_year=year,
                    view=ctx.view, value=entry.values[year], provenance=provenance,
                ))
                deducted.append(entry.name)
        supp_intangibles = _supplemental_input(ctx, INTANGIBLE_FIELD, year)
        if supp_intangibles is not None:
            intangible_inputs.append(supp_intangibles)
            deducted.append("supplemental intangible assets figure")
        s1 = _carry_step("reported_equity", "Total equity, as spread", equity_in)
        if intangible_inputs:
            s2 = _subtract_step(
                "less_intangibles", "Equity − intangible assets",
                [_step_input(s1), *intangible_inputs])
            keep(_finish(fig, [s1, s2],
                         f"Intangibles deducted as reported: {', '.join(deducted)}."))
        else:
            keep(_finish(fig, [s1],
                         "No intangible assets are reported in the spread; "
                         "tangible net worth equals reported equity."))
    tnw = figures["tangible_net_worth"]

    # -- Leverage ---------------------------------------------------------
    fig = _base("debt_to_ebitda", "Total debt / EBITDA", "ratio", year, entity)
    if total_debt.status != "computed":
        keep(_propagated(fig, total_debt))
    elif ebitda.status != "computed":
        keep(_propagated(fig, ebitda))
    elif ebitda.value == 0:
        keep(_not_computable(fig, [f"ebitda (FY{year}) is zero"]))
    else:
        keep(_finish(fig, [_divide_step(
            "debt_to_ebitda", "Total debt / EBITDA",
            _figure_input(total_debt), _figure_input(ebitda))]))

    fig = _base("debt_to_tangible_net_worth", "Total liabilities / tangible net worth",
                "ratio", year, entity)
    tl_in = _spread_input(ctx, "total_liabilities", year)
    if tl_in is None:
        keep(_not_computable(fig, [_missing("total_liabilities", year)]))
    elif tnw.status != "computed":
        keep(_propagated(fig, tnw))
    elif tnw.value == 0:
        keep(_not_computable(fig, [f"tangible_net_worth (FY{year}) is zero"]))
    else:
        keep(_finish(fig, [_divide_step(
            "debt_to_tnw", "Total liabilities / tangible net worth",
            tl_in, _figure_input(tnw))],
            "The leverage covenant form: all liabilities, not funded debt "
            "alone, over tangible net worth."))

    # -- Coverage ---------------------------------------------------------
    fig = _base("dscr", "Debt service coverage ratio", "ratio", year, entity)
    if ebitda.status != "computed":
        keep(_propagated(fig, ebitda))
    elif debt_service.status != "computed":
        keep(_propagated(fig, debt_service))
    elif debt_service.value == 0:
        keep(_not_computable(fig, [f"debt_service (FY{year}) is zero"]))
    else:
        keep(_finish(fig, [_divide_step(
            "dscr", "EBITDA / debt service",
            _figure_input(ebitda), _figure_input(debt_service))]))

    fig = _base("fixed_charge_coverage", "Fixed-charge coverage", "ratio", year, entity)
    capex_in = _spread_input(ctx, "capital_expenditures", year)
    if ebitda.status != "computed":
        keep(_propagated(fig, ebitda))
    elif capex_in is None:
        keep(_not_computable(fig, [_missing("capital_expenditures", year)]))
    elif debt_service.status != "computed":
        keep(_propagated(fig, debt_service))
    elif debt_service.value == 0:
        keep(_not_computable(fig, [f"debt_service (FY{year}) is zero"]))
    else:
        subtractions = [_figure_input(ebitda), capex_in]
        named = ["capital expenditures"]
        for field, label in ((CASH_TAXES_FIELD, "cash income taxes"),
                             (DISTRIBUTIONS_FIELD, "owner distributions")):
            extra = _supplemental_input(ctx, field, year)
            if extra is not None:
                subtractions.append(extra)
                named.append(label)
        s1 = _subtract_step(
            "funds_for_fixed_charges",
            "EBITDA − " + " − ".join(named), subtractions)
        absent = [label for field, label in
                  ((CASH_TAXES_FIELD, "cash income taxes"),
                   (DISTRIBUTIONS_FIELD, "owner distributions"))
                  if _supplemental_input(ctx, field, year) is None]
        note = "Fixed charges are the period's debt service."
        if absent:
            note += f" Not reported, so not deducted: {', '.join(absent)}."
        keep(_finish(fig, [s1, _divide_step(
            "fixed_charge_coverage", "Funds available / debt service",
            _step_input(s1), _figure_input(debt_service))], note))
    fccr_done = figures["fixed_charge_coverage"]  # noqa: F841 - symmetry with peers

    # -- Profitability ------------------------------------------------------
    revenue_in = _spread_input(ctx, "revenue", year)

    def _margin(key: str, label: str, numerator: StepInput | None,
                numerator_missing: list[str]) -> ComputedFigure:
        fig = _base(key, label, "ratio", year, entity)
        if numerator is None:
            return _not_computable(fig, numerator_missing)
        if revenue_in is None:
            return _not_computable(fig, [_missing("revenue", year)])
        if revenue_in.value == 0:
            return _not_computable(fig, [f"revenue (FY{year}) is zero"])
        return _finish(fig, [_divide_step(key, f"{label.split(' (')[0]} / revenue",
                                          numerator, revenue_in)])

    gp_in = _spread_input(ctx, "gross_profit", year)
    keep(_margin("gross_margin", "Gross margin",
                 gp_in, [_missing("gross_profit", year)]))
    keep(_margin("operating_margin", "Operating margin",
                 ebit_in, [_missing("ebit", year)]))
    fig = _base("ebitda_margin", "EBITDA margin", "ratio", year, entity)
    if ebitda.status != "computed":
        keep(_propagated(fig, ebitda))
    elif revenue_in is None:
        keep(_not_computable(fig, [_missing("revenue", year)]))
    elif revenue_in.value == 0:
        keep(_not_computable(fig, [f"revenue (FY{year}) is zero"]))
    else:
        keep(_finish(fig, [_divide_step(
            "ebitda_margin", "EBITDA / revenue",
            _figure_input(ebitda), revenue_in)]))

    ni_in = _spread_input(ctx, "net_income", year)
    fig = _base("return_on_assets", "Return on assets", "ratio", year, entity)
    ta_in = _spread_input(ctx, "total_assets", year)
    if ni_in is None or ta_in is None:
        keep(_not_computable(fig, [
            _missing(k, year) for k, v in
            (("net_income", ni_in), ("total_assets", ta_in)) if v is None]))
    elif ta_in.value == 0:
        keep(_not_computable(fig, [f"total_assets (FY{year}) is zero"]))
    else:
        keep(_finish(fig, [_divide_step(
            "return_on_assets", "Net income / total assets (period-end)",
            ni_in, ta_in)]))

    fig = _base("return_on_equity", "Return on equity", "ratio", year, entity)
    if ni_in is None or equity_in is None:
        keep(_not_computable(fig, [
            _missing(k, year) for k, v in
            (("net_income", ni_in), ("total_equity", equity_in)) if v is None]))
    elif equity_in.value == 0:
        keep(_not_computable(fig, [f"total_equity (FY{year}) is zero"]))
    else:
        keep(_finish(fig, [_divide_step(
            "return_on_equity", "Net income / total equity (period-end)",
            ni_in, equity_in)]))

    # -- Cash flow -----------------------------------------------------------
    ocf_in = _spread_input(ctx, "net_cash_from_operations", year)
    fig = _base("operating_cash_flow", "Operating cash flow", "USD", year, entity)
    if ocf_in is None:
        keep(_not_computable(fig, [_missing("net_cash_from_operations", year)]))
    else:
        keep(_finish(fig, [_carry_step(
            "operating_cash_flow", "Net cash from operations, as spread", ocf_in)]))
    ocf = figures["operating_cash_flow"]

    fig = _base("free_cash_flow", "Free cash flow", "USD", year, entity)
    if ocf_in is None:
        keep(_not_computable(fig, [_missing("net_cash_from_operations", year)]))
    elif capex_in is None:
        keep(_not_computable(fig, [_missing("capital_expenditures", year)]))
    else:
        keep(_finish(fig, [_subtract_step(
            "free_cash_flow", "Operating cash flow − capital expenditures",
            [ocf_in, capex_in])]))

    fig = _base("cash_conversion", "Cash conversion", "ratio", year, entity)
    if ocf.status != "computed":
        keep(_propagated(fig, ocf))
    elif ebitda.status != "computed":
        keep(_propagated(fig, ebitda))
    elif ebitda.value == 0:
        keep(_not_computable(fig, [f"ebitda (FY{year}) is zero"]))
    else:
        keep(_finish(fig, [_divide_step(
            "cash_conversion", "Operating cash flow / EBITDA",
            _figure_input(ocf), _figure_input(ebitda))],
            "How much of EBITDA arrived as operating cash."))

    return ordered


# ---------------------------------------------------------------------
# Covenant headroom
# ---------------------------------------------------------------------

_OPERATORS = {
    ">=": lambda v, t: v >= t,
    ">": lambda v, t: v > t,
    "<=": lambda v, t: v <= t,
    "<": lambda v, t: v < t,
}


def evaluate_covenants(
    figures: list[ComputedFigure],
    definitions: list[CovenantDefinition],
    supplemental: list[ExtractedValue],
) -> list[CovenantResult]:
    """Headroom of the pack's figures against the given definitions.

    A definition whose metric is absent or not computable yields a
    not-computable result naming what is missing — headroom is never
    estimated. A definition that requires capital leases in debt
    service cannot be answered from a run that was handed no
    provenanced lease figure for the period.
    """
    by_key = {(f.key, f.fiscal_year): f for f in figures}
    lease_years = {
        period_year_of(v) for v in supplemental if v.field == CAPITAL_LEASE_FIELD
    }
    results: list[CovenantResult] = []
    for definition in sorted(definitions, key=lambda d: (d.name, d.fiscal_year)):
        result = CovenantResult(
            name=definition.name,
            metric_key=definition.metric_key,
            fiscal_year=definition.fiscal_year,
            operator=definition.operator,
            threshold=definition.threshold,
            status="not_computable",
        )
        if definition.require_capital_leases and definition.fiscal_year not in lease_years:
            result.missing_inputs = [
                f"capital_lease_payments (FY{definition.fiscal_year}) — required "
                f"by covenant definition {definition.name!r}"
            ]
            results.append(result)
            continue
        fig = by_key.get((definition.metric_key, definition.fiscal_year))
        if fig is None:
            result.missing_inputs = [
                f"metric {definition.metric_key!r} was not computed for "
                f"FY{definition.fiscal_year}"
            ]
        elif fig.status != "computed" or fig.value is None:
            result.missing_inputs = list(fig.missing_inputs)
        else:
            value = fig.value
            result.status = "computed"
            result.value = value
            result.compliant = _OPERATORS[definition.operator](value, definition.threshold)
            if definition.operator in (">=", ">"):
                headroom = value - definition.threshold
            else:
                headroom = definition.threshold - value
            result.headroom = money(headroom) if fig.unit == "USD" else ratio(headroom)
            if definition.threshold != 0:
                result.headroom_pct = ratio(headroom / abs(definition.threshold))
        results.append(result)
    return results


# ---------------------------------------------------------------------
# Pack assembly
# ---------------------------------------------------------------------


def _canonical_hash(descriptor: dict) -> str:
    blob = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _sorted_supplemental(supplemental: list[ExtractedValue]) -> list[ExtractedValue]:
    return sorted(
        supplemental,
        key=lambda v: (
            v.field,
            period_year_of(v),
            v.provenance.document_id,
            v.provenance.page,
            v.provenance.line or "",
            v.provenance.cell or "",
        ),
    )


def compute_entity_pack(
    *,
    credit_file_id: str,
    entity: str,
    spread: Spread,
    extractions: list[ExtractedValue] | tuple[ExtractedValue, ...] = (),
    supplemental: list[ExtractedValue] | tuple[ExtractedValue, ...] = (),
    covenant_definitions: list[CovenantDefinition] | tuple[CovenantDefinition, ...] = (),
    view: str = "adjusted",
) -> ComputationPack:
    """Compute every per-period figure for one entity over its spread.

    Pure and deterministic: the same spread, supplemental figures and
    definitions rebuild the identical pack, id included.
    """
    if view not in ("adjusted", "unadjusted"):
        raise ValidationError(f"computation view must be adjusted or unadjusted, not {view!r}")
    supplemental_sorted = _sorted_supplemental(list(supplemental))
    if supplemental_sorted:
        require_complete_provenance(supplemental_sorted)
    supp_index = {
        (v.field, period_year_of(v)): v for v in supplemental_sorted
    }
    ctx = _Ctx(
        spread=spread,
        view=view,
        entity=entity,
        prov=_provenance_index(spread, list(extractions)),
        supplemental=supp_index,
    )
    figures: list[ComputedFigure] = []
    periods = sorted(spread.periods)
    for idx, year in enumerate(periods):
        prior = periods[idx - 1] if idx > 0 else None
        figures.extend(_compute_year(ctx, year, prior))
    definitions = sorted(
        covenant_definitions, key=lambda d: (d.name, d.fiscal_year)
    )
    covenant_results = evaluate_covenants(figures, definitions, supplemental_sorted)
    inputs_hash = _canonical_hash({
        "engine_version": ENGINE_VERSION,
        "scope": "entity",
        "spread_id": spread.spread_id,
        "view": view,
        "supplemental": [v.model_dump(mode="json") for v in supplemental_sorted],
        "covenant_definitions": [d.model_dump(mode="json") for d in definitions],
    })
    return ComputationPack(
        pack_id=f"pack-{inputs_hash[:16]}",
        credit_file_id=credit_file_id,
        spread_id=spread.spread_id,
        entity=entity,
        scope="entity",
        engine_version=ENGINE_VERSION,
        view=view,
        inputs_hash=inputs_hash,
        entities=[entity],
        supplemental=supplemental_sorted,
        covenant_definitions=definitions,
        figures=figures,
        covenant_results=covenant_results,
    )


def compute_global_pack(
    *,
    credit_file_id: str,
    borrower_pack: ComputationPack,
    entity_packs: dict[str, ComputationPack] | None = None,
    eliminations: list[Elimination] | tuple[Elimination, ...] = (),
    expected_guarantors: list[str] | tuple[str, ...] = (),
    exclude_missing_guarantors: bool = False,
    covenant_definitions: list[CovenantDefinition] | tuple[CovenantDefinition, ...] = (),
) -> ComputationPack:
    """Combine a borrower with guarantor entity packs into the
    global cash-flow view: combined EBITDA and debt service, named
    eliminations applied, global DSCR.

    A guarantor named in `expected_guarantors` with no pack is a
    refusal — unless the caller explicitly opts into exclusion, in
    which case the pack carries an exclusion record naming them.
    Silence is not an option the function offers.
    """
    entity_packs = dict(entity_packs or {})
    for elimination in eliminations:
        require_valid_elimination(elimination)

    included: list[tuple[str, ComputationPack]] = [(borrower_pack.entity, borrower_pack)]
    exclusions: list[ExclusionRecord] = []
    for name in sorted(entity_packs):
        if name != borrower_pack.entity:
            included.append((name, entity_packs[name]))
    for guarantor in sorted(set(expected_guarantors)):
        if guarantor == borrower_pack.entity or guarantor in entity_packs:
            continue
        if not exclude_missing_guarantors:
            raise ValidationError(
                f"guarantor {guarantor!r} is named for the global view but "
                "has no spread and no computation pack; refusing to "
                "silently leave them out"
            )
        exclusions.append(ExclusionRecord(
            entity=guarantor,
            reason="named as a guarantor but no spread was provided; "
                   "excluded from the global view at the caller's "
                   "explicit instruction",
        ))

    eliminations_sorted = sorted(
        eliminations, key=lambda e: (e.name, e.fiscal_year, e.target)
    )
    figures: list[ComputedFigure] = []
    years = sorted({
        f.fiscal_year for f in borrower_pack.figures
        if f.key == "ebitda" and f.fiscal_year is not None
    })
    for year in years:
        figures.extend(_global_year(included, eliminations_sorted, year))

    all_supplemental: list[ExtractedValue] = []
    for _, pack in included:
        all_supplemental.extend(pack.supplemental)
    definitions = sorted(
        covenant_definitions, key=lambda d: (d.name, d.fiscal_year)
    )
    covenant_results = evaluate_covenants(
        figures, definitions, _sorted_supplemental(all_supplemental)
    )
    entity_pack_ids = [pack.pack_id for _, pack in included]
    inputs_hash = _canonical_hash({
        "engine_version": ENGINE_VERSION,
        "scope": "global",
        "entity_pack_ids": sorted(entity_pack_ids),
        "eliminations": [e.model_dump(mode="json") for e in eliminations_sorted],
        "exclusions": [e.model_dump(mode="json") for e in exclusions],
        "expected_guarantors": sorted(expected_guarantors),
        "exclude_missing_guarantors": exclude_missing_guarantors,
        "covenant_definitions": [d.model_dump(mode="json") for d in definitions],
    })
    return ComputationPack(
        pack_id=f"pack-{inputs_hash[:16]}",
        credit_file_id=credit_file_id,
        spread_id=borrower_pack.spread_id,
        entity="global",
        scope="global",
        engine_version=ENGINE_VERSION,
        view=borrower_pack.view,
        inputs_hash=inputs_hash,
        entities=[name for name, _ in included],
        entity_pack_ids=entity_pack_ids,
        supplemental=_sorted_supplemental(all_supplemental),
        covenant_definitions=definitions,
        eliminations=eliminations_sorted,
        exclusions=exclusions,
        figures=figures,
        covenant_results=covenant_results,
    )


def _entity_figure(pack: ComputationPack, key: str, year: int) -> ComputedFigure | None:
    for f in pack.figures:
        if f.key == key and f.fiscal_year == year:
            return f
    return None


def _global_year(
    included: list[tuple[str, ComputationPack]],
    eliminations: list[Elimination],
    year: int,
) -> list[ComputedFigure]:
    out: list[ComputedFigure] = []

    def combined(key: str, label: str, target: str,
                 figures_key: str) -> ComputedFigure:
        fig = _base(figures_key, label, "USD", year, "global")
        inputs: list[StepInput] = []
        missing: list[str] = []
        for entity_name, pack in included:
            dep = _entity_figure(pack, key, year)
            if dep is None or dep.status != "computed" or dep.value is None:
                missing.append(
                    f"{entity_name}: {key} (FY{year}) "
                    + ("not computed" if dep is None else "not computable")
                )
            else:
                inputs.append(_figure_input(dep, pack_id=pack.pack_id))
        if missing:
            return _not_computable(fig, missing)
        steps = [_sum_step(f"combined_{key}", f"Combined {key.replace('_', ' ')}", inputs)]
        for elim in eliminations:
            if elim.fiscal_year == year and elim.target == target:
                steps.append(_subtract_step(
                    f"less_{elim.name}",
                    f"Less elimination: {elim.name}",
                    [_step_input(steps[-1]),
                     StepInput(kind="elimination", elimination_name=elim.name,
                               fiscal_year=year, value=elim.amount)],
                ))
        return _finish(fig, steps,
                       "Combined across: "
                       + ", ".join(name for name, _ in included) + ".")

    cash_flow = combined("ebitda", "Global cash flow (combined EBITDA, "
                                   "eliminations applied)", "cash_flow",
                         "global_cash_flow")
    out.append(cash_flow)
    debt_service = combined("debt_service", "Global debt service (combined, "
                                            "eliminations applied)", "debt_service",
                            "global_debt_service")
    out.append(debt_service)

    fig = _base("global_dscr", "Global DSCR", "ratio", year, "global")
    if cash_flow.status != "computed":
        out.append(_propagated(fig, cash_flow))
    elif debt_service.status != "computed":
        out.append(_propagated(fig, debt_service))
    elif debt_service.value == 0:
        out.append(_not_computable(fig, [f"global_debt_service (FY{year}) is zero"]))
    else:
        out.append(_finish(fig, [_divide_step(
            "global_dscr", "Global cash flow / global debt service",
            _figure_input(cash_flow), _figure_input(debt_service))]))
    return out


# ---------------------------------------------------------------------
# Trace verification
# ---------------------------------------------------------------------


@dataclass
class TraceReport:
    figures_checked: int = 0
    figures_complete: int = 0
    gaps: list[str] = dc_field(default_factory=list)

    @property
    def completeness(self) -> float:
        if self.figures_checked == 0:
            return 1.0
        return self.figures_complete / self.figures_checked


def verify_trace(
    pack: ComputationPack,
    *,
    spread: Spread | None = None,
    packs: dict[str, ComputationPack] | None = None,
) -> TraceReport:
    """Walk every computed figure's derivation to its leaves.

    A figure's trace is complete when every step input resolves:
    spread-line inputs carry a provenance chain (and, when the
    spread is supplied, the value recorded matches the cell in the
    named view); supplemental inputs carry provenance and are
    recorded on the pack; figure and step inputs resolve and
    recurse; elimination inputs name an elimination recorded on the
    pack, reason stated. Each step's arithmetic is also re-performed
    from its inputs and compared with the recorded result.
    """
    report = TraceReport()
    registry: dict[str, ComputationPack] = {pack.pack_id: pack}
    if packs:
        registry.update(packs)
    spread_lines = (
        {line.key: line for line in spread.lines} if spread is not None else {}
    )

    def resolve_figure(owner: ComputationPack, inp: StepInput) -> ComputedFigure | None:
        target_pack = registry.get(inp.pack_id or owner.pack_id)
        if target_pack is None:
            return None
        for f in target_pack.figures:
            if (
                f.key == inp.figure_key
                and f.fiscal_year == inp.fiscal_year
                and (inp.entity is None or f.entity == inp.entity)
            ):
                return f
        return None

    def check_figure(owner: ComputationPack, fig: ComputedFigure,
                     stack: frozenset) -> list[str]:
        gaps: list[str] = []
        ident = (owner.pack_id, fig.key, fig.fiscal_year, fig.entity)
        if ident in stack:
            return [f"{fig.key} FY{fig.fiscal_year}: derivation cycle"]
        stack = stack | {ident}
        steps_by_key = {s.step_key: s for s in fig.steps}
        for step in fig.steps:
            raw = _raw_result(step)
            if raw is not None and step.result not in (raw, money(raw), ratio(raw)):
                gaps.append(
                    f"{fig.key} FY{fig.fiscal_year} step {step.step_key}: "
                    f"recorded result {step.result} does not re-perform "
                    f"from its inputs ({raw})"
                )
            for inp in step.inputs:
                gaps.extend(check_input(owner, fig, steps_by_key, inp, stack))
        return gaps

    def check_input(owner: ComputationPack, fig: ComputedFigure,
                    steps_by_key: dict[str, ComputationStep],
                    inp: StepInput, stack: frozenset) -> list[str]:
        where = f"{fig.key} FY{fig.fiscal_year}"
        if inp.kind == "spread_line":
            if not inp.provenance:
                return [f"{where}: input {inp.line_key} FY{inp.fiscal_year} "
                        "has no provenance chain"]
            line = spread_lines.get(inp.line_key or "")
            if line is not None:
                values = (
                    line.adjusted_values if inp.view == "adjusted" else line.values
                )
                if values.get(inp.fiscal_year) != inp.value:
                    return [f"{where}: input {inp.line_key} FY{inp.fiscal_year} "
                            "value does not match the spread cell"]
            return []
        if inp.kind == "supplemental":
            if not inp.provenance:
                return [f"{where}: supplemental input {inp.line_key} "
                        "has no provenance"]
            recorded = any(
                v.field == inp.line_key and period_year_of(v) == inp.fiscal_year
                and v.value == inp.value
                for v in owner.supplemental
            )
            if not recorded:
                return [f"{where}: supplemental input {inp.line_key} "
                        "is not recorded on the pack"]
            return []
        if inp.kind == "figure":
            target = resolve_figure(owner, inp)
            if target is None:
                return [f"{where}: figure input {inp.figure_key} does not resolve"]
            if target.value != inp.value:
                return [f"{where}: figure input {inp.figure_key} value does "
                        "not match the figure it names"]
            target_pack = registry.get(inp.pack_id or owner.pack_id)
            return check_figure(target_pack or owner, target, stack)
        if inp.kind == "step":
            target_step = steps_by_key.get(inp.step_key or "")
            if target_step is None:
                return [f"{where}: step input {inp.step_key} does not resolve"]
            if target_step.result != inp.value:
                return [f"{where}: step input {inp.step_key} value mismatch"]
            gaps: list[str] = []
            for sub in target_step.inputs:
                gaps.extend(check_input(owner, fig, steps_by_key, sub, stack))
            return gaps
        if inp.kind == "elimination":
            match = next(
                (e for e in owner.eliminations if e.name == inp.elimination_name),
                None,
            )
            if match is None:
                return [f"{where}: elimination {inp.elimination_name} "
                        "is not recorded on the pack"]
            if not match.reason or not match.reason.strip():
                return [f"{where}: elimination {inp.elimination_name} "
                        "has no stated reason"]
            if match.amount != inp.value:
                return [f"{where}: elimination {inp.elimination_name} "
                        "amount mismatch"]
            return []
        return [f"{where}: unknown input kind {inp.kind!r}"]

    for fig in pack.figures:
        if fig.status != "computed":
            continue
        report.figures_checked += 1
        gaps = check_figure(pack, fig, frozenset())
        if gaps:
            report.gaps.extend(gaps)
        else:
            report.figures_complete += 1
    return report


def _raw_result(step: ComputationStep) -> Decimal | None:
    """The unquantized result of a step's operation over its inputs,
    or None when the operation has no re-performable meaning."""
    if not step.inputs:
        return None
    if step.operation == "carry":
        return step.inputs[0].value
    if step.operation in ("add", "sum"):
        return sum((i.value for i in step.inputs), Decimal("0"))
    if step.operation == "subtract":
        result = step.inputs[0].value
        for i in step.inputs[1:]:
            result -= i.value
        return result
    if step.operation == "divide":
        if len(step.inputs) != 2 or step.inputs[1].value == 0:
            return None
        return step.inputs[0].value / step.inputs[1].value
    return None
