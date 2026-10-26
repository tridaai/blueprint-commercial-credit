"""Annual reviews: the pack, the summary, the stance (round 12).

An approval starts a facility's monitoring life (rounds 10–11);
this module is the periodic judgement that life is reviewed
under. Once a year the bank owes itself a fresh look at the
credit — on fresh figures, against current policy as well as the
policy the credit was decided under — and a named credit officer
records a coded stance on what they found.

- **The obligation is a clock, derived in code.** Every covenant
  set carries annual review instances: the first falls due twelve
  months after the facility's anchor — its closing date, or the
  approval date when no closing is recorded — then annually to
  maturity. Instances are derived, never stored: the same set,
  the same outcomes and the same as_of always produce the same
  obligations, which is what makes the graded date table
  (leap-year anniversaries included) possible. An instance stands
  scheduled / due_soon / due / overdue until an outcome closes
  it, and then it stands completed; the next instance derives
  from the anchor regardless — a late review never pushes the
  following one out.
- **The pack is assembled, not written.** An AnnualReviewPack is
  built in code from stored artefacts only: the current spread
  and computation pack (recomputed through the existing engines
  over the latest statements, with the set's covenants as
  definitions), the period's covenant test history with
  restatement standing, its breach cases, the policy report
  re-run under BOTH the pack version the memo pinned and the
  current version — differences named, never silently re-based —
  the round 9 readiness report recomputed at the review date,
  and the material disagreements the spread carries. When a
  required input is stale or missing the pack is still produced,
  marked incomplete, naming every blocker (law 7's honesty,
  carried into the review): absence is stated, never papered
  over.
- **The summary cannot soften the record.** Its locked facts are
  rendered by code from the pinned artefacts — counts, failed
  tests, breaches, the policy position under both versions, the
  disagreements in their canonical terms, the incompleteness
  statement — and the composer's narrative, drafted through the
  model port, passes through the round 5 figure verifier before
  the pack exists: every figure traces to a pinned input, every
  locked paragraph stands verbatim, and prose that denies a
  locked fact (a period with breaches described as breach-free)
  is refused outright.
- **The stance is coded, named, and not self-reviewed.** The
  officer records one stance from the versioned vocabulary —
  affirm / affirm_with_conditions / place_on_watchlist /
  escalate_to_committee — with coded reasons; conditions reuse
  the round 7 machinery whole (structure, threshold traces,
  schedulable cadences). The reviewer must hold a credit-officer
  role, and the officer who approved the decided memo may not
  review their own credit alone: their outcome requires a
  countersigning officer. Escalation mints a CommitteeReferral —
  a recorded surface naming the pack and the memo; the committee
  itself still acts only through the round 7 gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

from .committee import validate_conditions
from .covenants import add_months, frequency_from_cadence, maturity_date
from .errors import MemoVerificationError, ValidationError
from .memo import (
    MemoInputs,
    build_trace_universe,
    extract_figures,
    fmt_money,
    fmt_ratio,
    render_disagreement_paragraph,
)
from .models import (
    BREACH_TERMINAL_STATES,
    REVIEW_COMPLETED,
    SCHEDULE_DUE,
    SCHEDULE_DUE_SOON,
    SCHEDULE_OVERDUE,
    SCHEDULE_SCHEDULED,
    AnnualReviewOutcome,
    BreachCase,
    CommitteeCondition,
    ComputationPack,
    CovenantSet,
    CovenantTestResult,
    CreditFile,
    CreditMemo,
    FigureTrace,
    PolicyReport,
    RatingAssignment,
    RatingOverride,
    ReadinessEntry,
    ReadinessReport,
    ReviewSummaryVerification,
    Spread,
)
from .officer_gate import require_officer

# An instance stands `due_soon` this many days before its due
# date — the window in which the pack should be assembled. The
# same window the covenant schedule uses (round 10).
DUE_SOON_DAYS = 30

# ---------------------------------------------------------------------
# The stance + reason vocabulary (versioned, like the gates').
# A stance without coded reasons is a free-text stance, and a
# free-text stance is refused: the record must say, mechanically,
# WHY the officer landed where they landed.
# ---------------------------------------------------------------------

REVIEW_VOCABULARY_VERSION = "annual-review-reasons-v1"

STANCE_AFFIRM = "affirm"
STANCE_AFFIRM_WITH_CONDITIONS = "affirm_with_conditions"
STANCE_WATCHLIST = "place_on_watchlist"
STANCE_ESCALATE = "escalate_to_committee"

REVIEW_STANCES = frozenset({
    STANCE_AFFIRM,
    STANCE_AFFIRM_WITH_CONDITIONS,
    STANCE_WATCHLIST,
    STANCE_ESCALATE,
})

REVIEW_REASON_CODES: dict[str, str] = {
    "performance_in_line": (
        "performance is in line with the underwriting case for "
        "the credit"
    ),
    "covenant_breach_in_period": (
        "a covenant breached in the period under review"
    ),
    "policy_exception_open": (
        "a policy exception stands open under the current policy "
        "pack"
    ),
    "deteriorating_trend": (
        "the figures trend adversely against the prior period"
    ),
    "incomplete_inputs": (
        "required inputs are stale or missing: the pack is "
        "incomplete"
    ),
    "watchlist_criteria_met": (
        "the credit meets the bank's watchlist criteria"
    ),
    "other": "another reason, stated in the review note",
}


# ---------------------------------------------------------------------
# The obligation: derived instances over the set's anchor.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewInstance:
    """One annual review obligation, derived — never stored.

    `period_start` is the anchor for the first review and the
    previous review's due date thereafter: the window the pack's
    covenant history and breaches are read over. `state` is one
    of the SCHEDULE_* states, or `completed` once an outcome has
    closed the instance."""

    credit_file_id: str
    set_id: str
    review_number: int
    anchor_date: date
    period_start: date
    due_date: date
    state: str
    days_until_due: int
    outcome_id: str | None = None
    pack_id: str | None = None


def review_due_dates(anchor: date, *, maturity: date) -> list[date]:
    """The annual anniversaries of `anchor`, up to and including
    maturity. Month-lattice arithmetic (round 10's add_months):
    a facility closed on a leap day is reviewed on Feb 28 in
    common years and Feb 29 in leap years — never Mar 1, never
    skipped."""
    due: list[date] = []
    step = 1
    while True:
        candidate = add_months(anchor, 12 * step)
        if candidate > maturity:
            return due
        due.append(candidate)
        step += 1


def derive_review_instances(
    credit_file_id: str,
    covenant_set: CovenantSet,
    *,
    anchor: date,
    as_of: date,
    outcomes: list[AnnualReviewOutcome],
) -> list[ReviewInstance]:
    """Every review instance of one set at `as_of`.

    The anchor is the set's closing date when one is recorded —
    else the approval date the pipeline supplies (a set created
    before closing still owes its reviews; the anchor is named,
    never guessed silently — the pack states which basis it
    used). Maturity caps the series: a facility is not reviewed
    after it matures."""
    maturity = maturity_date(anchor, covenant_set.term_months)
    by_number = {o.review_number: o for o in outcomes}
    instances: list[ReviewInstance] = []
    period_start = anchor
    for number, due_date in enumerate(
        review_due_dates(anchor, maturity=maturity), start=1
    ):
        outcome = by_number.get(number)
        days = (due_date - as_of).days
        if outcome is not None:
            state = REVIEW_COMPLETED
        elif as_of > due_date:
            state = SCHEDULE_OVERDUE
        elif as_of >= due_date:
            state = SCHEDULE_DUE
        elif days <= DUE_SOON_DAYS:
            state = SCHEDULE_DUE_SOON
        else:
            state = SCHEDULE_SCHEDULED
        instances.append(ReviewInstance(
            credit_file_id=credit_file_id,
            set_id=covenant_set.set_id,
            review_number=number,
            anchor_date=anchor,
            period_start=period_start,
            due_date=due_date,
            state=state,
            days_until_due=days,
            outcome_id=outcome.outcome_id if outcome else None,
        ))
        period_start = due_date
    return instances


# States in which a review instance is work: assemble the pack,
# take the stance.
REVIEW_WORK_STATES = frozenset({
    SCHEDULE_OVERDUE, SCHEDULE_DUE, SCHEDULE_DUE_SOON,
})


# ---------------------------------------------------------------------
# Assembly inputs: everything the pack pins, in one place — the
# composer reads them, the locked facts render from them, and the
# verifier traces against them.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewInputs:
    credit_file: CreditFile
    covenant_set: CovenantSet
    review_number: int
    anchor_date: date
    anchor_basis: str  # closing_date | approval_date
    period_start: date
    due_date: date
    as_of: date
    spread: Spread
    pack: ComputationPack
    pinned_report: PolicyReport
    current_report: PolicyReport
    readiness: ReadinessReport
    results: list[CovenantTestResult] = field(default_factory=list)
    standings: dict[str, str] = field(default_factory=dict)
    breaches: list[BreachCase] = field(default_factory=list)
    open_breaches: list[BreachCase] = field(default_factory=list)
    blockers: list[ReadinessEntry] = field(default_factory=list)
    # Round 13: the rating standing current at assembly (or the
    # one a stored pack pinned), with the head of its override
    # chain. Carried as an input; never a stance input.
    rating: RatingAssignment | None = None
    rating_override: RatingOverride | None = None
    # Round 17: figures that appear only inside quarantined
    # document spans — struck from the review universe exactly
    # as from the memo universe.
    quarantined_figures: frozenset = frozenset()

    @property
    def complete(self) -> bool:
        return not self.blockers

    @property
    def differences(self) -> list[tuple[str, str, str]]:
        """(test_id, pinned outcome, current outcome) for every
        test whose outcome moved between the two reports."""
        current = {o.test_id: o.outcome for o in self.current_report.outcomes}
        return [
            (o.test_id, o.outcome, current[o.test_id])
            for o in self.pinned_report.outcomes
            if o.test_id in current and current[o.test_id] != o.outcome
        ]


# ---------------------------------------------------------------------
# The locked facts: rendered by code from the pinned artefacts.
# No composer writes these and no officer edits them; the
# verifier requires them verbatim in the rendered summary.
# ---------------------------------------------------------------------


def _fmt_result_figure(inputs: ReviewInputs, metric_key: str, value: Decimal) -> str:
    """Format a covenant figure the way its unit demands — money
    as money, ratios as ratios — reading the unit off the review
    pack's own figures for the metric."""
    for figure in inputs.pack.figures:
        if figure.key == metric_key:
            return fmt_money(value) if figure.unit == "USD" else fmt_ratio(value)
    return fmt_ratio(value)


def locked_fact_paragraphs(inputs: ReviewInputs) -> list[str]:
    """The canonical record paragraphs of one review pack."""
    credit_file = inputs.credit_file
    facility = credit_file.facility
    covenant_set = inputs.covenant_set
    paragraphs: list[str] = []

    anchor_word = (
        "the facility's closing date"
        if inputs.anchor_basis == "closing_date"
        else "the approval date (no closing date is recorded)"
    )
    paragraphs.append(
        f"Review position: {credit_file.borrower.legal_name} — "
        f"{facility.facility_type.value.replace('_', ' ')} facility "
        f"of {fmt_money(facility.amount)} over "
        f"{facility.term_months} months; covenant set "
        f"`{covenant_set.set_id}` from decision "
        f"`{covenant_set.decision_id}`, under policy pack "
        f"{covenant_set.policy_pack_id} {covenant_set.policy_version}. "
        f"Annual review {inputs.review_number}, anchored on "
        f"{anchor_word} {inputs.anchor_date.isoformat()}, covers "
        f"{inputs.period_start.isoformat()} to "
        f"{inputs.due_date.isoformat()}."
    )

    results = inputs.results
    if not results:
        paragraphs.append(
            "Covenant record for the period: no covenant tests "
            "were recorded in the period under review."
        )
    else:
        counts = {
            outcome: sum(1 for r in results if r.outcome == outcome)
            for outcome in ("pass", "fail", "not_evaluable", "awaiting_inputs")
        }
        superseded = sum(
            1 for r in results
            if inputs.standings.get(r.result_id)
            == "superseded_by_restatement"
        )
        paragraphs.append(
            f"Covenant record for the period: {len(results)} "
            f"test(s) recorded — {counts['pass']} passed, "
            f"{counts['fail']} failed, "
            f"{counts['not_evaluable']} not evaluable, "
            f"{counts['awaiting_inputs']} awaiting inputs; "
            f"{superseded} stand superseded by restatement."
        )
        for result in results:
            if result.outcome != "fail":
                continue
            paragraphs.append(
                f"FAILED COVENANT TEST: {result.covenant_name} "
                f"({result.period_label}) — figure "
                f"{_fmt_result_figure(inputs, result.metric_key, result.figure_value)} "
                f"against {result.operator} "
                f"{_fmt_result_figure(inputs, result.metric_key, result.threshold)} "
                f"on test date {result.test_date.isoformat()}; "
                f"result `{result.result_id}`; standing: "
                f"{inputs.standings.get(result.result_id, 'current')}."
            )

    if inputs.breaches:
        for breach in inputs.breaches:
            paragraphs.append(
                f"BREACH: {breach.covenant_name} "
                f"({breach.period_label}) — figure "
                f"{_fmt_result_figure(inputs, breach.metric_key, breach.figure_value)} "
                f"against {breach.operator} "
                f"{_fmt_result_figure(inputs, breach.metric_key, breach.threshold)}; "
                f"opened by the test of "
                f"{breach.test_date.isoformat()}; cure deadline "
                f"{breach.cure_deadline.isoformat()}; state: "
                f"{breach.state}; breach `{breach.breach_id}`."
            )
    paragraphs.append(
        f"Breach record: {len(inputs.breaches)} breach case(s) "
        f"arose from tests in the period; "
        f"{len(inputs.open_breaches)} breach case(s) stand open "
        "on this facility at this review."
    )

    pinned, current = inputs.pinned_report, inputs.current_report
    paragraphs.append(
        f"Policy record: the credit was decided under policy "
        f"pack {pinned.policy_pack_id} {pinned.policy_version} — "
        f"re-run at this review under {pinned.policy_version}: "
        f"{pinned.summary.get('pass', 0)} pass, "
        f"{pinned.summary.get('fail', 0)} fail, "
        f"{pinned.summary.get('not_evaluable', 0)} not evaluable. "
        f"Under the current version, {current.policy_version}: "
        f"{current.summary.get('pass', 0)} pass, "
        f"{current.summary.get('fail', 0)} fail, "
        f"{current.summary.get('not_evaluable', 0)} not evaluable."
    )
    differences = inputs.differences
    if differences:
        listing = "; ".join(
            f"`{test_id}` ({pinned_outcome} under "
            f"{pinned.policy_version}, {current_outcome} under "
            f"{current.policy_version})"
            for test_id, pinned_outcome, current_outcome in differences
        )
        paragraphs.append(
            f"{len(differences)} test outcome(s) differ between "
            f"the two versions: {listing}. The difference is "
            "recorded, not reconciled: the credit stands decided "
            "under the version it was decided under."
        )
    else:
        paragraphs.append(
            "No test outcome differs between the version the "
            "credit was decided under and the current version."
        )

    for record in inputs.spread.disagreements:
        if record.material:
            paragraphs.append(render_disagreement_paragraph(record))

    if inputs.rating is not None:
        # Round 13: the rating stated as the artefact citation it
        # is — grades and versions code-spanned like the ids, the
        # factor record riding on the pack itself. The sentence
        # that follows it is part of the locked fact: the rating
        # is an input to the review, and the record says so, so
        # no reader mistakes it for the review's stance.
        rating = inputs.rating
        final_grade = (
            inputs.rating_override.new_grade
            if inputs.rating_override is not None
            else rating.assigned_grade
        )
        moved = (
            f", moved to a final grade of `{final_grade}` by "
            f"override `{inputs.rating_override.override_id}` "
            f"(reason `{inputs.rating_override.reason_code}`)"
            if inputs.rating_override is not None
            else ""
        )
        paragraphs.append(
            f"Risk rating: assignment `{rating.assignment_id}` "
            f"under scorecard `{rating.scorecard_id}` version "
            f"`{rating.scorecard_version}` rates this credit "
            f"grade `{rating.assigned_grade}` "
            f"({rating.assigned_band}) as of "
            f"{rating.as_of.isoformat()}{moved}. The rating is a "
            "coded input carried into this review — it is not "
            "the review's stance, and it decides nothing."
        )

    if inputs.blockers:
        keys = ", ".join(f"`{b.input_key}`" for b in inputs.blockers)
        paragraphs.append(
            f"INCOMPLETE PACK: {len(inputs.blockers)} required "
            f"input(s) are stale or missing at this review — "
            f"{keys}. The pack is assembled with the gap named; "
            "it is not a complete pack, and the review's stance "
            "is read against that fact."
        )
    return paragraphs


# ---------------------------------------------------------------------
# The trace universe for a review summary: the round 5 universe
# over the pack's pinned artefacts, extended with the review's
# own facts — both policy reports in full, the covenant results
# and breaches the pack carries, and the counts the locked facts
# state. A figure outside this mapping does not trace.
# ---------------------------------------------------------------------


def build_review_universe(inputs: ReviewInputs) -> dict[Decimal, str]:
    universe = build_trace_universe(MemoInputs(
        credit_file=inputs.credit_file,
        spread=inputs.spread,
        pack=inputs.pack,
        policy_report=inputs.current_report,
        quarantined_figures=inputs.quarantined_figures,
    ))

    def add(value: Decimal | None, source: str) -> None:
        if value is not None:
            universe.setdefault(value, source)

    for report in (inputs.pinned_report, inputs.current_report):
        base = f"policy report {report.report_id}"
        for outcome in report.outcomes:
            add(outcome.threshold, f"{base}, test {outcome.test_id} threshold")
            if outcome.threshold is not None:
                add(
                    outcome.threshold * 100,
                    f"{base}, test {outcome.test_id} threshold as a percentage",
                )
            for read in outcome.values_read:
                try:
                    parsed = Decimal(read.value)
                except InvalidOperation:
                    continue
                add(parsed, f"{base}, test {outcome.test_id}, input {read.input}")
        for key, count in sorted(report.summary.items()):
            add(Decimal(count), f"{base} summary: {key}")
        add(Decimal(len(report.outcomes)), f"{base}, tests evaluated")

    add(Decimal(inputs.review_number), "the annual review's number")
    counts = {
        outcome: sum(1 for r in inputs.results if r.outcome == outcome)
        for outcome in ("pass", "fail", "not_evaluable", "awaiting_inputs")
    }
    add(Decimal(len(inputs.results)), "covenant tests recorded in the period")
    for outcome, count in counts.items():
        add(Decimal(count), f"covenant tests in the period: {outcome}")
    add(
        Decimal(sum(
            1 for r in inputs.results
            if inputs.standings.get(r.result_id)
            == "superseded_by_restatement"
        )),
        "covenant results superseded by restatement",
    )
    for result in inputs.results:
        base = f"covenant test result {result.result_id}"
        add(result.figure_value, f"{base} figure")
        add(result.threshold, f"{base} threshold")
        add(result.headroom, f"{base} headroom")
    add(Decimal(len(inputs.breaches)), "breach cases arising in the period")
    add(Decimal(len(inputs.open_breaches)), "breach cases standing open")
    for breach in inputs.breaches:
        base = f"breach case {breach.breach_id}"
        add(breach.figure_value, f"{base} figure")
        add(breach.threshold, f"{base} threshold")
    add(Decimal(len(inputs.blockers)), "readiness blockers on the pack")
    add(
        Decimal(len(inputs.differences)),
        "policy outcomes differing between versions",
    )
    return universe


# ---------------------------------------------------------------------
# The composer port. A composer proposes narrative; the verifier
# disposes. The offline profile is the deterministic default; the
# live profile is defined and refuses loudly until a provider key
# is wired — the same posture as the memo's profiles (round 5).
# ---------------------------------------------------------------------


class ReviewComposer(Protocol):
    name: str

    def compose(self, inputs: ReviewInputs) -> list[str]: ...


def _figure(inputs: ReviewInputs, key: str, fiscal_year: int):
    for figure in inputs.pack.figures:
        if figure.key == key and figure.fiscal_year == fiscal_year:
            return figure
    return None


def _spread_line_value(spread: Spread, key: str, year: int) -> Decimal | None:
    line = next((l for l in spread.lines if l.key == key), None)
    return line.adjusted_values.get(year) if line else None


class OfflineReviewComposer:
    """The deterministic default profile: narrative assembled
    from the pinned artefacts only, every figure formatted
    through the memo helpers so the verifier can read it back."""

    name = "offline"

    def compose(self, inputs: ReviewInputs) -> list[str]:
        paragraphs: list[str] = []
        spread = inputs.spread
        if not spread.periods:
            return paragraphs
        latest = max(spread.periods)
        borrower = inputs.credit_file.borrower.legal_name
        paragraphs.append(
            f"{borrower} is reviewed here on its most recent "
            f"spread statements, FY{latest}."
        )
        clauses: list[str] = []
        revenue = _spread_line_value(spread, "revenue", latest)
        if revenue is not None:
            clauses.append(f"revenue was {fmt_money(revenue)}")
        ebitda = _figure(inputs, "ebitda", latest)
        if ebitda is not None and ebitda.status == "computed":
            clauses.append(f"EBITDA was {fmt_money(ebitda.value)}")
        dscr = _figure(inputs, "dscr", latest)
        if dscr is not None:
            if dscr.status == "computed":
                clauses.append(
                    f"debt service coverage stood at "
                    f"{fmt_ratio(dscr.value)}"
                )
            else:
                clauses.append(
                    "debt service coverage was not computable "
                    f"for FY{latest} from the statements on file"
                )
        leverage = _figure(inputs, "debt_to_tangible_net_worth", latest)
        if leverage is not None and leverage.status == "computed":
            clauses.append(
                f"leverage (total liabilities to tangible net "
                f"worth) stood at {fmt_ratio(leverage.value)}"
            )
        if clauses:
            paragraphs.append(
                f"For FY{latest}: " + "; ".join(clauses) + "."
            )
        earlier = [y for y in spread.periods if y < latest]
        if earlier:
            prior = max(earlier)
            line = next(
                (l for l in spread.lines if l.key == "revenue"), None
            )
            change = line.changes.get(latest) if line else None
            if change is not None and change != 0:
                direction = "rose" if change > 0 else "fell"
                paragraphs.append(
                    f"Revenue {direction} by "
                    f"{fmt_money(abs(change))} against FY{prior}."
                )
        paragraphs.append(
            "The covenant record, the breach record and the "
            "policy position for the period are stated in this "
            "pack's locked record; this narrative adds no figure "
            "to them and softens nothing in them."
        )
        return paragraphs


class LiveReviewComposer:
    """The live drafting profile: defined, not configured — the
    same refusal the memo's live profile gives (round 5)."""

    name = "live"

    def compose(self, inputs: ReviewInputs) -> list[str]:
        raise ValidationError(
            "the 'live' review drafting profile is defined but "
            "not configured in this round — no provider key is "
            "wired; use the 'offline' profile"
        )


REVIEW_COMPOSER_PROFILES: dict[str, type] = {
    OfflineReviewComposer.name: OfflineReviewComposer,
    LiveReviewComposer.name: LiveReviewComposer,
}


def get_review_composer(profile: str) -> ReviewComposer:
    composer_type = REVIEW_COMPOSER_PROFILES.get(profile)
    if composer_type is None:
        raise ValidationError(
            f"unknown review composer profile {profile!r}; "
            f"known profiles: {sorted(REVIEW_COMPOSER_PROFILES)}"
        )
    return composer_type()


# ---------------------------------------------------------------------
# Rendering + the verifier. The rendered summary is the locked
# facts followed by the narrative; verification runs over exactly
# that text, exactly as the memo's verifier runs over the memo.
# ---------------------------------------------------------------------


def render_review_summary(locked_facts: list[str], paragraphs: list[str]) -> str:
    return "\n\n".join([*locked_facts, *paragraphs])


# Prose that denies a fact the locked record states. A composer
# (or a hand) claiming one of these while the fact stands adverse
# is not a style choice — it is a softened record, and the
# verifier refuses it by name.
_DENIAL_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("breach", (
        "no breach", "no breaches", "no covenant breach",
        "breach-free", "without a breach",
    )),
    ("failed test", (
        "all covenant tests passed", "all tests passed",
        "no test failed", "no covenant test failed", "no failures",
    )),
    ("policy exception", (
        "no policy exceptions", "no exceptions",
        "fully compliant", "in full compliance",
    )),
)


def verify_review_summary(
    *,
    paragraphs: list[str],
    locked_facts: list[str],
    inputs: ReviewInputs,
    composer_profile: str,
) -> ReviewSummaryVerification:
    """Verify a review summary against the pack's pinned inputs.

    Three checks, all mechanical:
      1. every figure in the rendered text traces into the
         review universe built from the pinned artefacts;
      2. the locked record — recomputed here from the pinned
         inputs, never taken from the candidate — stands
         verbatim in the rendered text: a dropped or reworded
         locked fact is a softened record;
      3. the narrative carries no denial of a fact the locked
         record states adversely (breaches described as none,
         failed tests as all-passed, open exceptions as full
         compliance).
    Any failure raises MemoVerificationError naming the offenders
    — the caller stores nothing."""
    universe = build_review_universe(inputs)
    canonical_locked = locked_fact_paragraphs(inputs)
    rendered = render_review_summary(locked_facts, paragraphs)
    traces: list[FigureTrace] = []
    offenders: list[str] = []
    for figure in extract_figures(rendered):
        source = universe.get(figure.value)
        if source is None:
            offenders.append(figure.text)
        else:
            traces.append(FigureTrace(
                text=figure.text,
                value=format(figure.value, "f"),
                kind=figure.kind,
                source=source,
                section_key="review_summary",
            ))
    problems: list[str] = []
    if offenders:
        problems.append(
            "untraceable figure(s): " + "; ".join(offenders)
        )
    missing_locked = [p for p in canonical_locked if p not in rendered]
    if missing_locked:
        problems.append(
            f"{len(missing_locked)} locked fact paragraph(s) "
            "missing or reworded in the rendered summary — the "
            "locked record may not be softened"
        )
    adverse = {
        "breach": bool(inputs.breaches or inputs.open_breaches),
        "failed test": any(
            r.outcome == "fail" for r in inputs.results
        ),
        "policy exception": bool(
            inputs.current_report.non_pass_outcomes()
        ),
    }
    narrative = "\n".join(paragraphs).lower()
    for fact, phrases in _DENIAL_PHRASES:
        if not adverse[fact]:
            continue
        for phrase in phrases:
            if phrase in narrative:
                problems.append(
                    f"the narrative denies the {fact} record "
                    f"({phrase!r}) while the locked record states "
                    "it adversely — a softened summary is refused"
                )
    if problems:
        raise MemoVerificationError(
            "review summary refused by the figure verifier — "
            + "; ".join(problems),
            untraceable=offenders,
        )
    return ReviewSummaryVerification(
        composer_profile=composer_profile,
        figure_count=len(traces),
        figures=traces,
        locked_facts_expected=len(canonical_locked),
        locked_facts_present=len(canonical_locked) - len(missing_locked),
        verified=True,
    )


# ---------------------------------------------------------------------
# The stance: validation. The pipeline supplies the facts (the
# pack, the decided memo and its inputs); this function refuses
# or allows, purely.
# ---------------------------------------------------------------------


def validate_review_reasons(reason_codes: list[str], note: str) -> None:
    """Coded reasons from the current vocabulary — or no stance.

    A stance with no codes is a free-text stance, and free-text
    stances are refused: the reviewer (and the record) must be
    able to say exactly why the stance landed where it did."""
    codes = list(reason_codes or [])
    if not codes:
        raise ValidationError(
            "an annual review outcome requires at least one "
            f"coded reason from {REVIEW_VOCABULARY_VERSION}; a "
            "note alone is not a reason"
        )
    unknown = [c for c in codes if c not in REVIEW_REASON_CODES]
    if unknown:
        raise ValidationError(
            f"unknown review reason code(s): {', '.join(unknown)}; "
            f"the vocabulary is {REVIEW_VOCABULARY_VERSION}: "
            f"{', '.join(sorted(REVIEW_REASON_CODES))}"
        )
    if "other" in codes and not (note or "").strip():
        raise ValidationError(
            "review reason 'other' requires a note stating the "
            "reason — 'other' without one is a free-text stance"
        )


def validate_review_outcome(
    *,
    stance: str,
    reason_codes: list[str],
    note: str,
    conditions: list[CommitteeCondition],
    reviewer_id: str,
    reviewer_role: str,
    memo: CreditMemo,
    memo_inputs: MemoInputs,
    countersigner_id: str | None = None,
    countersigner_role: str | None = None,
) -> list[CommitteeCondition]:
    """Validate one review outcome; return its conditions as
    they will be stored (validated + traced, round 7 machinery).

    Refusals: an unknown stance; an unnamed reviewer or one
    without a credit-officer role; no coded reasons, or unknown
    ones; conditions on a stance that carries none, or none on
    affirm_with_conditions; a condition whose threshold does not
    trace or whose cadence cannot be scheduled; and — the
    self-review wall — the decided memo's approving officer
    recording the outcome alone, without a countersigning
    officer who is someone else."""
    require_officer(reviewer_id, reviewer_role, what="an annual review outcome")
    if stance not in REVIEW_STANCES:
        raise ValidationError(
            f"unknown review stance {stance!r}; stances are "
            f"{sorted(REVIEW_STANCES)}"
        )
    validate_review_reasons(reason_codes, note)
    conditions = list(conditions or [])
    stored: list[CommitteeCondition] = []
    if stance == STANCE_AFFIRM_WITH_CONDITIONS:
        if not conditions:
            raise ValidationError(
                "an affirm_with_conditions outcome requires at "
                "least one structured condition: conditions are "
                "records, not prose in a note"
            )
        numbered = [
            c.model_copy(update={
                "condition_id": c.condition_id or f"cond-{i + 1}",
            })
            for i, c in enumerate(conditions)
        ]
        stored = validate_conditions(numbered, memo_inputs)
        for condition in stored:
            if condition.type in (
                "financial_covenant", "reporting_requirement"
            ):
                frequency_from_cadence(
                    condition.due_cadence,
                    what=(
                        f"review condition {condition.condition_id!r}"
                    ),
                )
    elif conditions:
        raise ValidationError(
            f"a {stance} outcome carries no conditions; "
            "conditions belong to affirm_with_conditions"
        )
    if memo.reviewed_by and reviewer_id == memo.reviewed_by:
        if not countersigner_id:
            raise ValidationError(
                f"reviewer {reviewer_id!r} approved the decided "
                f"memo {memo.memo_id} at the credit officer gate: "
                "the approver of a credit does not review it "
                "alone — a countersigning credit officer is "
                "required"
            )
        require_officer(
            countersigner_id, countersigner_role or "",
            what="countersigning an annual review outcome",
        )
        if countersigner_id == reviewer_id:
            raise ValidationError(
                "the countersigner must be a different officer "
                "than the reviewer: a countersignature by the same "
                "person countersigns nothing"
            )
    elif countersigner_id:
        require_officer(
            countersigner_id, countersigner_role or "",
            what="countersigning an annual review outcome",
        )
    return stored
