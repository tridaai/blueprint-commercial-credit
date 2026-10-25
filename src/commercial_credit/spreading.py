"""The spreading engine: extractions in, the bank's template out.

Spreading is the deterministic heart of the credit workflow. The
template below is the bank's standard taxonomy — balance sheet,
income statement, cash-flow statement — expressed as data: each line
names the extraction field it is fed from. The builder maps every
extracted value onto its template line; a value whose field the
template does not map is carried as a named unmapped entry, never
dropped and never folded into a line it does not belong to.

The builder is a pure function of (credit file, extractions,
adjustments). It performs no I/O and stamps nothing time- or
random-based into the result: the same inputs rebuild the same
spread, byte for byte, with the same content-derived spread id.
Monetary values are Decimal end to end.

What the builder deliberately does not do: compute ratios or run
policy tests (rounds 3 and 4). Disagreements between documents are
round 8, and the builder's rule for them is the one design law 5
states: within one source type and one period, the latest version
wins whole — a restated audited statement supersedes the earlier
audited statement, and the spread says which version it rests on.
Across source types nothing supersedes: the spread works from the
highest-precedence source that reports each line (audited, then
tax, then management — disagreements.py), and where the sources
differ beyond the disagreement policy's tolerance the difference
is recorded on the spread as a Disagreement, every source's value
carried with its provenance. Nothing is averaged, ever.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

from .disagreements import (
    DISAGREEMENT_POLICY,
    SOURCE_AUDITED,
    detect_disagreement,
    precedence_rank,
    source_type_for_document,
)
from .errors import ValidationError
from .models import (
    CreditFile,
    Disagreement,
    ExtractedValue,
    PeriodSource,
    Spread,
    SpreadAdjustment,
    SpreadLine,
    UnmappedEntry,
    require_valid_adjustment,
)

TEMPLATE_VERSION = "aldergrove-spread-v1"

SECTION_BALANCE_SHEET = "balance_sheet"
SECTION_INCOME_STATEMENT = "income_statement"
SECTION_CASH_FLOW_STATEMENT = "cash_flow_statement"


@dataclass(frozen=True)
class TemplateLine:
    key: str
    label: str
    section: str
    field: str  # canonical extraction field that feeds this line


def _line(key: str, label: str, section: str, field: str) -> TemplateLine:
    return TemplateLine(key=key, label=label, section=section, field=field)


# The Aldergrove spreading taxonomy. Line order is presentation order.
TEMPLATE_LINES: tuple[TemplateLine, ...] = (
    # Balance sheet
    _line("cash_and_equivalents", "Cash and equivalents", SECTION_BALANCE_SHEET, "cash_and_equivalents"),
    _line("accounts_receivable", "Accounts receivable", SECTION_BALANCE_SHEET, "accounts_receivable"),
    _line("inventory", "Inventory", SECTION_BALANCE_SHEET, "inventory"),
    _line("total_current_assets", "Total current assets", SECTION_BALANCE_SHEET, "total_current_assets"),
    _line("net_fixed_assets", "Net fixed assets", SECTION_BALANCE_SHEET, "net_fixed_assets"),
    _line("total_assets", "Total assets", SECTION_BALANCE_SHEET, "total_assets"),
    _line("accounts_payable", "Accounts payable", SECTION_BALANCE_SHEET, "accounts_payable"),
    _line("current_portion_ltd", "Current portion of long-term debt", SECTION_BALANCE_SHEET, "current_portion_ltd"),
    _line("total_current_liabilities", "Total current liabilities", SECTION_BALANCE_SHEET, "total_current_liabilities"),
    _line("long_term_debt", "Long-term debt", SECTION_BALANCE_SHEET, "long_term_debt"),
    _line("total_liabilities", "Total liabilities", SECTION_BALANCE_SHEET, "total_liabilities"),
    _line("total_equity", "Total equity", SECTION_BALANCE_SHEET, "total_equity"),
    # Income statement
    _line("revenue", "Revenue", SECTION_INCOME_STATEMENT, "revenue"),
    _line("cost_of_goods_sold", "Cost of goods sold", SECTION_INCOME_STATEMENT, "cost_of_goods_sold"),
    _line("gross_profit", "Gross profit", SECTION_INCOME_STATEMENT, "gross_profit"),
    _line("operating_expenses", "Operating expenses", SECTION_INCOME_STATEMENT, "operating_expenses"),
    _line("ebit", "EBIT", SECTION_INCOME_STATEMENT, "ebit"),
    _line("interest_expense", "Interest expense", SECTION_INCOME_STATEMENT, "interest_expense"),
    _line("net_income", "Net income", SECTION_INCOME_STATEMENT, "net_income"),
    # Cash-flow statement
    _line("depreciation_and_amortization", "Depreciation and amortization", SECTION_CASH_FLOW_STATEMENT, "depreciation_and_amortization"),
    _line("change_in_receivables", "Change in accounts receivable", SECTION_CASH_FLOW_STATEMENT, "change_in_receivables"),
    _line("change_in_inventory", "Change in inventory", SECTION_CASH_FLOW_STATEMENT, "change_in_inventory"),
    _line("change_in_payables", "Change in accounts payable", SECTION_CASH_FLOW_STATEMENT, "change_in_payables"),
    _line("net_cash_from_operations", "Net cash provided by operating activities", SECTION_CASH_FLOW_STATEMENT, "net_cash_from_operations"),
    _line("capital_expenditures", "Capital expenditures", SECTION_CASH_FLOW_STATEMENT, "capital_expenditures"),
    _line("net_cash_from_investing", "Net cash used in investing activities", SECTION_CASH_FLOW_STATEMENT, "net_cash_from_investing"),
    _line("debt_proceeds", "Proceeds from borrowings", SECTION_CASH_FLOW_STATEMENT, "debt_proceeds"),
    _line("debt_repayments", "Repayments of borrowings", SECTION_CASH_FLOW_STATEMENT, "debt_repayments"),
    _line("net_cash_from_financing", "Net cash from financing activities", SECTION_CASH_FLOW_STATEMENT, "net_cash_from_financing"),
    _line("net_change_in_cash", "Net change in cash", SECTION_CASH_FLOW_STATEMENT, "net_change_in_cash"),
)

TEMPLATE_BY_FIELD: dict[str, TemplateLine] = {t.field: t for t in TEMPLATE_LINES}
TEMPLATE_BY_KEY: dict[str, TemplateLine] = {t.key: t for t in TEMPLATE_LINES}

_YEAR_IN_LABEL = re.compile(r"(?:FY)?\s*((?:19|20)\d{2})")


def period_year_of(value: ExtractedValue) -> int:
    """The fiscal year an extracted value belongs to.

    Falls back to the year named in the period label; a value that
    can be placed in no period cannot join a multi-period spread, and
    the builder refuses it loudly instead of dropping it quietly.
    """
    if value.fiscal_year is not None:
        return value.fiscal_year
    match = _YEAR_IN_LABEL.search(value.period_label or "")
    if match:
        return int(match.group(1))
    raise ValidationError(
        f"extracted value {value.field!r} has no fiscal year and its "
        f"period label {value.period_label!r} names none"
    )


def validate_adjustment(adjustment: SpreadAdjustment, credit_file_id: str) -> None:
    """Full receiving-side validation for one adjustment: intrinsic
    validity (models.require_valid_adjustment), the right credit
    file, and a spread line the taxonomy actually has."""
    require_valid_adjustment(adjustment)
    if adjustment.credit_file_id != credit_file_id:
        raise ValidationError(
            f"adjustment {adjustment.adjustment_id} belongs to credit file "
            f"{adjustment.credit_file_id}, not {credit_file_id}"
        )
    if adjustment.line_key not in TEMPLATE_BY_KEY:
        raise ValidationError(
            f"adjustment {adjustment.adjustment_id} names spread line "
            f"{adjustment.line_key!r}, which is not in template {TEMPLATE_VERSION}"
        )


def build_spread(
    credit_file: CreditFile,
    extractions: list[ExtractedValue],
    adjustments: list[SpreadAdjustment],
) -> Spread:
    """Build the spread for a credit file from its extractions and
    adjustments. Pure and deterministic: inputs are canonically
    sorted before use, so input ordering cannot change the output."""
    for adjustment in adjustments:
        validate_adjustment(adjustment, credit_file.credit_file_id)

    # Document registration order on the credit file is the version
    # order: a statement filed later for a period supersedes the one
    # filed earlier. Documents unknown to the file sort after, in
    # first-seen order, so the rule stays total and deterministic.
    doc_order: dict[str, int] = {
        doc.document_id: idx for idx, doc in enumerate(credit_file.documents)
    }

    def order_of(document_id: str) -> int:
        if document_id not in doc_order:
            doc_order[document_id] = len(doc_order)
        return doc_order[document_id]

    ordered = sorted(
        extractions,
        key=lambda v: (
            period_year_of(v),
            order_of(v.provenance.document_id),
            v.field,
            v.provenance.page,
            v.provenance.line or "",
            v.provenance.cell or "",
        ),
    )

    periods = sorted({period_year_of(v) for v in ordered})

    # -- source types ---------------------------------------------------
    # Every value belongs to a source type: the extractor stamps it,
    # and a value that arrives unstamped takes its type from the
    # document record on the credit file (an unknown document is
    # treated as a statement — the pipeline's producers all stamp).
    def source_type_of(v: ExtractedValue) -> str:
        if v.source_type:
            return v.source_type
        doc = credit_file.document(v.provenance.document_id)
        if doc is not None:
            return source_type_for_document(doc.kind.value)
        return SOURCE_AUDITED

    # -- which document version each (period, source type) rests on -----
    # Supersession is per source type: the latest version OF THAT
    # TYPE filed for the period is its current version. A tax return
    # filed after the audited statements supersedes nothing — the
    # two stand side by side, and their differences are judged
    # below as disagreements.
    docs_by_period_type: dict[tuple[int, str], list[str]] = {}
    for v in ordered:
        key = (period_year_of(v), source_type_of(v))
        doc_ids = docs_by_period_type.setdefault(key, [])
        if v.provenance.document_id not in doc_ids:
            doc_ids.append(v.provenance.document_id)
    current_doc: dict[tuple[int, str], str] = {
        key: doc_ids[-1] for key, doc_ids in docs_by_period_type.items()
    }

    period_sources: list[PeriodSource] = []
    for year in periods:
        types_present = sorted(
            (t for (y, t) in docs_by_period_type if y == year),
            key=lambda t: (precedence_rank(t), t),
        )
        for source_type in types_present:
            doc_ids = docs_by_period_type[(year, source_type)]
            current_id = doc_ids[-1]
            source_doc = credit_file.document(current_id)
            label = next(
                v.period_label
                for v in ordered
                if period_year_of(v) == year
                and source_type_of(v) == source_type
                and v.provenance.document_id == current_id
            )
            period_sources.append(
                PeriodSource(
                    fiscal_year=year,
                    period_label=label,
                    document_id=current_id,
                    document_sha256=source_doc.sha256 if source_doc else "",
                    version=len(doc_ids),
                    superseded_document_ids=doc_ids[:-1],
                    source_type=source_type,
                )
            )

    # -- the reported figure per (field, period, source type) -----------
    # Only each type's CURRENT version reports. Within it, the
    # first occurrence of a line stands (ordered runs page/line
    # within the document) — a repeated line with a different value
    # inside one document is that document contradicting itself,
    # not a disagreement between sources, and is not averaged.
    reported: dict[tuple[str, int, str], ExtractedValue] = {}
    for v in ordered:
        year = period_year_of(v)
        source_type = source_type_of(v)
        if v.provenance.document_id != current_doc[(year, source_type)]:
            continue  # a superseded version of its own type
        reported.setdefault((v.field, year, source_type), v)

    source_types_present = sorted(
        {t for (_, _, t) in reported},
        key=lambda t: (precedence_rank(t), t),
    )

    def working_value(field: str, year: int) -> ExtractedValue | None:
        """The figure the spread works from for one line and
        period: the highest-precedence source type that reports
        it. The other reporters are not discarded — they are the
        disagreement record below."""
        for source_type in source_types_present:
            value = reported.get((field, year, source_type))
            if value is not None:
                return value
        return None

    adjustments_by_cell: dict[tuple[str, int], Decimal] = {}
    for adjustment in adjustments:
        key = (adjustment.line_key, adjustment.fiscal_year)
        adjustments_by_cell[key] = adjustments_by_cell.get(key, Decimal("0")) + adjustment.amount

    # -- template lines ------------------------------------------------
    lines: list[SpreadLine] = []
    for template in TEMPLATE_LINES:
        raw: dict[int, Decimal | None] = {year: None for year in periods}
        for year in periods:
            winner = working_value(template.field, year)
            if winner is not None:
                raw[year] = winner.value
        adjusted: dict[int, Decimal | None] = {}
        changes: dict[int, Decimal | None] = {}
        for idx, year in enumerate(periods):
            base = raw[year]
            delta = adjustments_by_cell.get((template.key, year))
            if base is None and delta is None:
                adjusted[year] = None
            else:
                adjusted[year] = (base if base is not None else Decimal("0")) + (
                    delta if delta is not None else Decimal("0")
                )
            if idx == 0:
                changes[year] = None
            else:
                prior = raw[periods[idx - 1]]
                changes[year] = (
                    base - prior if base is not None and prior is not None else None
                )
        lines.append(
            SpreadLine(
                key=template.key,
                label=template.label,
                section=template.section,
                values=raw,
                adjusted_values=adjusted,
                changes=changes,
            )
        )

    # -- unmapped entries ----------------------------------------------
    # The same working-value rule as the template lines: per field
    # and period, the highest-precedence reporter's figure, with
    # its provenance. Other reporters live in the disagreements.
    unmapped_values: dict[str, dict[int, Decimal]] = {}
    unmapped_sources: dict[str, dict[int, ExtractedValue]] = {}
    unmapped_names: dict[str, str] = {}
    for (field, year, _type), v in reported.items():
        if field in TEMPLATE_BY_FIELD:
            continue  # a template line, spread above
        if working_value(field, year) is not v:
            continue
        unmapped_values.setdefault(field, {})[year] = v.value
        unmapped_sources.setdefault(field, {})[year] = v
        unmapped_names.setdefault(
            field, v.source_label or field.removeprefix(UNMAPPED_FIELD_PREFIX)
        )
    unmapped = [
        UnmappedEntry(
            field=field,
            name=unmapped_names[field],
            values={year: unmapped_values[field].get(year) for year in periods},
            sources={
                year: unmapped_sources[field][year].provenance
                for year in periods
                if year in unmapped_sources[field]
            },
        )
        for field in sorted(unmapped_values)
    ]

    canonical_adjustments = sorted(
        adjustments, key=lambda a: (a.line_key, a.fiscal_year, a.adjustment_id)
    )

    # -- disagreements between source types ------------------------------
    # Every (line, period) that more than one source type reports is
    # judged under the disagreement policy. A record is created
    # only where a source differs from the working figure beyond
    # tolerance — and the record carries every source's figure, so
    # the spread can never be read as if the working value were the
    # only value anyone reported.
    disagreements: list[Disagreement] = []
    for year in periods:
        fields = sorted({f for (f, y, _t) in reported if y == year})
        for field in fields:
            reporters = [
                (t, reported[(field, year, t)]) for t in source_types_present
                if (field, year, t) in reported
            ]
            if len(reporters) < 2:
                continue
            template = TEMPLATE_BY_FIELD.get(field)
            if template is not None:
                label = template.label
            else:
                label = unmapped_names.get(
                    field, field.removeprefix(UNMAPPED_FIELD_PREFIX)
                )
            record = detect_disagreement(
                credit_file_id=credit_file.credit_file_id,
                field=field,
                line_label=label,
                fiscal_year=year,
                reported=[
                    (
                        source_type,
                        value,
                        (
                            credit_file.document(value.provenance.document_id).sha256
                            if credit_file.document(value.provenance.document_id)
                            else ""
                        ),
                    )
                    for source_type, value in reporters
                ],
                policy=DISAGREEMENT_POLICY,
            )
            if record is not None:
                disagreements.append(record)
    disagreements.sort(key=lambda d: (d.fiscal_year, d.line_key))

    spread = Spread(
        spread_id="",
        credit_file_id=credit_file.credit_file_id,
        template_version=TEMPLATE_VERSION,
        periods=periods,
        period_sources=period_sources,
        lines=lines,
        unmapped=unmapped,
        adjustments=canonical_adjustments,
        disagreements=disagreements,
        disagreement_policy_version=DISAGREEMENT_POLICY.version,
    )
    digest = hashlib.sha256(
        spread.model_dump_json(exclude={"spread_id"}).encode("utf-8")
    ).hexdigest()
    spread.spread_id = f"spread-{digest[:16]}"
    return spread
