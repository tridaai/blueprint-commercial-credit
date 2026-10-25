"""Memo drafting and the figure verifier (design law 1, enforced).

The memo is where every earlier artefact becomes prose a credit
officer reads — and prose is where figures go to escape their
provenance. This round's centrepiece is the guard against that:
the FIGURE VERIFIER. A memo is composed from the pinned artefacts
(credit file, spread, computation pack, policy report), rendered to
its final text, and then — as code, over the rendered text, not
over the composer's intentions — every numeric figure in the text
and tables is extracted and traced. A figure traces when it equals
a figure in the pinned computation pack, a spread line value, a
policy threshold or outcome value, or a stated fact of the credit
file (amounts, counts, dates as recorded). A memo with even one
untraceable figure is REFUSED: it is never stored as a draft, and
the refusal names the offending figures. The traces that cleared
are stored with the memo as its verification record.

Policy exceptions carry the same discipline structurally: the
exceptions section must account for exactly the pinned report's
non-pass outcomes — every fail and every not_evaluable, by test
id. A draft that omits a blocker fail, or softens a not_evaluable
into silence, cannot be stored either.

Document disagreements (round 8) carry it the same way again: when
the pinned spread records disagreements between its source types,
the memo must carry a disagreements section accounting for every
MATERIAL one, by id, stated in the record's own terms — each
source's figure and the distance between them. A draft that drops
the section, omits a material disagreement, or rewords a material
difference into smaller language cannot be stored. Figures
computed over a materially disagreed line carry the disagreement's
flag, and their traces say so.

Drafting goes through a composer port with named profiles. The
default profile, `offline`, is deterministic: it assembles the
narrative from the structured artefacts and formats every figure
through the helpers below, inventing nothing. A `live` profile is
defined behind the same port for a model-backed drafter; it is not
configured in this round (no provider key is wired), and selecting
it fails loudly rather than silently falling back. Whichever
profile drafts, the verifier — not the drafter — decides whether
the memo may exist.

The memo states facts and policy outcomes. It does not recommend:
the recommendation and conditions sections are placeholders the
credit officer completes at the gate (round 6).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Protocol

from .disagreements import SOURCE_TYPE_LABELS, get_disagreement_policy
from .errors import MemoVerificationError, ValidationError
from .models import (
    ComputedFigure,
    ComputationPack,
    CreditFile,
    CreditMemo,
    Disagreement,
    FigureTrace,
    MemoSection,
    MemoTable,
    MemoVerification,
    PolicyReport,
    PolicyTestOutcome,
    Spread,
)
from .policy import get_policy_pack

# ---------------------------------------------------------------------
# Figure formatting — the only shapes a figure may take in memo text.
# The verifier's extractor recognises exactly these shapes (plus
# bare counts), so a composer that formats through these helpers
# produces text the verifier can read back losslessly.
# ---------------------------------------------------------------------

_CENT = Decimal("0.01")


def fmt_money(value: Decimal) -> str:
    q = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    return f"{sign}${abs(q):,.2f}"


def fmt_ratio(value: Decimal) -> str:
    return f"{format(value, 'f')}×"


def fmt_percent(value: Decimal) -> str:
    return f"{format(value, 'f')}%"


def fmt_figure(figure: ComputedFigure) -> str:
    if figure.value is None:
        return "not computable"
    return fmt_money(figure.value) if figure.unit == "USD" else fmt_ratio(figure.value)


# ---------------------------------------------------------------------
# Figure extraction — over the final rendered text.
#
# Machine identifiers (artefact ids, pack versions) are rendered in
# `code spans` and are not figures; ISO dates, fiscal-year tags
# (FY2024) and policy section references (§4.2) are not figures
# either. Everything else numeric — $-amounts, ratios (×), percents
# and bare numbers — is a figure and must trace.
# ---------------------------------------------------------------------

_CODE_SPAN_RE = re.compile(r"`[^`]*`")
_MONEY_RE = re.compile(r"-?\$(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_RATIO_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?×")
_PERCENT_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?%")
_BARE_RE = re.compile(
    r"(?<![\w$.,§/:\-])-?\d[\d,]*(?:\.\d+)?(?![\d×%.,/\w\-])"
)


@dataclass(frozen=True)
class ExtractedFigure:
    text: str  # exactly as it appears in the rendered text
    value: Decimal
    kind: str  # amount | ratio | percent | number


def _parse_number(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


_PATTERNS: list[tuple[re.Pattern[str], str, object]] = [
    (_MONEY_RE, "amount", lambda s: _parse_number(s.replace("$", ""))),
    (_RATIO_RE, "ratio", lambda s: _parse_number(s.rstrip("×"))),
    (_PERCENT_RE, "percent", lambda s: _parse_number(s.rstrip("%"))),
    (_BARE_RE, "number", _parse_number),
]


def extract_figures(text: str) -> list[ExtractedFigure]:
    """Every figure in a rendered text, in reading order."""
    working = _CODE_SPAN_RE.sub(lambda m: " " * len(m.group(0)), text)
    found: list[tuple[int, ExtractedFigure]] = []
    for pattern, kind, parse in _PATTERNS:
        for match in pattern.finditer(working):
            found.append((
                match.start(),
                ExtractedFigure(
                    text=match.group(0),
                    value=parse(match.group(0)),  # type: ignore[operator]
                    kind=kind,
                ),
            ))
        working = pattern.sub(lambda m: " " * len(m.group(0)), working)
    found.sort(key=lambda item: item[0])
    return [figure for _, figure in found]


# ---------------------------------------------------------------------
# The trace universe: every value a memo on these inputs may state,
# each mapped to the artefact + field it traces to. A rendered
# figure that is not a key of this mapping does not trace, full stop.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class MemoInputs:
    credit_file: CreditFile
    spread: Spread
    pack: ComputationPack
    policy_report: PolicyReport
    global_pack: ComputationPack | None = None


def build_trace_universe(inputs: MemoInputs) -> dict[Decimal, str]:
    universe: dict[Decimal, str] = {}

    def add(value: Decimal | None, source: str) -> None:
        if value is not None:
            universe.setdefault(value, source)

    credit_file, spread = inputs.credit_file, inputs.spread
    report = inputs.policy_report

    # Computation packs: figures, covenant results, supplemental inputs.
    for pack, pack_label in (
        (inputs.pack, "computation pack"),
        (inputs.global_pack, "global computation pack"),
    ):
        if pack is None:
            continue
        for figure in pack.figures:
            if figure.status == "computed" and figure.value is not None:
                where = (
                    f"FY{figure.fiscal_year}"
                    if figure.fiscal_year is not None
                    else "combined"
                )
                source = (
                    f"{pack_label} {pack.pack_id}, figure {figure.key} "
                    f"({figure.label}), {where}"
                )
                if figure.disagreement_ids:
                    # Round 8: a figure computed over a materially
                    # disagreed line never traces as a clean figure —
                    # its trace names the disagreement(s) it rests on.
                    source += (
                        "; computed over document disagreement(s) "
                        + ", ".join(figure.disagreement_ids)
                    )
                add(figure.value, source)
        for result in pack.covenant_results:
            base = f"{pack_label} {pack.pack_id}, covenant {result.name!r}"
            add(result.value, f"{base} value, FY{result.fiscal_year}")
            add(result.threshold, f"{base} threshold")
            add(result.headroom, f"{base} headroom, FY{result.fiscal_year}")
        for value_in in pack.supplemental:
            add(
                value_in.value,
                f"{pack_label} {pack.pack_id}, supplemental input "
                f"{value_in.field} ({value_in.period_label})",
            )
        for elimination in pack.eliminations:
            add(
                elimination.amount,
                f"{pack_label} {pack.pack_id}, elimination "
                f"{elimination.name!r} amount",
            )

    # Spread lines: both views, every period, plus period changes.
    for line in spread.lines:
        for view_name, values in (
            ("adjusted", line.adjusted_values),
            ("as reported", line.values),
        ):
            for year, value in sorted(values.items()):
                add(
                    value,
                    f"spread {spread.spread_id}, line {line.key} "
                    f"({line.label}) FY{year}, {view_name} view",
                )
        for year, change in sorted(line.changes.items()):
            if change is not None:
                add(
                    change,
                    f"spread {spread.spread_id}, line {line.key} "
                    f"({line.label}) change in FY{year}",
                )
                add(
                    abs(change),
                    f"spread {spread.spread_id}, line {line.key} "
                    f"({line.label}) change in FY{year}, magnitude",
                )
    for entry in spread.unmapped:
        for year, value in sorted(entry.values.items()):
            add(
                value,
                f"spread {spread.spread_id}, unmapped line "
                f"{entry.label!r} FY{year}",
            )
    for adjustment in spread.adjustments:
        add(
            adjustment.amount,
            f"spread {spread.spread_id}, adjustment "
            f"{adjustment.adjustment_id} amount",
        )

    # Document disagreements (round 8): every source's figure, the
    # distance from the figure the spread carries, the counts, and
    # the materiality line the policy drew — the figures the
    # disagreements section is allowed to state, each traced to
    # the record it comes from.
    if spread.disagreements:
        material_count = sum(1 for d in spread.disagreements if d.material)
        add(
            Decimal(len(spread.disagreements)),
            f"spread {spread.spread_id}, document disagreements recorded",
        )
        add(
            Decimal(material_count),
            f"spread {spread.spread_id}, material document disagreements",
        )
        add(
            Decimal(len(spread.disagreements) - material_count),
            f"spread {spread.spread_id}, immaterial document disagreements",
        )
        for record in spread.disagreements:
            base = (
                f"spread {spread.spread_id}, disagreement "
                f"{record.disagreement_id} on line {record.line_key} "
                f"({record.line_label}) FY{record.fiscal_year}"
            )
            for source in record.sources:
                label = SOURCE_TYPE_LABELS.get(
                    source.source_type, source.source_type
                )
                add(source.value, f"{base}, {label} figure")
                add(
                    source.delta_abs,
                    f"{base}, {label} difference from the spread figure",
                )
                if source.delta_rel is not None:
                    add(
                        (source.delta_rel * 100).quantize(Decimal("0.01")),
                        f"{base}, {label} difference from the spread "
                        "figure, percent",
                    )
        try:
            disagreement_policy = get_disagreement_policy(
                spread.disagreement_policy_version or None
            )
        except ValueError:
            disagreement_policy = None
        if disagreement_policy is not None:
            add(
                disagreement_policy.materiality_absolute,
                f"disagreement policy {disagreement_policy.version} "
                "materiality threshold, dollars",
            )
            add(
                disagreement_policy.materiality_relative * 100,
                f"disagreement policy {disagreement_policy.version} "
                "materiality threshold, percent of the spread figure",
            )

    # Policy report: thresholds, the values the tests read, the pack.
    for outcome in report.outcomes:
        base = f"policy report {report.report_id}, test {outcome.test_id}"
        add(outcome.threshold, f"{base} threshold")
        if outcome.threshold is not None:
            add(
                outcome.threshold * 100,
                f"{base} threshold expressed as a percentage",
            )
        for read in outcome.values_read:
            try:
                parsed = Decimal(read.value)
            except InvalidOperation:
                continue  # ids, dates, "5/8" tallies — not single figures
            add(parsed, f"{base}, input {read.input}")
    pack = get_policy_pack(report.policy_version)
    add(pack.capital_base, f"policy pack {pack.version} stated capital base")
    for test in pack.tests:
        base = f"policy pack {pack.version}, test {test.test_id}"
        add(test.threshold, f"{base} threshold")
        add(test.limit, f"{base} limit")
        if test.segment_thresholds:
            for segment, ceiling in test.segment_thresholds.items():
                add(ceiling, f"{base} ceiling for segment {segment}")

    # Stated facts of the credit file itself.
    facility, borrower = credit_file.facility, credit_file.borrower
    facts = "credit file"
    add(facility.amount, f"{facts}, facility amount requested")
    add(Decimal(facility.term_months), f"{facts}, facility term in months")
    add(Decimal(borrower.years_in_business), f"{facts}, years in business")
    for guarantor in facility.guarantors:
        add(
            guarantor.ownership_pct,
            f"{facts}, guarantor {guarantor.name} ownership",
        )
    required = [i for i in credit_file.checklist if i.required]
    add(Decimal(len(credit_file.checklist)), f"{facts}, checklist items")
    add(Decimal(len(required)), f"{facts}, required checklist items")
    add(
        Decimal(len(credit_file.missing_required())),
        f"{facts}, required items outstanding",
    )
    add(
        Decimal(len(required) - len(credit_file.missing_required())),
        f"{facts}, required items on file",
    )
    add(Decimal(len(credit_file.documents)), f"{facts}, documents on file")
    add(Decimal(len(facility.guarantors)), f"{facts}, guarantors named")
    add(Decimal(len(spread.periods)), f"spread {spread.spread_id}, periods")
    for year in spread.periods:
        add(Decimal(year), f"fiscal year {year}")
    for key, count in sorted(report.summary.items()):
        add(Decimal(count), f"policy report {report.report_id} summary: {key}")
    add(
        Decimal(len(report.outcomes)),
        f"policy report {report.report_id}, tests evaluated",
    )
    return universe


# ---------------------------------------------------------------------
# Rendering: the memo as the officer reads it. The verifier runs
# over exactly this text.
# ---------------------------------------------------------------------


def _render_header(memo: CreditMemo) -> str:
    if memo.status == "committee_approved":
        title = "CREDIT MEMORANDUM — COMMITTEE APPROVED"
        status_line = "Status: approved by the credit committee"
    elif memo.status == "committee_approved_conditional":
        title = "CREDIT MEMORANDUM — COMMITTEE APPROVED WITH CONDITIONS"
        status_line = (
            "Status: approved by the credit committee, with conditions"
        )
    elif memo.status == "declined":
        title = "CREDIT MEMORANDUM — DECLINED"
        status_line = (
            "Status: declined by the credit committee; the "
            "adverse-action reasons are on the decision record"
        )
    elif memo.status == "deferred":
        title = "CREDIT MEMORANDUM — OFFICER APPROVED"
        status_line = (
            "Status: deferred by the credit committee — returned "
            "to the credit officer for re-presentment"
        )
    elif memo.status == "in_committee":
        title = "CREDIT MEMORANDUM — OFFICER APPROVED"
        status_line = "Status: officer approved — before the credit committee"
    elif memo.status == "officer_approved":
        title = "CREDIT MEMORANDUM — OFFICER APPROVED"
        status_line = "Status: officer approved — committee-ready"
    elif memo.status == "returned":
        title = "CREDIT MEMORANDUM — DRAFT"
        status_line = "Status: returned for rework by the credit officer"
    elif memo.status == "in_officer_review":
        title = "CREDIT MEMORANDUM — DRAFT"
        status_line = "Status: in credit officer review"
    else:
        title = "CREDIT MEMORANDUM — DRAFT"
        status_line = "Status: draft — awaiting credit officer review"
    lines = [
        title,
        f"Borrower: {memo.borrower_name}",
        f"Facility: {memo.facility_type.replace('_', ' ')}",
        status_line,
        f"As of: {memo.as_of.isoformat()}",
        f"Spread: `{memo.spread_id}`",
        f"Computation pack: `{memo.computation_pack_id}`",
    ]
    if memo.global_pack_id:
        lines.append(f"Global computation pack: `{memo.global_pack_id}`")
    lines += [
        f"Policy report: `{memo.policy_report_id}` "
        f"(pack `{memo.policy_pack_id}`, version `{memo.policy_version}`)",
        f"Drafted by profile: {memo.composer_profile}",
    ]
    if memo.supersedes_memo_id:
        lines.append(f"Supersedes: `{memo.supersedes_memo_id}`")
    if memo.reviewed_by:
        # Code-spanned like every artefact id: an identifier, not
        # a figure, and never read as one by the verifier.
        lines.append(f"Reviewed by: `{memo.reviewed_by}`")
    return "\n".join(lines)


def _render_section(section: MemoSection) -> str:
    parts = [f"## {section.title}"]
    parts.extend(section.paragraphs)
    for table in section.tables:
        rows = []
        if table.caption:
            rows.append(table.caption)
        rows.append("| " + " | ".join(table.columns) + " |")
        rows.append("| " + " | ".join("---" for _ in table.columns) + " |")
        rows.extend("| " + " | ".join(row) + " |" for row in table.rows)
        parts.append("\n".join(rows))
    return "\n\n".join(parts)


def _render_chunks(memo: CreditMemo) -> list[tuple[str, str]]:
    chunks = [("header", _render_header(memo))]
    chunks.extend((s.key, _render_section(s)) for s in memo.sections)
    return chunks


def render_memo(memo: CreditMemo) -> str:
    """The memo's final rendered text — the verifier's input."""
    return "\n\n".join(chunk for _, chunk in _render_chunks(memo))


def render_disagreement_paragraph(record: Disagreement) -> str:
    """A disagreement stated in its record's own terms: the line
    and period, every source's figure exactly as reported, the
    distance of each from the figure the spread carries, and the
    policy's materiality verdict.

    This rendering is canonical: the composer writes it, and the
    verifier requires it verbatim. A memo may add prose around a
    disagreement, but the disagreement itself may not be reworded —
    rewording is how a material difference gets softened into a
    smaller one, and the check exists to make that a refusal
    rather than a judgement call.
    """
    parts: list[str] = []
    for source in record.sources:
        label = SOURCE_TYPE_LABELS.get(source.source_type, source.source_type)
        piece = f"the {label} report {fmt_money(source.value)}"
        if source.source_type == record.working_source_type:
            piece += ", the figure the spread carries"
        elif source.delta_rel is not None:
            direction = (
                "below" if source.value < record.working_value else "above"
            )
            percent = (source.delta_rel * 100).quantize(Decimal("0.01"))
            piece += (
                f", a difference of {fmt_money(source.delta_abs)} "
                f"({percent}% {direction} the spread figure)"
            )
        else:
            piece += (
                f", a difference of {fmt_money(source.delta_abs)} "
                "from the spread figure"
            )
        parts.append(piece)
    verdict = (
        "The disagreement policy judges the difference material."
        if record.material
        else "The disagreement policy judges the difference below "
             "the materiality line."
    )
    listing = "; ".join(parts)
    return (
        f"{record.line_label} (fiscal {record.fiscal_year}): "
        f"{listing}. {verdict}"
    )


# ---------------------------------------------------------------------
# The verifier.
# ---------------------------------------------------------------------


def verify_memo(memo: CreditMemo, inputs: MemoInputs) -> MemoVerification:
    """Verify a composed memo against the artefacts it pins.

    Three checks, all over the artefact as rendered:
      1. every figure in the rendered text traces into the universe
         built from the pinned inputs;
      2. the exceptions section accounts for exactly the pinned
         report's non-pass outcomes, by test id;
      3. when the pinned spread records document disagreements,
         the disagreements section exists and accounts for exactly
         the material ones, by id, each stated in its record's
         canonical terms (round 8).
    Any failure raises MemoVerificationError naming the offenders —
    the caller stores nothing.
    """
    universe = build_trace_universe(inputs)
    traces: list[FigureTrace] = []
    offenders: list[str] = []
    for section_key, chunk in _render_chunks(memo):
        for figure in extract_figures(chunk):
            source = universe.get(figure.value)
            if source is None:
                offenders.append(
                    f"{figure.text} in section {section_key!r}"
                )
            else:
                traces.append(FigureTrace(
                    text=figure.text,
                    value=format(figure.value, "f"),
                    kind=figure.kind,
                    source=source,
                    section_key=section_key,
                ))

    expected = [o.test_id for o in inputs.policy_report.non_pass_outcomes()]
    exceptions = memo.section("policy_exceptions")
    listed = list(exceptions.exception_test_ids) if exceptions else []
    missing_exceptions = [t for t in expected if t not in listed]
    unexpected = [t for t in listed if t not in expected]
    duplicated = sorted({t for t in listed if listed.count(t) > 1})

    # Round 8: disagreements integrity, the exceptions discipline
    # applied to the spread's contested lines.
    spread = inputs.spread
    material_records = [d for d in spread.disagreements if d.material]
    expected_disagreements = [d.disagreement_id for d in material_records]
    disagreements_section = memo.section("document_disagreements")
    listed_disagreements = (
        list(disagreements_section.disagreement_ids)
        if disagreements_section else []
    )
    missing_disagreements = [
        d for d in expected_disagreements if d not in listed_disagreements
    ]
    unexpected_disagreements = [
        d for d in listed_disagreements if d not in expected_disagreements
    ]
    duplicated_disagreements = sorted(
        {d for d in listed_disagreements if listed_disagreements.count(d) > 1}
    )
    unstated: list[str] = []
    if disagreements_section is not None:
        paragraphs = set(disagreements_section.paragraphs)
        unstated = [
            d.disagreement_id
            for d in material_records
            if render_disagreement_paragraph(d) not in paragraphs
        ]

    problems: list[str] = []
    if offenders:
        problems.append(
            "untraceable figure(s): " + "; ".join(offenders)
        )
    if missing_exceptions:
        problems.append(
            "policy exception(s) omitted: " + ", ".join(missing_exceptions)
        )
    if unexpected:
        problems.append(
            "exception(s) listed that the pinned report does not "
            "record: " + ", ".join(unexpected)
        )
    if duplicated:
        problems.append(
            "exception(s) listed more than once: " + ", ".join(duplicated)
        )
    if spread.disagreements and disagreements_section is None:
        problems.append(
            "document disagreements section missing: the pinned "
            f"spread records {len(spread.disagreements)} disagreement(s) "
            f"({len(material_records)} material) and the memo "
            "carries none of them"
        )
    if missing_disagreements:
        problems.append(
            "material disagreement(s) omitted: "
            + ", ".join(missing_disagreements)
        )
    if unexpected_disagreements:
        problems.append(
            "disagreement(s) listed that the pinned spread does not "
            "record as material: " + ", ".join(unexpected_disagreements)
        )
    if duplicated_disagreements:
        problems.append(
            "disagreement(s) listed more than once: "
            + ", ".join(duplicated_disagreements)
        )
    if unstated:
        problems.append(
            "material disagreement(s) not stated in their recorded "
            "terms: " + ", ".join(unstated)
        )
    if problems:
        raise MemoVerificationError(
            "memo refused by the figure verifier — " + "; ".join(problems),
            untraceable=offenders,
            missing_exceptions=missing_exceptions,
        )
    return MemoVerification(
        composer_profile=memo.composer_profile,
        figure_count=len(traces),
        figures=traces,
        exceptions_expected=expected,
        exceptions_listed=listed,
        disagreements_expected=expected_disagreements,
        disagreements_listed=listed_disagreements,
        verified=True,
    )


# ---------------------------------------------------------------------
# Composer port and profiles.
# ---------------------------------------------------------------------


class MemoComposer(Protocol):
    """Drafts memo sections from the pinned artefacts.

    A composer proposes; the verifier disposes. Nothing a composer
    returns is stored until verify_memo has cleared it, so a
    composer may be swapped (offline, live) without weakening law 1.
    """

    name: str

    def compose(self, inputs: MemoInputs) -> list[MemoSection]: ...


def _figure_for(
    pack: ComputationPack, key: str, fiscal_year: int
) -> ComputedFigure | None:
    for figure in pack.figures:
        if figure.key == key and figure.fiscal_year == fiscal_year:
            return figure
    return None


def _latest_figure(
    pack: ComputationPack, key: str
) -> ComputedFigure | None:
    candidates = [
        f for f in pack.figures if f.key == key and f.fiscal_year is not None
    ]
    return max(candidates, key=lambda f: f.fiscal_year) if candidates else None


def _outcome_figure_value(outcome: PolicyTestOutcome) -> Decimal | None:
    for read in outcome.values_read:
        if read.input.startswith("figure:"):
            try:
                return Decimal(read.value)
            except InvalidOperation:
                return None
    return None


def _outcome_fact(outcome: PolicyTestOutcome, name: str) -> str | None:
    for read in outcome.values_read:
        if read.input == f"fact:{name}":
            return read.value
    return None


class OfflineMemoComposer:
    """The deterministic default profile.

    Assembles the narrative from the structured artefacts only,
    formatting every figure through the helpers above. Given the
    same inputs it writes the same memo, word for word — which is
    what makes the composed artefact content-addressable.
    """

    name = "offline"

    def compose(self, inputs: MemoInputs) -> list[MemoSection]:
        sections = [
            self._request_summary(inputs),
            self._borrower_overview(inputs),
            self._financial_analysis(inputs),
        ]
        disagreements = self._document_disagreements(inputs)
        if disagreements is not None:
            sections.append(disagreements)
        sections += [
            self._strengths(inputs),
            self._risks_and_mitigants(inputs),
            self._policy_exceptions(inputs),
            self._recommendation(inputs),
            self._conditions(inputs),
        ]
        return sections

    # -- sections ----------------------------------------------------

    def _request_summary(self, inputs: MemoInputs) -> MemoSection:
        credit_file = inputs.credit_file
        facility = credit_file.facility
        paragraphs = [
            f"{credit_file.borrower.legal_name} requests a "
            f"{facility.facility_type.value.replace('_', ' ')} of "
            f"{fmt_money(facility.amount)} over a term of "
            f"{facility.term_months} months.",
            f"Stated purpose: {facility.purpose}.",
        ]
        if facility.collateral_description:
            paragraphs.append(
                f"Collateral: {facility.collateral_description}."
            )
        if facility.guarantors:
            names = " and ".join(
                f"{g.name} ({fmt_percent(g.ownership_pct)} ownership)"
                for g in facility.guarantors
            )
            paragraphs.append(f"The facility is guaranteed by {names}.")
        return MemoSection(
            key="request_summary", title="Request summary",
            paragraphs=paragraphs,
        )

    def _borrower_overview(self, inputs: MemoInputs) -> MemoSection:
        credit_file = inputs.credit_file
        borrower = credit_file.borrower
        paragraphs = [
            f"{borrower.legal_name} is a "
            f"{borrower.segment.value.replace('_', ' ')} borrower formed "
            f"in {borrower.state_of_formation}, in business for "
            f"{borrower.years_in_business} years.",
        ]
        if borrower.dba:
            paragraphs.append(f"It trades as {borrower.dba}.")
        missing = len(credit_file.missing_required())
        paragraphs.append(
            f"The credit file holds {len(credit_file.documents)} "
            f"documents against a checklist of "
            f"{len(credit_file.checklist)} items, of which {missing} "
            f"required items remain outstanding."
            if missing
            else f"The credit file holds {len(credit_file.documents)} "
            f"documents against a checklist of "
            f"{len(credit_file.checklist)} items; every required item "
            f"is on file."
        )
        return MemoSection(
            key="borrower_overview", title="Borrower overview",
            paragraphs=paragraphs,
        )

    def _financial_analysis(self, inputs: MemoInputs) -> MemoSection:
        spread, pack = inputs.spread, inputs.pack
        paragraphs: list[str] = []
        revenue = next(
            (line for line in spread.lines if line.key == "revenue"), None
        )
        if revenue is not None and len(spread.periods) >= 2:
            latest = max(spread.periods)
            change = revenue.changes.get(latest)
            if change is not None and change != 0:
                direction = "grew" if change > 0 else "fell"
                paragraphs.append(
                    f"Revenue {direction} by {fmt_money(abs(change))} "
                    f"in FY{latest} against the prior year."
                )
        years = sorted(
            {f.fiscal_year for f in pack.figures if f.fiscal_year is not None}
        )
        for year in years:
            clauses: list[str] = []
            rev = revenue.adjusted_values.get(year) if revenue else None
            if rev is not None:
                clauses.append(f"revenue was {fmt_money(rev)}")
            net_income = _spread_value(spread, "net_income", year)
            if net_income is not None:
                clauses.append(f"net income was {fmt_money(net_income)}")
            current_ratio = _figure_for(pack, "current_ratio", year)
            if current_ratio is not None and current_ratio.value is not None:
                clauses.append(
                    f"the current ratio stood at {fmt_ratio(current_ratio.value)}"
                )
            working_capital = _figure_for(pack, "working_capital", year)
            if working_capital is not None and working_capital.value is not None:
                clauses.append(
                    f"working capital was {fmt_money(working_capital.value)}"
                )
            leverage = _figure_for(pack, "debt_to_tangible_net_worth", year)
            if leverage is not None and leverage.value is not None:
                clauses.append(
                    "total liabilities stood at "
                    f"{fmt_ratio(leverage.value)} of tangible net worth"
                )
            if clauses:
                paragraphs.append(
                    f"In FY{year}, " + "; ".join(clauses) + "."
                )
        if years:
            latest = years[-1]
            not_computable = [
                f for f in pack.figures
                if f.fiscal_year == latest and f.status != "computed"
                and f.key in _HEADLINE_KEYS
            ]
            for figure in not_computable:
                missing = "; ".join(figure.missing_inputs)
                paragraphs.append(
                    f"{figure.label} for FY{latest} could not be "
                    f"computed from the statements on file"
                    + (f" — the missing input is {missing}." if missing else ".")
                )
        tables = [self._spread_table(inputs), self._ratio_table(inputs)]
        return MemoSection(
            key="financial_analysis", title="Financial analysis",
            paragraphs=paragraphs,
            tables=[t for t in tables if t.rows],
        )

    def _spread_table(self, inputs: MemoInputs) -> MemoTable:
        spread = inputs.spread
        years = sorted(spread.periods)
        rows: list[list[str]] = []
        for key in _SPREAD_TABLE_LINES:
            line = next((l for l in spread.lines if l.key == key), None)
            if line is None:
                continue
            cells = [line.label]
            for year in years:
                value = line.adjusted_values.get(year)
                cells.append(fmt_money(value) if value is not None else "—")
            rows.append(cells)
        return MemoTable(
            caption="Spread summary (adjusted view)",
            columns=["Line"] + [f"FY{year}" for year in years],
            rows=rows,
        )

    def _ratio_table(self, inputs: MemoInputs) -> MemoTable:
        pack = inputs.pack
        years = sorted(
            {f.fiscal_year for f in pack.figures if f.fiscal_year is not None}
        )
        rows: list[list[str]] = []
        for key in _RATIO_TABLE_KEYS:
            figures = [
                _figure_for(pack, key, year) for year in years
            ]
            if all(f is None for f in figures):
                continue
            label = next(
                (f.label for f in figures if f is not None), key
            )
            cells = [label]
            for figure in figures:
                if figure is None:
                    cells.append("—")
                elif figure.value is None:
                    cells.append("not computable")
                else:
                    cells.append(fmt_figure(figure))
            rows.append(cells)
        return MemoTable(
            caption="Computed measures",
            columns=["Measure"] + [f"FY{year}" for year in years],
            rows=rows,
        )

    def _document_disagreements(
        self, inputs: MemoInputs
    ) -> MemoSection | None:
        """The disagreements section (round 8): present only when
        the spread records disagreements — a memo on agreeing
        documents has nothing to carry, and an empty section would
        imply a check that found nothing to say either way.

        Every material disagreement is stated in its record's
        canonical terms (the verifier requires them verbatim);
        immaterial ones are counted, not listed — their full
        records ride on the spread and through the disagreements
        view, so the count here is a pointer, not a summary that
        could quietly absorb a material one (the verifier checks
        the section's id list against the spread's material set,
        never against the prose)."""
        spread = inputs.spread
        if not spread.disagreements:
            return None
        material = [d for d in spread.disagreements if d.material]
        immaterial = [d for d in spread.disagreements if not d.material]
        try:
            policy = get_disagreement_policy(
                spread.disagreement_policy_version or None
            )
        except ValueError:
            policy = get_disagreement_policy()
        paragraphs = [
            "The documents behind this spread do not fully agree, "
            "and the differences are carried on the record rather "
            "than resolved quietly. The spread records "
            f"{len(spread.disagreements)} document disagreement(s) "
            f"under disagreement policy `{policy.policy_id}` "
            f"version `{policy.version}`: {len(material)} "
            "material and "
            f"{len(immaterial)} below the materiality line. The "
            "spread's working figures follow the policy's source "
            "precedence — the audited statements first, then the "
            "tax return, then the management statements — and no "
            "figure was averaged and no source's figure dropped "
            "to reach them.",
        ]
        paragraphs.extend(
            render_disagreement_paragraph(d) for d in material
        )
        if material:
            paragraphs.append(
                "Figures computed over a materially disagreed "
                "line carry the disagreement's flag in the "
                "computation pack, so a ratio that rests on a "
                "contested figure is marked as resting on one "
                "wherever it is read."
            )
        if immaterial:
            paragraphs.append(
                f"The remaining {len(immaterial)} disagreement(s) "
                "fall below the disagreement policy's materiality "
                f"line — a difference reaching "
                f"{fmt_money(policy.materiality_absolute)} or "
                f"{fmt_percent(policy.materiality_relative * 100)} of "
                "the spread figure — and are recorded in full on "
                "the spread and through the disagreements view. "
                "They are counted here so the file shows they were "
                "seen; they flag no figure."
            )
        return MemoSection(
            key="document_disagreements",
            title="Document disagreements",
            paragraphs=paragraphs,
            disagreement_ids=[d.disagreement_id for d in material],
        )

    def _strengths(self, inputs: MemoInputs) -> MemoSection:
        report = inputs.policy_report
        paragraphs: list[str] = []
        for outcome in report.outcomes:
            if outcome.outcome != "pass":
                continue
            paragraphs.append(self._strength_line(inputs, outcome))
        if not paragraphs:
            paragraphs.append(
                "No policy test passed on the pinned report; every "
                "outcome is set out in the policy exceptions section."
            )
        return MemoSection(
            key="strengths", title="Strengths", paragraphs=paragraphs
        )

    def _strength_line(
        self, inputs: MemoInputs, outcome: PolicyTestOutcome
    ) -> str:
        report = inputs.policy_report
        pack = get_policy_pack(report.policy_version)
        test = next(
            (t for t in pack.tests if t.test_id == outcome.test_id), None
        )
        if test is not None and test.kind == "figure" and test.figure_key:
            source_pack = (
                inputs.global_pack
                if test.scope == "global" and inputs.global_pack is not None
                else inputs.pack
            )
            figure = _latest_figure(source_pack, test.figure_key)
            value = _outcome_figure_value(outcome)
            if figure is not None and value is not None and outcome.threshold is not None:
                return (
                    f"{outcome.name}: {figure.label} for "
                    f"FY{outcome.fiscal_year} is {fmt_figure(figure)} "
                    f"against a requirement of {outcome.operator} "
                    f"{fmt_ratio(outcome.threshold)} — the test passes."
                )
        if test is not None and test.fact == "required_documents":
            required = [
                i for i in inputs.credit_file.checklist if i.required
            ]
            return (
                f"{outcome.name}: all {len(required)} required "
                "documents are on file."
            )
        if test is not None and test.fact == "statement_age":
            age = _outcome_fact(outcome, "age_months")
            if age is not None and outcome.threshold is not None:
                return (
                    f"{outcome.name}: the most recent statements are "
                    f"{age} months old at the as-of date, within the "
                    f"{fmt_ratio(outcome.threshold).rstrip('×')}-month limit."
                )
        return f"{outcome.name}: the test passes on the pinned report."

    def _risks_and_mitigants(self, inputs: MemoInputs) -> MemoSection:
        report = inputs.policy_report
        credit_file = inputs.credit_file
        paragraphs: list[str] = []
        non_pass = report.non_pass_outcomes()
        if non_pass:
            names = "; ".join(o.name for o in non_pass)
            paragraphs.append(
                f"Policy exceptions are open on this file: {names}. "
                "Each is set out in full, with its figures and "
                "citation, in the policy exceptions section."
            )
        else:
            paragraphs.append(
                "The pinned policy report records no exceptions on "
                "this file."
            )
        years = sorted(
            {f.fiscal_year for f in inputs.pack.figures
             if f.fiscal_year is not None}
        )
        if years:
            latest = years[-1]
            gaps = [
                f.label for f in inputs.pack.figures
                if f.fiscal_year == latest and f.status != "computed"
                and f.key in _HEADLINE_KEYS
            ]
            if gaps:
                paragraphs.append(
                    "Measures the statements on file cannot support "
                    f"for FY{latest}: " + "; ".join(gaps) + ". A "
                    "coverage conclusion cannot be drawn until the "
                    "missing inputs are on file."
                )
        facility = credit_file.facility
        if facility.guarantors:
            names = " and ".join(g.name for g in facility.guarantors)
            paragraphs.append(
                f"Mitigant: the facility is guaranteed by {names}."
            )
        if facility.collateral_description:
            paragraphs.append(
                "Mitigant: collateral is described on the file as "
                f"follows — {facility.collateral_description}."
            )
        paragraphs.append(
            "No other mitigants are recorded in the artefacts this "
            "draft is composed from; a mitigant the officer relies "
            "on must be recorded when the draft is reviewed."
        )
        return MemoSection(
            key="risks_mitigants", title="Risks and mitigants",
            paragraphs=paragraphs,
        )

    def _policy_exceptions(self, inputs: MemoInputs) -> MemoSection:
        report = inputs.policy_report
        non_pass = report.non_pass_outcomes()
        if not non_pass:
            return MemoSection(
                key="policy_exceptions", title="Policy exceptions",
                paragraphs=[
                    "The pinned policy report records no exceptions: "
                    "every test passed."
                ],
                exception_test_ids=[],
            )
        paragraphs = [
            self._exception_paragraph(inputs, outcome) for outcome in non_pass
        ]
        return MemoSection(
            key="policy_exceptions", title="Policy exceptions",
            paragraphs=paragraphs,
            exception_test_ids=[o.test_id for o in non_pass],
        )

    def _exception_paragraph(
        self, inputs: MemoInputs, outcome: PolicyTestOutcome
    ) -> str:
        report = inputs.policy_report
        verdict = "FAIL" if outcome.outcome == "fail" else "NOT EVALUABLE"
        head = (
            f"{outcome.name} — {verdict} (severity: {outcome.severity})."
        )
        body = ""
        test = next(
            (t for t in get_policy_pack(report.policy_version).tests
             if t.test_id == outcome.test_id),
            None,
        )
        if outcome.outcome == "not_evaluable":
            missing = "; ".join(outcome.missing_inputs)
            body = (
                " The test could not be evaluated"
                + (f": {missing}." if missing else ".")
            )
        elif test is not None and test.kind == "figure":
            value = _outcome_figure_value(outcome)
            source_pack = (
                inputs.global_pack
                if test.scope == "global" and inputs.global_pack is not None
                else inputs.pack
            )
            figure = (
                _figure_for(source_pack, test.figure_key, outcome.fiscal_year)
                if test.figure_key and outcome.fiscal_year is not None
                else None
            )
            if value is not None and outcome.threshold is not None:
                rendered = (
                    fmt_figure(figure) if figure is not None
                    else fmt_ratio(value)
                )
                body = (
                    f" The figure for FY{outcome.fiscal_year} is "
                    f"{rendered} against a requirement of "
                    f"{outcome.operator} {fmt_ratio(outcome.threshold)}."
                )
        elif test is not None and test.fact == "required_documents":
            missing_items = [
                i for i in inputs.credit_file.checklist
                if i.required and not i.satisfied
            ]
            required = [
                i for i in inputs.credit_file.checklist if i.required
            ]
            labels = "; ".join(
                f"{i.item_id} ({i.label})" for i in missing_items
            )
            body = (
                f" {len(missing_items)} of {len(required)} required "
                f"items remain outstanding: {labels}."
            )
        elif test is not None and test.fact in ("statement_age", "appraisal_age"):
            age = _outcome_fact(outcome, "age_months")
            if age is not None and outcome.threshold is not None:
                subject = (
                    "statements" if test.fact == "statement_age"
                    else "appraisal"
                )
                body = (
                    f" The most recent {subject} are {age} months old "
                    f"at {report.as_of.isoformat()}, against a limit "
                    f"of {format(outcome.threshold, 'f')} months."
                )
        elif test is not None and test.fact == "concentration":
            share = _outcome_fact(outcome, "concentration_share")
            capital = get_policy_pack(report.policy_version).capital_base
            if share is not None and outcome.threshold is not None:
                body = (
                    f" The facility is {fmt_ratio(Decimal(share))} of "
                    f"the bank's stated capital base of "
                    f"{fmt_money(capital)}, against a limit of "
                    f"{fmt_ratio(outcome.threshold)}."
                )
        return f"{head}{body} Citation: {outcome.citation}"

    def _recommendation(self, inputs: MemoInputs) -> MemoSection:
        return MemoSection(
            key="recommendation", title="Recommendation",
            paragraphs=[
                "Reserved for the credit officer. This draft states "
                "the facts of the credit file and the outcomes of the "
                "pinned policy report; it makes no recommendation. "
                "The officer's recommendation, and the reasons for "
                "it, are recorded when this draft is reviewed."
            ],
        )

    def _conditions(self, inputs: MemoInputs) -> MemoSection:
        missing = len(inputs.credit_file.missing_required())
        if missing:
            text = (
                f"Conditions are set at approval; none are recorded "
                f"yet. {missing} required checklist items remain "
                "outstanding on this file, and the conditions must "
                "address each of them."
            )
        else:
            text = (
                "Conditions are set at approval; none are recorded "
                "yet. No required checklist items remain outstanding "
                "on this file."
            )
        return MemoSection(
            key="conditions", title="Conditions", paragraphs=[text]
        )


_HEADLINE_KEYS = {
    "ebitda", "dscr", "debt_service", "fixed_charge_coverage",
    "debt_to_ebitda", "free_cash_flow", "operating_cash_flow",
    "global_dscr",
}

_SPREAD_TABLE_LINES = [
    "revenue", "gross_profit", "ebit", "net_income",
    "total_assets", "total_liabilities", "total_equity",
]

_RATIO_TABLE_KEYS = [
    "current_ratio", "quick_ratio", "working_capital", "total_debt",
    "debt_to_tangible_net_worth", "debt_to_ebitda", "ebitda_margin",
    "dscr", "fixed_charge_coverage", "return_on_assets",
    "return_on_equity",
]


def _spread_value(spread: Spread, key: str, year: int) -> Decimal | None:
    line = next((l for l in spread.lines if l.key == key), None)
    return line.adjusted_values.get(year) if line else None


class LiveMemoComposer:
    """The live drafting profile: defined, not configured.

    A model-backed drafter slots in behind the same port; its
    output faces the same verifier. Provider-key handling belongs
    to a later round, so composing under this profile fails loudly
    instead of silently substituting the offline profile.
    """

    name = "live"

    def compose(self, inputs: MemoInputs) -> list[MemoSection]:
        raise ValidationError(
            "the 'live' memo drafting profile is defined but not "
            "configured in this round — no provider key is wired; "
            "use the 'offline' profile"
        )


COMPOSER_PROFILES: dict[str, type] = {
    OfflineMemoComposer.name: OfflineMemoComposer,
    LiveMemoComposer.name: LiveMemoComposer,
}


def get_composer(profile: str) -> MemoComposer:
    composer_type = COMPOSER_PROFILES.get(profile)
    if composer_type is None:
        raise ValidationError(
            f"unknown memo composer profile {profile!r}; "
            f"known profiles: {sorted(COMPOSER_PROFILES)}"
        )
    return composer_type()


# ---------------------------------------------------------------------
# Composition: draft, verify, address. Only a verified memo exists.
# ---------------------------------------------------------------------


def _canonical_hash(descriptor: dict) -> str:
    blob = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compose_memo(
    credit_file: CreditFile,
    spread: Spread,
    pack: ComputationPack,
    policy_report: PolicyReport,
    *,
    global_pack: ComputationPack | None = None,
    composer: MemoComposer | None = None,
) -> CreditMemo:
    """Compose a draft memo from pinned artefacts and verify it.

    Pure and deterministic: identical inputs under the same
    composer profile reproduce the identical memo, id included.
    Raises MemoVerificationError — and produces nothing storable —
    when the draft cannot be fully traced or drops an exception.
    """
    composer = composer or OfflineMemoComposer()
    inputs = MemoInputs(
        credit_file=credit_file,
        spread=spread,
        pack=pack,
        policy_report=policy_report,
        global_pack=global_pack,
    )
    sections = composer.compose(inputs)
    draft = CreditMemo(
        memo_id="memo-pending",
        credit_file_id=credit_file.credit_file_id,
        borrower_name=credit_file.borrower.legal_name,
        facility_type=credit_file.facility.facility_type.value,
        spread_id=spread.spread_id,
        computation_pack_id=pack.pack_id,
        global_pack_id=global_pack.pack_id if global_pack else None,
        policy_pack_id=policy_report.policy_pack_id,
        policy_version=policy_report.policy_version,
        policy_report_id=policy_report.report_id,
        as_of=policy_report.as_of,
        composer_profile=composer.name,
        sections=sections,
        verification=MemoVerification(
            composer_profile=composer.name, figure_count=0
        ),
    )
    verification = verify_memo(draft, inputs)
    descriptor = {
        "credit_file_id": draft.credit_file_id,
        "spread_id": draft.spread_id,
        "computation_pack_id": draft.computation_pack_id,
        "global_pack_id": draft.global_pack_id,
        "policy_report_id": draft.policy_report_id,
        "composer_profile": draft.composer_profile,
        "sections": [s.model_dump(mode="json") for s in draft.sections],
        "verification": verification.model_dump(mode="json"),
    }
    memo_id = f"memo-{_canonical_hash(descriptor)[:16]}"
    return draft.model_copy(
        update={"memo_id": memo_id, "verification": verification}
    )
