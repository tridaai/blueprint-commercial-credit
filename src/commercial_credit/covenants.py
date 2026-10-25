"""Covenants: clocks + math (round 10; design law 6).

An approval is not the end of a credit's life; it is the start of
its monitoring life. This module holds the covenant machinery:

- **The template, as data.** The policy pack's standard covenant
  template for each facility type: the financial covenants every
  facility of that type carries (a leverage ceiling tested
  quarterly, a DSCR floor tested annually, a liquidity floor), and
  the standard reporting deliverables. A committee condition can
  tighten a template covenant — same metric, same frequency — and
  the condition's threshold then governs; the record keeps both
  origins visible, it never merges them silently.
- **The set.** Built from one committee decision: the decision's
  financial-covenant conditions plus the template, deduplicated
  by (metric, frequency, basis) with the condition governing.
  Reporting conditions join as dated deliverables alongside the
  template's. The set pins the policy pack version of the memo
  the decision decided, and the decision id it came from.
- **The schedule.** Pure date arithmetic. Test dates are fiscal
  period ends — quarterly, semiannual or annual closes counted
  from the facility's closing date, capped at the facility's
  maturity (closing + term) — plus, for a point-in-time covenant,
  one final test at maturity itself. Every instance names the
  inputs its test needs and stands in a derived state at the
  as_of date. Nothing here reads the wall clock: the as_of is
  always passed in, so the same set at the same date always
  produces the same schedule — which is what makes the evals'
  graded table possible.
- **Amendment discipline.** A covenant's threshold is never
  edited. An amendment mints the next version of the covenant —
  superseding, never rewriting — with a named author, a coded
  reason from the versioned vocabulary below, and the committee
  decision that authorised it. The old version stays on the set's
  record forever: an examiner reads the threshold the borrower
  was actually tested under at any point in the facility's life.

Detection is this module's job; re-spreading, testing and breach
cases are round 11's. Waiver and enforcement are the bank's.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from .errors import NotFoundError, ValidationError
from .memo import _canonical_hash
from .models import (
    FREQUENCY_ANNUAL,
    FREQUENCY_QUARTERLY,
    FREQUENCY_SEMIANNUAL,
    ITEM_COVENANT_TEST,
    ITEM_REPORTING_DELIVERABLE,
    SCHEDULE_AWAITING_INPUTS,
    SCHEDULE_DUE,
    SCHEDULE_DUE_SOON,
    SCHEDULE_OVERDUE,
    SCHEDULE_SCHEDULED,
    CommitteeDecision,
    Covenant,
    CovenantAmendment,
    CovenantSet,
    CovenantTestInstance,
    CreditFile,
    FacilityType,
    ReportingDeliverable,
)

# ---------------------------------------------------------------------
# The computation vocabulary. A covenant may test only a figure the
# engine actually produces: a covenant over an unknown metric could
# never be tested, so it is refused at set creation (and at
# condition time the committee gate already refused metrics outside
# the decided memo's packs).
# ---------------------------------------------------------------------

FIGURE_KEYS = frozenset({
    # entity-pack figures (computations.py)
    "ebitda", "total_debt", "debt_service", "working_capital",
    "current_ratio", "quick_ratio", "tangible_net_worth",
    "debt_to_ebitda", "debt_to_tangible_net_worth", "dscr",
    "fixed_charge_coverage", "gross_margin", "operating_margin",
    "ebitda_margin", "return_on_assets", "return_on_equity",
    "operating_cash_flow", "free_cash_flow", "cash_conversion",
    # the global pack's combined coverage figure
    "global_dscr",
})

FREQUENCIES = frozenset({
    FREQUENCY_QUARTERLY, FREQUENCY_SEMIANNUAL, FREQUENCY_ANNUAL,
})

# Months between tests, per frequency.
_FREQUENCY_MONTHS = {
    FREQUENCY_QUARTERLY: 3,
    FREQUENCY_SEMIANNUAL: 6,
    FREQUENCY_ANNUAL: 12,
}

# An instance stands `due_soon` this many days before its test
# date — the window in which the inputs should be chased.
DUE_SOON_DAYS = 30

OPERATORS = frozenset({">=", "<=", ">", "<"})

# ---------------------------------------------------------------------
# The amendment vocabulary (versioned, like the gates'). A change
# to a covenant's threshold, operator, frequency or grace is a
# credit decision, not an edit: it carries one of these codes, a
# named author, and the committee decision that authorised it.
# ---------------------------------------------------------------------

AMENDMENT_REASONS_VERSION = "covenant-amendment-reasons-v1"

AMENDMENT_REASON_CODES: dict[str, str] = {
    "committee_redecision": (
        "the credit committee re-decided the covenant on "
        "re-presentment of the credit"
    ),
    "borrower_cure_negotiated": (
        "amended as part of a negotiated cure with the borrower"
    ),
    "documentation_correction": (
        "corrects a covenant recorded contrary to the executed "
        "credit agreement"
    ),
    "facility_restructure": (
        "the facility was restructured and the covenant set "
        "re-cut to the restructured terms"
    ),
    "other": "another reason, stated in the amendment note",
}

# Roles that may record an amendment: the same discipline as the
# committee gate — the preparer's side of the house does not amend
# covenants. The authorisation itself is the committee decision
# the amendment names; this role check is the floor, not the proof.
AMENDMENT_AUTHOR_ROLES = frozenset({
    "committee_chair",
    "committee_member",
    "credit_officer",
    "senior_credit_officer",
    "chief_credit_officer",
})

# ---------------------------------------------------------------------
# The standard covenant template, as data. One entry per financial
# covenant: the metric, operator, threshold, cadence and basis the
# policy expects for the facility type, and the policy citation
# the entry encodes. Reporting deliverables likewise. Facility
# types absent from the template carry no standard financial
# covenants (their committee conditions still schedule).
# ---------------------------------------------------------------------

_CITATION_MONITORING = (
    "Aldergrove Commercial Lending Policy §7.1 (covenant "
    "monitoring: quarterly leverage and liquidity testing)"
)
_CITATION_ANNUAL = (
    "Aldergrove Commercial Lending Policy §7.2 (annual covenant "
    "testing on audited fiscal-year figures)"
)
_CITATION_REPORTING = (
    "Aldergrove Commercial Lending Policy §7.3 (post-close "
    "reporting: audited annual statements within 90 days of "
    "fiscal year end)"
)

_BASE_FINANCIAL: tuple[dict, ...] = (
    {
        "template_id": "tpl-max-leverage-quarterly",
        "name": "Maximum leverage (total liabilities / tangible net worth)",
        "metric_key": "debt_to_tangible_net_worth",
        "operator": "<=",
        "threshold": "2.50",
        "frequency": FREQUENCY_QUARTERLY,
        "basis": "fiscal_period_end",
        "grace_days": 30,
        "citation": _CITATION_MONITORING,
    },
    {
        "template_id": "tpl-min-dscr-annual",
        "name": "Minimum debt service coverage",
        "metric_key": "dscr",
        "operator": ">=",
        "threshold": "1.25",
        "frequency": FREQUENCY_ANNUAL,
        "basis": "fiscal_period_end",
        "grace_days": 45,
        "citation": _CITATION_ANNUAL,
    },
    {
        "template_id": "tpl-min-current-ratio-quarterly",
        "name": "Minimum current ratio",
        "metric_key": "current_ratio",
        "operator": ">=",
        "threshold": "1.20",
        "frequency": FREQUENCY_QUARTERLY,
        "basis": "point_in_time",
        "grace_days": 30,
        "citation": _CITATION_MONITORING,
    },
)

_BASE_REPORTING: tuple[dict, ...] = (
    {
        "template_id": "tpl-annual-audited-statements",
        "name": "Annual audited financial statements",
        "report_what": (
            "Audited annual financial statements for the fiscal "
            "year just ended"
        ),
        "frequency": FREQUENCY_ANNUAL,
        "citation": _CITATION_REPORTING,
    },
)

# Facility-specific tuning of the base template: CRE carries a
# tighter DSCR floor on the annual test (the property's coverage
# is the credit), construction a tighter leverage ceiling while
# the project is unstabilised. Overrides replace the base entry's
# fields by template id; everything else carries through.
_FACILITY_OVERRIDES: dict[str, dict[str, dict]] = {
    FacilityType.CRE_LOAN.value: {
        "tpl-min-dscr-annual": {"threshold": "1.35"},
    },
    FacilityType.CONSTRUCTION_LOAN.value: {
        "tpl-max-leverage-quarterly": {"threshold": "2.00"},
    },
}

_NO_STANDARD_FINANCIAL = frozenset({FacilityType.SBA_7A.value})


def covenant_template(facility_type: str) -> tuple[list[dict], list[dict]]:
    """The standard template for one facility type, as data:
    (financial covenant entries, reporting deliverable entries)."""
    if facility_type in _NO_STANDARD_FINANCIAL:
        financial: list[dict] = []
    else:
        overrides = _FACILITY_OVERRIDES.get(facility_type, {})
        financial = []
        for entry in _BASE_FINANCIAL:
            merged = dict(entry)
            merged.update(overrides.get(entry["template_id"], {}))
            financial.append(merged)
    return financial, [dict(e) for e in _BASE_REPORTING]


# ---------------------------------------------------------------------
# Date arithmetic. Pure: every function takes its dates as
# arguments. add_months clamps the day to the target month's last
# day (Jan 31 + 1 month is Feb 28/29, never Mar 3).
# ---------------------------------------------------------------------


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def fiscal_year_end(year: int, *, month: int = 12, day: int = 31) -> date:
    """The fiscal year end of `year` for a borrower whose fiscal
    year ends on (month, day) — clamped to the month's real last
    day, so a Feb 29 fiscal year end exists only in leap years."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def maturity_date(closing: date, term_months: int) -> date:
    """The facility's maturity: closing + term."""
    return add_months(closing, term_months)


def _stepped_period_end(
    year: int, *, month: int, day: int, months_back: int
) -> date:
    """The fiscal period end `months_back` months before the fiscal
    year end of (month, day) in `year`. Computed on the (year,
    month) lattice with the day clamped to the target month's last
    day — never by day-stepping, which drifts (Jun 30 stepped back
    3 months by day-count lands on Mar 30; the quarter end is
    Mar 31)."""
    index = year * 12 + (month - 1) - months_back
    target_year, target_month0 = divmod(index, 12)
    target_month = target_month0 + 1
    last_day = calendar.monthrange(target_year, target_month)[1]
    return date(target_year, target_month, min(day, last_day))


def _period_ends_after(
    anchor: date, *, step_months: int, month: int, day: int, limit: date
) -> list[tuple[date, int]]:
    """Fiscal period ends strictly after `anchor`, at the cadence
    the frequency names, up to and including `limit`. Returns
    (period_end, fiscal_year) pairs, sorted. Period ends live on a
    lattice: the fiscal year ends of successive years, each
    stepped back by the cadence — quarterly tests land on the
    quarter ends preceding and including each fiscal year end.
    The generating year names the fiscal year: a period end
    belongs to the fiscal year whose year end follows it (a Sep
    2026 quarter end belongs to the June-2027 fiscal year of a
    June-FYE borrower)."""
    ends: list[tuple[date, int]] = []
    seen: set[date] = set()
    for year in range(anchor.year - 1, limit.year + 2):
        for months_back in range(0, 12, step_months):
            period_end = _stepped_period_end(
                year, month=month, day=day, months_back=months_back
            )
            if anchor < period_end <= limit and period_end not in seen:
                seen.add(period_end)
                ends.append((period_end, year))
    ends.sort()
    return ends


def covenant_test_dates(
    first_basis: date,
    *,
    frequency: str,
    basis: str,
    term_months: int,
    fiscal_month: int = 12,
    fiscal_day: int = 31,
) -> list[tuple[date, int | None]]:
    """Every test date of one covenant over the facility's life.

    For a `fiscal_period_end` covenant: the fiscal period ends at
    the covenant's cadence, strictly after closing, up to and
    including maturity — each with the fiscal year it closes.
    For a `point_in_time` covenant: the same period ends as
    measurement dates, plus a final test at maturity itself when
    maturity is not already a period end (a facility that matures
    mid-quarter is still tested at the end)."""
    maturity = maturity_date(first_basis, term_months)
    step = _FREQUENCY_MONTHS[frequency]
    ends = _period_ends_after(
        first_basis,
        step_months=step,
        month=fiscal_month,
        day=fiscal_day,
        limit=maturity,
    )
    dates: list[tuple[date, int | None]] = list(ends)
    if basis == "point_in_time":
        if all(end != maturity for end, _ in ends):
            dates.append((maturity, None))
        dates.sort(key=lambda pair: pair[0])
    return dates


def deliverable_due_dates(
    closing: date,
    *,
    frequency: str,
    term_months: int,
    fiscal_month: int = 12,
    fiscal_day: int = 31,
) -> list[tuple[date, int]]:
    """Due dates of one reporting deliverable: each fiscal period
    end at the deliverable's cadence, plus the reporting lag the
    policy allows (annual statements: 90 days after fiscal year
    end; quarterly: 45; semiannual: 60), up to maturity. The final
    period that ends AT maturity still carries its deliverable —
    it falls due after maturity, and the bank still expects it."""
    lag_days = {FREQUENCY_ANNUAL: 90, FREQUENCY_SEMIANNUAL: 60,
                FREQUENCY_QUARTERLY: 45}[frequency]
    maturity = maturity_date(closing, term_months)
    step = _FREQUENCY_MONTHS[frequency]
    ends = _period_ends_after(
        closing,
        step_months=step,
        month=fiscal_month,
        day=fiscal_day,
        limit=maturity,
    )
    return [
        (period_end + timedelta(days=lag_days), fiscal_year)
        for period_end, fiscal_year in ends
    ]


# ---------------------------------------------------------------------
# Set construction. Pure over (credit file, decision, conditions):
# the pipeline supplies the decision's stored conditions (already
# validated and traced at the committee gate) and stores the set.
# ---------------------------------------------------------------------

_CONDITION_FREQUENCY = {
    "quarterly": FREQUENCY_QUARTERLY,
    "semiannual": FREQUENCY_SEMIANNUAL,
    "semi-annually": FREQUENCY_SEMIANNUAL,
    "annual": FREQUENCY_ANNUAL,
    "annually": FREQUENCY_ANNUAL,
    "yearly": FREQUENCY_ANNUAL,
}


def frequency_from_cadence(cadence: str, *, what: str) -> str:
    """Read a committee condition's free due_cadence as a testing
    frequency. The cadence is data the committee recorded; the
    scheduler must be able to read it as one of the three
    frequencies, or the condition cannot be scheduled and the set
    refuses it by name — an unschedulable covenant is not quietly
    dropped from monitoring."""
    text = (cadence or "").strip().lower()
    for token, frequency in _CONDITION_FREQUENCY.items():
        if token in text:
            return frequency
    raise ValidationError(
        f"{what} names due cadence {cadence!r}, which the "
        "covenant scheduler cannot read as quarterly, semiannual "
        "or annual: a condition that cannot be scheduled cannot "
        "enter a covenant set"
    )


def build_covenant_set(
    credit_file: CreditFile,
    decision: CommitteeDecision,
    *,
    policy_version: str,
    policy_pack_id: str,
    created_by: str,
) -> CovenantSet:
    """Build the covenant set one approval creates.

    Sources, in governing order:
      1. the decision's financial-covenant conditions — the
         committee's own terms for this credit, thresholds
         already traced at the gate;
      2. the policy template for the facility type — a condition
         on the same (metric, frequency, basis) governs over the
         template entry, which is then NOT duplicated: the set
         carries the condition's covenant, and the template entry
         it displaced is named in the audit by the pipeline.

    A condition whose metric is outside the computation
    vocabulary, whose operator is unknown, or whose cadence the
    scheduler cannot read refuses the whole build — a set that
    silently dropped a committee condition would be worse than no
    set. The set is `awaiting_closing_date` when the file records
    no closing date: covenants exist, their schedule does not —
    yet."""
    if decision.outcome not in ("approve", "approve_with_conditions"):
        raise ValidationError(
            f"decision {decision.decision_id} is "
            f"{decision.outcome!r}: a covenant set is created by "
            "an approval, never by a decline or a deferral"
        )
    closing = credit_file.closing_date
    descriptor: dict = {
        "credit_file_id": credit_file.credit_file_id,
        "decision_id": decision.decision_id,
        "memo_id": decision.memo_id,
        "policy_pack_id": policy_pack_id,
        "policy_version": policy_version,
        "closing_date": closing.isoformat() if closing else None,
        "term_months": credit_file.facility.term_months,
    }
    set_id = f"covset-{_canonical_hash(descriptor)[:16]}"

    def covenant_id(origin: str) -> str:
        return f"cov-{_canonical_hash({'set': set_id, 'origin': origin})[:16]}"

    conditions = [
        c for c in decision.conditions if c.type == "financial_covenant"
    ]
    reporting = [
        c for c in decision.conditions if c.type == "reporting_requirement"
    ]
    covenants: list[Covenant] = []
    governed: set[tuple[str, str, str]] = set()
    for condition in conditions:
        if condition.metric_key not in FIGURE_KEYS:
            raise ValidationError(
                f"committee condition {condition.condition_id!r} "
                f"sets a covenant on metric "
                f"{condition.metric_key!r}, which is not a figure "
                "the computation engine produces: the covenant "
                "could never be tested, and the set refuses it"
            )
        if condition.operator not in OPERATORS:
            raise ValidationError(
                f"committee condition {condition.condition_id!r} "
                f"has operator {condition.operator!r}; operators "
                f"are {sorted(OPERATORS)}"
            )
        if condition.threshold is None:
            raise ValidationError(
                f"committee condition {condition.condition_id!r} "
                "is a financial covenant without a threshold"
            )
        frequency = frequency_from_cadence(
            condition.due_cadence,
            what=f"committee condition {condition.condition_id!r}",
        )
        governed.add((condition.metric_key, frequency, "fiscal_period_end"))
        covenants.append(Covenant(
            covenant_id=covenant_id(f"condition:{condition.condition_id}"),
            set_id=set_id,
            credit_file_id=credit_file.credit_file_id,
            name=(
                f"Committee covenant: {condition.metric_key} "
                f"{condition.operator} {condition.threshold}"
            ),
            metric_key=condition.metric_key,
            operator=condition.operator,
            threshold=condition.threshold,
            threshold_trace=condition.threshold_trace,
            frequency=frequency,
            basis="fiscal_period_end",
            grace_days=30,
            source="committee_condition",
            origin_id=condition.condition_id,
        ))

    template_financial, template_reporting = covenant_template(
        credit_file.facility.facility_type.value
    )
    for entry in template_financial:
        key = (entry["metric_key"], entry["frequency"], entry["basis"])
        if key in governed:
            continue  # the committee's term governs; not duplicated
        covenants.append(Covenant(
            covenant_id=covenant_id(f"template:{entry['template_id']}"),
            set_id=set_id,
            credit_file_id=credit_file.credit_file_id,
            name=entry["name"],
            metric_key=entry["metric_key"],
            operator=entry["operator"],
            threshold=Decimal(entry["threshold"]),
            frequency=entry["frequency"],
            basis=entry["basis"],
            grace_days=entry["grace_days"],
            source="policy_template",
            origin_id=entry["template_id"],
        ))

    deliverables: list[ReportingDeliverable] = []
    for condition in reporting:
        frequency = frequency_from_cadence(
            condition.due_cadence,
            what=f"committee condition {condition.condition_id!r}",
        )
        deliverables.append(ReportingDeliverable(
            deliverable_id=(
                "del-" + _canonical_hash({
                    "set": set_id,
                    "origin": f"condition:{condition.condition_id}",
                })[:16]
            ),
            set_id=set_id,
            credit_file_id=credit_file.credit_file_id,
            name=f"Committee reporting: {condition.report_what}",
            report_what=condition.report_what or "",
            frequency=frequency,
            source="committee_condition",
            origin_id=condition.condition_id,
        ))
    for entry in template_reporting:
        deliverables.append(ReportingDeliverable(
            deliverable_id=(
                "del-" + _canonical_hash({
                    "set": set_id,
                    "origin": f"template:{entry['template_id']}",
                })[:16]
            ),
            set_id=set_id,
            credit_file_id=credit_file.credit_file_id,
            name=entry["name"],
            report_what=entry["report_what"],
            frequency=entry["frequency"],
            source="policy_template",
            origin_id=entry["template_id"],
        ))

    return CovenantSet(
        set_id=set_id,
        credit_file_id=credit_file.credit_file_id,
        decision_id=decision.decision_id,
        memo_id=decision.memo_id,
        policy_pack_id=policy_pack_id,
        policy_version=policy_version,
        state="active" if closing is not None else "awaiting_closing_date",
        closing_date=closing,
        term_months=credit_file.facility.term_months,
        covenants=covenants,
        deliverables=deliverables,
        created_by=created_by,
    )


def current_covenants(
    covenant_set: CovenantSet, amendments: list[CovenantAmendment]
) -> list[Covenant]:
    """The set's current covenants: the latest version of each
    lineage, in the set's original order (an amendment takes its
    predecessor's place in the reading order)."""
    latest: dict[str, Covenant] = {}
    order: list[str] = []
    for covenant in covenant_set.covenants:
        latest[covenant.covenant_id] = covenant
        order.append(covenant.covenant_id)
    by_superseded = {a.superseded_covenant_id: a.covenant for a in amendments}
    resolved: list[Covenant] = []
    for root_id in order:
        current = latest[root_id]
        while current.covenant_id in by_superseded:
            current = by_superseded[current.covenant_id]
        resolved.append(current)
    return resolved


# ---------------------------------------------------------------------
# The schedule. Derived, pure, and the queue's single source.
# ---------------------------------------------------------------------


def required_inputs_for(
    item: Covenant | ReportingDeliverable,
    fiscal_year: int | None,
) -> list[str]:
    """The spread inputs one scheduled item needs, named.

    A fiscal-period test needs that period's spread figures, which
    arrive with the period's financial statements; a point-in-time
    test needs the latest spread at its date; a deliverable needs
    the document it names. The names are the requirement, stated
    so the queue can say what is being waited for."""
    if isinstance(item, ReportingDeliverable):
        return [
            f"document: {item.report_what} "
            f"(FY{fiscal_year})" if fiscal_year else
            f"document: {item.report_what}"
        ]
    if item.basis == "point_in_time":
        return [
            f"spread figures at {item.name.split(':')[0].strip().lower()} "
            "measurement date (latest fiscal period spread)"
        ]
    return [
        f"FY{fiscal_year} financial statements, spread "
        f"({item.metric_key} for FY{fiscal_year})"
    ]


def derive_schedule(
    covenant_set: CovenantSet,
    *,
    as_of: date,
    spread_years: frozenset[int] | set[int] = frozenset(),
) -> list[CovenantTestInstance]:
    """Every scheduled item of the set at `as_of`, derived.

    `spread_years` is the set of fiscal years the file's current
    spread covers — the fact that decides whether a period test
    stands at awaiting_inputs. The derivation is total and pure:
    same set, same spread years, same as_of ⇒ same instances, ids
    included. An `awaiting_closing_date` set derives nothing: its
    covenants exist, its dates do not."""
    if covenant_set.state != "active" or covenant_set.closing_date is None:
        return []
    closing = covenant_set.closing_date
    instances: list[CovenantTestInstance] = []

    def state_for(
        test_date: date,
        inputs_ready: bool,
        grace_days: int,
        *,
        missing_dominates: bool = True,
    ) -> tuple[str, int]:
        """The derived state of one instance at `as_of`.

        For a covenant test, missing inputs dominate once the
        chasing window opens: a test whose period figures are not
        on file stands at awaiting_inputs — it cannot be run, and
        calling it overdue would blame the calendar for a missing
        document. For a reporting deliverable, the missing thing
        IS the obligation, so the date states stand (due, then
        overdue) and the queue names the document."""
        days = (test_date - as_of).days
        if (
            not inputs_ready and missing_dominates
            and as_of >= test_date - timedelta(days=DUE_SOON_DAYS)
        ):
            return SCHEDULE_AWAITING_INPUTS, days
        if as_of > test_date + timedelta(days=grace_days):
            return SCHEDULE_OVERDUE, days
        if as_of >= test_date:
            return SCHEDULE_DUE, days
        if days <= DUE_SOON_DAYS:
            return SCHEDULE_DUE_SOON, days
        return SCHEDULE_SCHEDULED, days

    for covenant in covenant_set.covenants:
        dates = covenant_test_dates(
            closing,
            frequency=covenant.frequency,
            basis=covenant.basis,
            term_months=covenant_set.term_months,
            fiscal_month=covenant_set.fiscal_year_end_month,
            fiscal_day=covenant_set.fiscal_year_end_day,
        )
        for test_date, fiscal_year in dates:
            if covenant.basis == "point_in_time":
                earlier = [y for y in spread_years if _fy_end(
                    y, covenant_set) <= test_date]
                ready = bool(earlier)
                period_label = (
                    f"FY{max(earlier)}" if earlier else "point-in-time"
                )
                period_year = max(earlier) if earlier else None
                inputs = [] if ready else required_inputs_for(
                    covenant, None
                )
            else:
                ready = fiscal_year in spread_years
                period_label = f"FY{fiscal_year}"
                period_year = fiscal_year
                inputs = [] if ready else required_inputs_for(
                    covenant, fiscal_year
                )
            state, days = state_for(test_date, ready, covenant.grace_days)
            instances.append(CovenantTestInstance(
                instance_id=(
                    f"covi-{_canonical_hash({'set': covenant_set.set_id, 'covenant': covenant.covenant_id, 'date': test_date.isoformat()})[:16]}"
                ),
                credit_file_id=covenant_set.credit_file_id,
                set_id=covenant_set.set_id,
                kind=ITEM_COVENANT_TEST,
                covenant_id=covenant.covenant_id,
                name=covenant.name,
                metric_key=covenant.metric_key,
                operator=covenant.operator,
                threshold=covenant.threshold,
                test_date=test_date,
                period_label=period_label,
                fiscal_year=period_year,
                state=state,
                days_until_due=days,
                # The inputs are named whenever they are missing
                # and the date is close enough to matter; a ready
                # instance names none — its figures are on file.
                required_inputs=(
                    inputs if state != SCHEDULE_SCHEDULED else []
                ),
            ))
    for deliverable in covenant_set.deliverables:
        dates = deliverable_due_dates(
            closing,
            frequency=deliverable.frequency,
            term_months=covenant_set.term_months,
            fiscal_month=covenant_set.fiscal_year_end_month,
            fiscal_day=covenant_set.fiscal_year_end_day,
        )
        for due_date, fiscal_year in dates:
            if fiscal_year in spread_years:
                # The statements this deliverable exists to
                # collect are already spread: the obligation is
                # met and leaves the schedule. (Receipt is a
                # delivery, unlike a covenant test, whose figures
                # being on file makes it testable, not satisfied.
                # The receipt record itself is round 11's.)
                continue
            state, days = state_for(
                due_date, False, 0, missing_dominates=False
            )
            instances.append(CovenantTestInstance(
                instance_id=(
                    f"covi-{_canonical_hash({'set': covenant_set.set_id, 'deliverable': deliverable.deliverable_id, 'date': due_date.isoformat()})[:16]}"
                ),
                credit_file_id=covenant_set.credit_file_id,
                set_id=covenant_set.set_id,
                kind=ITEM_REPORTING_DELIVERABLE,
                deliverable_id=deliverable.deliverable_id,
                name=deliverable.name,
                test_date=due_date,
                period_label=f"FY{fiscal_year}",
                fiscal_year=fiscal_year,
                state=state,
                days_until_due=days,
                required_inputs=(
                    required_inputs_for(deliverable, fiscal_year)
                    if state != SCHEDULE_SCHEDULED else []
                ),
            ))
    instances.sort(key=lambda i: (i.test_date, i.name))
    return instances


def _fy_end(year: int, covenant_set: CovenantSet) -> date:
    return fiscal_year_end(
        year,
        month=covenant_set.fiscal_year_end_month,
        day=covenant_set.fiscal_year_end_day,
    )


# States in which an instance is work: it needs the bank's
# attention now, or its inputs chased now.
WORK_STATES = frozenset({
    SCHEDULE_OVERDUE, SCHEDULE_DUE, SCHEDULE_AWAITING_INPUTS,
    SCHEDULE_DUE_SOON,
})

_STATE_ORDER = {
    SCHEDULE_OVERDUE: 0,
    SCHEDULE_DUE: 1,
    SCHEDULE_AWAITING_INPUTS: 2,
    SCHEDULE_DUE_SOON: 3,
    SCHEDULE_SCHEDULED: 4,
}


def due_work(
    instances: list[CovenantTestInstance], *, as_of: date
) -> list[CovenantTestInstance]:
    """The due-work queue's ordering: overdue first (most overdue
    first), then due, then awaiting inputs, then due soon — each
    naming the inputs it needs where it needs any."""
    work = [i for i in instances if i.state in WORK_STATES]
    return sorted(
        work,
        key=lambda i: (_STATE_ORDER[i.state], i.test_date, i.name),
    )


# ---------------------------------------------------------------------
# Amendment validation. The act itself is recorded by the
# pipeline; the discipline — who, why coded, authorised by which
# decision, changing what — is checked here, purely.
# ---------------------------------------------------------------------


def validate_amendment(
    current: Covenant,
    *,
    author_id: str,
    author_role: str,
    reason_code: str,
    note: str,
    decision: CommitteeDecision | None,
    new_threshold: Decimal | None,
    new_operator: str | None,
    new_frequency: str | None,
    new_grace_days: int | None,
) -> Covenant:
    """Mint the next version of a covenant, or refuse.

    Refusals: an unnamed or unroled author; an author whose role
    may not amend; an uncoded reason (or 'other' without a note);
    no authorising committee decision, or one that is not an
    approval of a memo on this credit file; a change that changes
    nothing; a new frequency the schedule cannot read. The minted
    covenant supersedes `current` and names all of it."""
    if not author_id or not author_id.strip():
        raise ValidationError(
            "a covenant amendment names its author: an anonymous "
            "amendment is refused"
        )
    if author_role not in AMENDMENT_AUTHOR_ROLES:
        raise ValidationError(
            f"author {author_id!r} carries role {author_role!r}, "
            f"which may not amend covenants; roles are "
            f"{sorted(AMENDMENT_AUTHOR_ROLES)}"
        )
    if reason_code not in AMENDMENT_REASON_CODES:
        raise ValidationError(
            f"unknown amendment reason code {reason_code!r}; the "
            f"vocabulary is {AMENDMENT_REASONS_VERSION}: "
            f"{', '.join(sorted(AMENDMENT_REASON_CODES))}"
        )
    if reason_code == "other" and not (note or "").strip():
        raise ValidationError(
            "amendment reason 'other' requires a note stating the "
            "reason"
        )
    if decision is None:
        raise ValidationError(
            "a covenant amendment requires the committee decision "
            "that authorised it: covenants change by decision, "
            "not by edit"
        )
    if decision.credit_file_id != current.credit_file_id:
        raise ValidationError(
            f"authorising decision {decision.decision_id} belongs "
            f"to credit file {decision.credit_file_id}, not "
            f"{current.credit_file_id}"
        )
    if decision.outcome not in ("approve", "approve_with_conditions"):
        raise ValidationError(
            f"authorising decision {decision.decision_id} is "
            f"{decision.outcome!r}: only an approval can authorise "
            "a covenant amendment"
        )
    threshold = new_threshold if new_threshold is not None else current.threshold
    operator = new_operator if new_operator is not None else current.operator
    frequency = new_frequency if new_frequency is not None else current.frequency
    grace = new_grace_days if new_grace_days is not None else current.grace_days
    if operator not in OPERATORS:
        raise ValidationError(
            f"amended operator {operator!r} is not one of "
            f"{sorted(OPERATORS)}"
        )
    if frequency not in FREQUENCIES:
        raise ValidationError(
            f"amended frequency {frequency!r} is not one of "
            f"{sorted(FREQUENCIES)}"
        )
    if grace < 0:
        raise ValidationError("an amended grace period cannot be negative")
    if (
        threshold == current.threshold
        and operator == current.operator
        and frequency == current.frequency
        and grace == current.grace_days
    ):
        raise ValidationError(
            f"the amendment of covenant {current.covenant_id} "
            "changes nothing — threshold, operator, frequency "
            "and grace all stand as recorded: a no-op amendment "
            "is refused, the record is not churned"
        )
    descriptor = {
        "supersedes": current.covenant_id,
        "version": current.version + 1,
        "threshold": str(threshold),
        "operator": operator,
        "frequency": frequency,
        "grace_days": grace,
        "decision_id": decision.decision_id,
    }
    return current.model_copy(update={
        "covenant_id": f"cov-{_canonical_hash(descriptor)[:16]}",
        "threshold": threshold,
        "operator": operator,
        "frequency": frequency,
        "grace_days": grace,
        "version": current.version + 1,
        "supersedes_covenant_id": current.covenant_id,
        "amended_by": author_id,
        "amendment_reason_code": reason_code,
        "amendment_decision_id": decision.decision_id,
        "amendment_note": note or "",
        "created_at": current.created_at,
    })


def amendment_record(
    covenant_set: CovenantSet,
    superseded: Covenant,
    amended: Covenant,
    *,
    author_id: str,
    author_role: str,
    reason_code: str,
    decision_id: str,
    note: str = "",
) -> CovenantAmendment:
    """The amendment artefact for a validated amendment."""
    descriptor = {
        "set": covenant_set.set_id,
        "superseded": superseded.covenant_id,
        "covenant": amended.covenant_id,
        "decision": decision_id,
    }
    return CovenantAmendment(
        amendment_id=f"amend-{_canonical_hash(descriptor)[:16]}",
        credit_file_id=covenant_set.credit_file_id,
        set_id=covenant_set.set_id,
        superseded_covenant_id=superseded.covenant_id,
        covenant=amended,
        author_id=author_id,
        author_role=author_role,
        reason_code=reason_code,
        decision_id=decision_id,
        note=note,
    )


def get_covenant_or_raise(
    covenant_set: CovenantSet,
    amendments: list[CovenantAmendment],
    covenant_id: str,
) -> Covenant:
    """The CURRENT version of a covenant by any of its lineage
    ids — amending by a superseded id amends the current version;
    the lineage is walked, never forked."""
    by_any_id: dict[str, Covenant] = {}
    for covenant in covenant_set.covenants:
        by_any_id[covenant.covenant_id] = covenant
    for amendment in amendments:
        by_any_id[amendment.covenant.covenant_id] = amendment.covenant
    if covenant_id not in by_any_id:
        raise NotFoundError(
            f"covenant {covenant_id} not found on set "
            f"{covenant_set.set_id}"
        )
    for covenant in current_covenants(covenant_set, amendments):
        # Walk this current version's lineage back to its root;
        # the queried id names the lineage wherever it sits.
        lineage_ids = {covenant.covenant_id}
        node = covenant
        seen: set[str] = set()
        while node.supersedes_covenant_id and node.supersedes_covenant_id not in seen:
            seen.add(node.supersedes_covenant_id)
            lineage_ids.add(node.supersedes_covenant_id)
            parent = by_any_id.get(node.supersedes_covenant_id)
            if parent is None:
                break
            node = parent
        if covenant_id in lineage_ids:
            return covenant
    raise NotFoundError(  # pragma: no cover - defensive
        f"covenant {covenant_id} not found on set {covenant_set.set_id}"
    )
