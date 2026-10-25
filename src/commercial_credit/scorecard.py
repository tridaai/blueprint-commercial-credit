"""The risk-rating scorecard: a coded stand-in, honestly labelled
(round 13).

A bank's obligor rating comes out of the bank's own rating model,
and that model sits under model governance of the SR 11-7 kind —
an inventory entry, independent validation, ongoing monitoring.
This blueprint is not that model, and says so wherever the rating
appears: what it ships is a **coded stand-in** — a versioned
scorecard definition held as data (factors, bands, weights, the
grade scale) plus the artefact discipline around it — so the
workflow a real model would plug into can be built and graded
end to end:

- **Every input traces.** A factor reads either a figure from a
  stored computation pack — recorded with the pack id, the
  figure key and period, and the step keys of the figure's own
  derivation — or a coded fact of the file derived in code from
  stored records (covenant test results, breach cases, the
  spread's period sources and disagreements), recorded with the
  records it counted. There is no third kind of input, and no
  model opinion anywhere in the module: the scoring is pure
  functions over stored artefacts.
- **Missing data is a named band, never a zero.** A figure that
  is not computable lands its factor in the factor's missing-
  data band, with the missing inputs named on the assignment.
  The factor set is fixed by the definition — a factor is never
  skipped, and its weight is never redistributed.
- **The composite is arithmetic on the record.** Factor scores
  (1 strongest, 10 weakest) combine by the definition's weights
  in Decimal; the composite is recorded to two decimals; the
  grade is the composite rounded half-up onto the 1–10 scale,
  named by the definition's grade bands.
- **An override is a governed act, not an edit.** A named credit
  officer may move the FINAL grade at most one grade from the
  assigned grade, for a coded reason from the versioned
  vocabulary, countersigned by a second officer. The assignment
  artefact is never touched; the override is a separate artefact
  beside it, and every surface shows both grades.
- **The rating decides nothing.** It is an input: the memo
  states it in its header, the annual review pack carries it
  with its factor breakdown. No gate reads it. The committee
  approves or declines on the memo and the evidence, exactly as
  it did before this round existed.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .errors import ValidationError
from .memo import _canonical_hash
from .models import (
    BREACH_TERMINAL_STATES,
    RESULT_CURRENT,
    TEST_AWAITING_INPUTS,
    TEST_FAIL,
    TEST_NOT_EVALUABLE,
    BreachCase,
    ComputationPack,
    ComputedFigure,
    CovenantTestResult,
    CreditFile,
    RatingAssignment,
    RatingFactorScore,
    RatingOverride,
    ScorecardBand,
    ScorecardDefinition,
    ScorecardFactorDef,
    ScorecardGradeBand,
    Spread,
)
from .officer_gate import require_officer

SCORECARD_ID = "aldergrove-scorecard"
CURRENT_SCORECARD_VERSION = "1"

# The override vocabulary, versioned like the gates': an override
# recorded under this version names these codes, and a later
# vocabulary change never reinterprets a recorded override.
# `other` is a code like any other, but only lawful with a note.
OVERRIDE_VOCABULARY_VERSION = "rating-override-reasons-v1"

OVERRIDE_REASON_CODES: dict[str, str] = {
    "management_quality_not_in_figures": (
        "management quality and track record the statements do "
        "not capture"
    ),
    "documented_forward_information": (
        "documented forward information — signed contracts, an "
        "order book — not yet in the statements"
    ),
    "collateral_strength": (
        "collateral coverage stronger than the scorecard sees"
    ),
    "documented_adverse_information": (
        "documented adverse information not yet in the statements"
    ),
    "data_quality_concern": (
        "the inputs the scorecard read are doubted on documented "
        "grounds"
    ),
    "other": "another reason, stated in the override note",
}

_CENT = Decimal("0.01")


def _band(band_id: str, label: str, score: int, **kwargs) -> ScorecardBand:
    return ScorecardBand(band_id=band_id, label=label, score=score, **kwargs)


def _missing_band(factor_id: str) -> ScorecardBand:
    return _band(
        f"{factor_id}_insufficient_data",
        "input not computable — scored as insufficient data, "
        "with the missing inputs named",
        8,
        is_missing_data=True,
    )


def _v1_definition() -> ScorecardDefinition:
    """The shipped stand-in, version 1.

    Bands are listed strongest first and matched in order.
    Weights sum to exactly 1 — `validate_scorecard_definition`
    refuses a definition that does not, at registration."""
    return ScorecardDefinition(
        scorecard_id=SCORECARD_ID,
        version="1",
        note=(
            "Coded stand-in for Aldergrove Bank's own obligor "
            "rating model, shipped so the rating workflow — "
            "traced inputs, governed overrides, kept history — "
            "can be exercised end to end. A deployment registers "
            "the bank's governed model definition in its place; "
            "the artefact discipline around it does not change."
        ),
        factors=[
            ScorecardFactorDef(
                factor_id="leverage",
                name="Leverage — total liabilities / tangible net worth",
                kind="figure",
                figure_key="debt_to_tangible_net_worth",
                weight=Decimal("0.20"),
                bands=[
                    _band("leverage_at_most_1_00", "at or below 1.00×", 1, at_most=Decimal("1.00")),
                    _band("leverage_at_most_1_50", "at or below 1.50×", 2, at_most=Decimal("1.50")),
                    _band("leverage_at_most_2_00", "at or below 2.00×", 3, at_most=Decimal("2.00")),
                    _band("leverage_at_most_2_50", "at or below 2.50×", 4, at_most=Decimal("2.50")),
                    _band("leverage_at_most_3_00", "at or below 3.00×", 5, at_most=Decimal("3.00")),
                    _band("leverage_at_most_3_50", "at or below 3.50×", 6, at_most=Decimal("3.50")),
                    _band("leverage_at_most_4_00", "at or below 4.00×", 7, at_most=Decimal("4.00")),
                    _band("leverage_at_most_5_00", "at or below 5.00×", 8, at_most=Decimal("5.00")),
                    _band("leverage_at_most_6_50", "at or below 6.50×", 9, at_most=Decimal("6.50")),
                    _band("leverage_above_6_50", "above 6.50×", 10),
                    _missing_band("leverage"),
                ],
            ),
            ScorecardFactorDef(
                factor_id="dscr",
                name="Debt service coverage",
                kind="figure",
                figure_key="dscr",
                weight=Decimal("0.25"),
                bands=[
                    _band("dscr_at_least_2_00", "at or above 2.00×", 1, at_least=Decimal("2.00")),
                    _band("dscr_at_least_1_75", "at or above 1.75×", 2, at_least=Decimal("1.75")),
                    _band("dscr_at_least_1_50", "at or above 1.50×", 3, at_least=Decimal("1.50")),
                    _band("dscr_at_least_1_35", "at or above 1.35×", 4, at_least=Decimal("1.35")),
                    _band("dscr_at_least_1_25", "at or above 1.25×", 5, at_least=Decimal("1.25")),
                    _band("dscr_at_least_1_15", "at or above 1.15×", 6, at_least=Decimal("1.15")),
                    _band("dscr_at_least_1_05", "at or above 1.05×", 7, at_least=Decimal("1.05")),
                    _band("dscr_at_least_1_00", "at or above 1.00×", 8, at_least=Decimal("1.00")),
                    _band("dscr_at_least_0_90", "at or above 0.90×", 9, at_least=Decimal("0.90")),
                    _band("dscr_below_0_90", "below 0.90×", 10),
                    _missing_band("dscr"),
                ],
            ),
            ScorecardFactorDef(
                factor_id="liquidity",
                name="Liquidity — current ratio",
                kind="figure",
                figure_key="current_ratio",
                weight=Decimal("0.15"),
                bands=[
                    _band("current_ratio_at_least_2_50", "at or above 2.50×", 1, at_least=Decimal("2.50")),
                    _band("current_ratio_at_least_2_00", "at or above 2.00×", 2, at_least=Decimal("2.00")),
                    _band("current_ratio_at_least_1_75", "at or above 1.75×", 3, at_least=Decimal("1.75")),
                    _band("current_ratio_at_least_1_50", "at or above 1.50×", 4, at_least=Decimal("1.50")),
                    _band("current_ratio_at_least_1_25", "at or above 1.25×", 5, at_least=Decimal("1.25")),
                    _band("current_ratio_at_least_1_10", "at or above 1.10×", 6, at_least=Decimal("1.10")),
                    _band("current_ratio_at_least_1_00", "at or above 1.00×", 7, at_least=Decimal("1.00")),
                    _band("current_ratio_at_least_0_90", "at or above 0.90×", 8, at_least=Decimal("0.90")),
                    _band("current_ratio_at_least_0_75", "at or above 0.75×", 9, at_least=Decimal("0.75")),
                    _band("current_ratio_below_0_75", "below 0.75×", 10),
                    _missing_band("liquidity"),
                ],
            ),
            ScorecardFactorDef(
                factor_id="profitability_trend",
                name="Profitability trend — EBITDA margin, latest less earliest period",
                kind="figure",
                figure_key="ebitda_margin",
                figure_mode="period_change",
                weight=Decimal("0.10"),
                bands=[
                    _band("margin_trend_at_least_0_0200", "margin up at least 0.0200", 1, at_least=Decimal("0.0200")),
                    _band("margin_trend_at_least_0_0100", "margin up at least 0.0100", 2, at_least=Decimal("0.0100")),
                    _band("margin_trend_at_least_0_0050", "margin up at least 0.0050", 3, at_least=Decimal("0.0050")),
                    _band("margin_trend_at_least_0", "margin flat to up", 4, at_least=Decimal("0")),
                    _band("margin_trend_at_least_-0_0050", "margin down at most 0.0050", 5, at_least=Decimal("-0.0050")),
                    _band("margin_trend_at_least_-0_0100", "margin down at most 0.0100", 6, at_least=Decimal("-0.0100")),
                    _band("margin_trend_at_least_-0_0200", "margin down at most 0.0200", 7, at_least=Decimal("-0.0200")),
                    _band("margin_trend_at_least_-0_0300", "margin down at most 0.0300", 8, at_least=Decimal("-0.0300")),
                    _band("margin_trend_at_least_-0_0500", "margin down at most 0.0500", 9, at_least=Decimal("-0.0500")),
                    _band("margin_trend_below_-0_0500", "margin down more than 0.0500", 10),
                    _missing_band("profitability_trend"),
                ],
            ),
            ScorecardFactorDef(
                factor_id="covenant_compliance",
                name="Covenant compliance — recorded test history",
                kind="file_fact",
                fact="covenant_compliance",
                weight=Decimal("0.10"),
                bands=[
                    _band("all_tests_passed", "every current covenant test passed", 2, category="all_tests_passed"),
                    _band("no_test_history", "no covenant tests recorded yet", 5, category="no_test_history"),
                    _band("tests_not_evaluable", "a current test could not be evaluated — its missing input is named on the result", 6, category="tests_not_evaluable"),
                    _band("tests_awaiting_inputs", "a current test stands awaiting the borrower's statements", 6, category="tests_awaiting_inputs"),
                    _band("covenant_test_failed", "a current covenant test failed", 8, category="covenant_test_failed"),
                ],
            ),
            ScorecardFactorDef(
                factor_id="breach_history",
                name="Breach history — covenant breach cases",
                kind="file_fact",
                fact="breach_history",
                weight=Decimal("0.10"),
                bands=[
                    _band("no_breach_history", "no breach cases on the file", 2, category="no_breach_history"),
                    _band("breach_history_resolved", "breach(es) resolved more than a year ago", 4, category="breach_history_resolved"),
                    _band("breach_cured_recent", "a breach cured within the last year", 6, category="breach_cured_recent"),
                    _band("breach_waived_recent", "a breach waived within the last year", 7, category="breach_waived_recent"),
                    _band("open_breach", "a breach case stands open", 9, category="open_breach"),
                ],
            ),
            ScorecardFactorDef(
                factor_id="statement_quality",
                name="Statement quality — source of the latest period",
                kind="file_fact",
                fact="statement_quality",
                weight=Decimal("0.10"),
                bands=[
                    _band("audited_statements_clean", "latest period rests on audited statements, no material disagreements", 2, category="audited_statements_clean"),
                    _band("audited_statements_contested", "latest period rests on audited statements, with material disagreements on the record", 5, category="audited_statements_contested"),
                    _band("tax_return_only", "latest period rests on the tax return alone", 6, category="tax_return_only"),
                    _band("management_statements_only", "latest period rests on management statements alone", 8, category="management_statements_only"),
                    _band(
                        "no_statements_for_latest_period",
                        "no statements on file for the latest period",
                        8,
                        category="no_statements_for_latest_period",
                        is_missing_data=True,
                    ),
                ],
            ),
        ],
        grade_bands=[
            ScorecardGradeBand(grade_from=1, grade_to=4, label="pass"),
            ScorecardGradeBand(grade_from=5, grade_to=6, label="watch"),
            ScorecardGradeBand(grade_from=7, grade_to=7, label="special mention"),
            ScorecardGradeBand(grade_from=8, grade_to=8, label="substandard"),
            ScorecardGradeBand(grade_from=9, grade_to=9, label="doubtful"),
            ScorecardGradeBand(grade_from=10, grade_to=10, label="loss"),
        ],
    )


# The categories each file-fact derivation can produce. A
# definition whose bands do not cover exactly these — for the
# facts it declares — is refused at registration: a category with
# no band would be a factor scored by nobody's rule.
_FACT_CATEGORIES: dict[str, frozenset[str]] = {
    "covenant_compliance": frozenset({
        "all_tests_passed", "no_test_history", "tests_not_evaluable",
        "tests_awaiting_inputs", "covenant_test_failed",
    }),
    "breach_history": frozenset({
        "no_breach_history", "breach_history_resolved",
        "breach_cured_recent", "breach_waived_recent", "open_breach",
    }),
    "statement_quality": frozenset({
        "audited_statements_clean", "audited_statements_contested",
        "tax_return_only", "management_statements_only",
        "no_statements_for_latest_period",
    }),
}


def validate_scorecard_definition(definition: ScorecardDefinition) -> None:
    """Refuse a scorecard definition that cannot score honestly:
    weights that do not sum to exactly 1, a figure factor without
    exactly one missing-data band, a file-fact factor whose bands
    do not cover the categories its derivation can produce, or a
    grade scale that does not cover 1–10 exactly once."""
    total = sum((f.weight for f in definition.factors), Decimal("0"))
    if total != Decimal("1"):
        raise ValidationError(
            f"scorecard {definition.scorecard_id} version "
            f"{definition.version}: factor weights sum to {total}, "
            "not 1 — a composite over partial weights is a grade "
            "nobody defined"
        )
    factor_ids = [f.factor_id for f in definition.factors]
    if len(set(factor_ids)) != len(factor_ids):
        raise ValidationError(
            f"scorecard version {definition.version}: duplicate "
            f"factor ids {factor_ids}"
        )
    for factor in definition.factors:
        missing = [b for b in factor.bands if b.is_missing_data]
        if factor.kind == "figure":
            if not factor.figure_key:
                raise ValidationError(
                    f"factor {factor.factor_id!r} is a figure "
                    "factor but names no figure"
                )
            if len(missing) != 1:
                raise ValidationError(
                    f"figure factor {factor.factor_id!r} must "
                    "carry exactly one missing-data band — an "
                    "input that cannot be computed must land "
                    "somewhere named"
                )
        else:
            expected = _FACT_CATEGORIES.get(factor.fact or "")
            if expected is None:
                raise ValidationError(
                    f"file-fact factor {factor.factor_id!r} names "
                    f"unknown fact {factor.fact!r}"
                )
            covered = {
                b.category for b in factor.bands if b.category
            }
            if covered != set(expected):
                raise ValidationError(
                    f"file-fact factor {factor.factor_id!r}: its "
                    f"bands cover {sorted(covered)}, but the "
                    f"{factor.fact} derivation can produce "
                    f"{sorted(expected)}"
                )
    covered_grades: list[int] = []
    for band in definition.grade_bands:
        covered_grades.extend(
            range(band.grade_from, band.grade_to + 1)
        )
    if sorted(covered_grades) != list(range(1, 11)):
        raise ValidationError(
            f"scorecard version {definition.version}: grade bands "
            f"cover {sorted(covered_grades)}, not 1–10 exactly once"
        )


_SCORECARDS: dict[str, ScorecardDefinition] = {}


def register_scorecard(definition: ScorecardDefinition) -> ScorecardDefinition:
    """Register one scorecard version — a deployment registers
    the bank's own governed model definition here. Registration
    validates the definition whole; a version already registered
    is never replaced (a change is a new version)."""
    validate_scorecard_definition(definition)
    if definition.scorecard_id != SCORECARD_ID:
        raise ValidationError(
            f"scorecard id {definition.scorecard_id!r} is not "
            f"{SCORECARD_ID!r}"
        )
    existing = _SCORECARDS.get(definition.version)
    if existing is not None:
        if existing != definition:
            raise ValidationError(
                f"scorecard version {definition.version} is "
                "already registered with different content — a "
                "change is a new version, never an edit"
            )
        return existing
    _SCORECARDS[definition.version] = definition
    return definition


def get_scorecard(version: str | None = None) -> ScorecardDefinition:
    version = version or CURRENT_SCORECARD_VERSION
    definition = _SCORECARDS.get(version)
    if definition is None:
        raise ValidationError(
            f"unknown scorecard version {version!r}; registered "
            f"versions: {sorted(_SCORECARDS)}"
        )
    return definition


def list_scorecard_versions() -> list[str]:
    return sorted(_SCORECARDS)


register_scorecard(_v1_definition())


def grade_band_label(definition: ScorecardDefinition, grade: int) -> str:
    for band in definition.grade_bands:
        if band.grade_from <= grade <= band.grade_to:
            return band.label
    raise ValidationError(f"grade {grade} is outside the scorecard scale")


# ---------------------------------------------------------------------
# Band matching
# ---------------------------------------------------------------------


def _missing_band_of(factor: ScorecardFactorDef) -> ScorecardBand:
    for band in factor.bands:
        if band.is_missing_data:
            return band
    raise ValidationError(
        f"factor {factor.factor_id!r} has no missing-data band"
    )


def _numeric_band(factor: ScorecardFactorDef, value: Decimal) -> ScorecardBand:
    for band in factor.bands:
        if band.is_missing_data or band.category is not None:
            continue
        if band.at_least is not None and value < band.at_least:
            continue
        if band.at_most is not None and value > band.at_most:
            continue
        return band
    raise ValidationError(
        f"factor {factor.factor_id!r}: no band matches value {value}"
    )


def _category_band(factor: ScorecardFactorDef, category: str) -> ScorecardBand:
    for band in factor.bands:
        if band.category == category:
            return band
    raise ValidationError(
        f"factor {factor.factor_id!r}: no band for category "
        f"{category!r}"
    )


# ---------------------------------------------------------------------
# Factor inputs: figures from the pack, facts from the file.
# ---------------------------------------------------------------------


def _figure_for(
    pack: ComputationPack, key: str, year: int
) -> ComputedFigure | None:
    for figure in pack.figures:
        if figure.key == key and figure.fiscal_year == year:
            return figure
    return None


def _figure_trace(figure: ComputedFigure, pack: ComputationPack) -> str:
    source = (
        f"computation pack {pack.pack_id}, figure {figure.key} "
        f"({figure.label}), FY{figure.fiscal_year}"
    )
    if figure.disagreement_ids:
        source += (
            "; computed over document disagreement(s) "
            + ", ".join(figure.disagreement_ids)
        )
    return source


def _score_figure_factor(
    factor: ScorecardFactorDef,
    pack: ComputationPack,
    latest_period: int | None,
) -> RatingFactorScore:
    base = dict(
        factor_id=factor.factor_id,
        factor_name=factor.name,
        kind=factor.kind,
        weight=factor.weight,
        figure_key=factor.figure_key,
    )
    missing = _missing_band_of(factor)

    def insufficient(trace: str, missing_inputs: list[str],
                     year: int | None) -> RatingFactorScore:
        return RatingFactorScore(
            **base,
            fiscal_year=year,
            trace=trace,
            band_id=missing.band_id,
            band_label=missing.label,
            score=missing.score,
            weighted_contribution=(
                factor.weight * missing.score
            ).quantize(_CENT, rounding=ROUND_HALF_UP),
            missing_inputs=missing_inputs,
        )

    if latest_period is None:
        return insufficient(
            f"computation pack {pack.pack_id} carries no period "
            "figures — there is nothing to read",
            [f"{factor.figure_key}: the pack has no periods"],
            None,
        )
    if factor.figure_mode == "period_change":
        periods = sorted({
            f.fiscal_year for f in pack.figures
            if f.key == factor.figure_key and f.fiscal_year is not None
        })
        if len(periods) < 2:
            return insufficient(
                f"computation pack {pack.pack_id}, figure "
                f"{factor.figure_key}: a trend needs two periods "
                f"and the pack has {len(periods)}",
                [f"{factor.figure_key}: only one period computed"],
                latest_period,
            )
        earliest, latest = periods[0], periods[-1]
        first = _figure_for(pack, factor.figure_key, earliest)
        last = _figure_for(pack, factor.figure_key, latest)
        for figure, year in ((first, earliest), (last, latest)):
            if figure is None or figure.status != "computed" \
                    or figure.value is None:
                named = (
                    list(figure.missing_inputs) if figure is not None
                    else [f"{factor.figure_key} (FY{year}) was not computed"]
                )
                return insufficient(
                    f"computation pack {pack.pack_id}, figure "
                    f"{factor.figure_key} FY{year} is not computable",
                    named,
                    latest_period,
                )
        change = last.value - first.value
        band = _numeric_band(factor, change)
        return RatingFactorScore(
            **base,
            input_value=change,
            fiscal_year=latest,
            trace=(
                f"computation pack {pack.pack_id}, figure "
                f"{factor.figure_key} FY{latest} less FY{earliest} "
                f"({_figure_trace(last, pack)}; "
                f"{_figure_trace(first, pack)})"
            ),
            derivation_steps=[
                *(s.step_key for s in last.steps),
                *(s.step_key for s in first.steps),
            ],
            band_id=band.band_id,
            band_label=band.label,
            score=band.score,
            weighted_contribution=(
                factor.weight * band.score
            ).quantize(_CENT, rounding=ROUND_HALF_UP),
        )
    figure = _figure_for(pack, factor.figure_key, latest_period)
    if figure is None or figure.status != "computed" or figure.value is None:
        named = (
            list(figure.missing_inputs) if figure is not None
            else [f"{factor.figure_key} (FY{latest_period}) was not computed"]
        )
        return insufficient(
            f"computation pack {pack.pack_id}, figure "
            f"{factor.figure_key} FY{latest_period} is not computable",
            named,
            latest_period,
        )
    band = _numeric_band(factor, figure.value)
    return RatingFactorScore(
        **base,
        input_value=figure.value,
        fiscal_year=latest_period,
        trace=_figure_trace(figure, pack),
        derivation_steps=[s.step_key for s in figure.steps],
        band_id=band.band_id,
        band_label=band.label,
        score=band.score,
        weighted_contribution=(
            factor.weight * band.score
        ).quantize(_CENT, rounding=ROUND_HALF_UP),
    )


def _head_results(
    results: list[CovenantTestResult],
) -> list[CovenantTestResult]:
    """The current result per instance lineage: a result another
    recorded result supersedes is history, not the standing
    record (the same head discipline the pipeline applies)."""
    superseded = {
        r.supersedes_result_id for r in results if r.supersedes_result_id
    }
    return [r for r in results if r.result_id not in superseded]


def _compliance_category(
    results: list[CovenantTestResult],
    standings: dict[str, str],
) -> tuple[str, str, list[str]]:
    heads = _head_results(results)
    current = [
        r for r in heads
        if standings.get(r.result_id, RESULT_CURRENT) == RESULT_CURRENT
    ]
    counts = {
        outcome: sum(1 for r in current if r.outcome == outcome)
        for outcome in ("pass", "fail", "not_evaluable", "awaiting_inputs")
    }
    trace = (
        f"covenant test results: {len(current)} current result(s) "
        f"of {len(results)} recorded — {counts['pass']} passed, "
        f"{counts['fail']} failed, {counts['not_evaluable']} not "
        f"evaluable, {counts['awaiting_inputs']} awaiting inputs"
    )
    if counts["fail"]:
        category = "covenant_test_failed"
    elif counts["not_evaluable"]:
        category = "tests_not_evaluable"
    elif counts["awaiting_inputs"]:
        category = "tests_awaiting_inputs"
    elif current:
        category = "all_tests_passed"
    else:
        category = "no_test_history"
    ids = [r.result_id for r in current]
    return category, trace, ids


def _breach_category(
    breaches: list[BreachCase], *, as_of: date
) -> tuple[str, str, list[str]]:
    horizon = as_of - timedelta(days=365)
    open_cases = [
        b for b in breaches if b.state not in BREACH_TERMINAL_STATES
    ]
    recent = [b for b in breaches if b.test_date >= horizon]
    waived_recent = [b for b in recent if b.state == "waived"]
    cured_recent = [b for b in recent if b.state == "cured"]
    trace = (
        f"breach cases: {len(breaches)} recorded — "
        f"{len(open_cases)} open, "
        f"{sum(1 for b in breaches if b.state == 'cured')} cured, "
        f"{sum(1 for b in breaches if b.state == 'waived')} waived, "
        f"{sum(1 for b in breaches if b.state == 'defaulted_path')} "
        "on the default path"
    )
    if open_cases:
        category = "open_breach"
    elif waived_recent:
        category = "breach_waived_recent"
    elif cured_recent:
        category = "breach_cured_recent"
    elif breaches:
        category = "breach_history_resolved"
    else:
        category = "no_breach_history"
    return category, trace, [b.breach_id for b in breaches]


def _quality_category(
    spread: Spread, latest_period: int | None
) -> tuple[str, str, list[str]]:
    if latest_period is None:
        return (
            "no_statements_for_latest_period",
            f"spread {spread.spread_id} covers no periods",
            [],
        )
    sources = [
        ps for ps in spread.period_sources
        if ps.fiscal_year == latest_period
    ]
    types = {ps.source_type for ps in sources}
    material = [
        d for d in spread.disagreements
        if d.material and d.fiscal_year == latest_period
    ]
    trace = (
        f"spread {spread.spread_id}, FY{latest_period} period "
        f"sources: "
        + (
            ", ".join(
                f"{ps.source_type} (document {ps.document_id}, "
                f"version {ps.version})"
                for ps in sorted(
                    sources, key=lambda p: (p.source_type, p.document_id)
                )
            )
            or "none recorded"
        )
        + f"; material disagreements on FY{latest_period}: "
        f"{len(material)}"
    )
    if "audited_statement" in types:
        category = (
            "audited_statements_contested" if material
            else "audited_statements_clean"
        )
    elif "tax_return" in types:
        category = "tax_return_only"
    elif "management_statement" in types:
        category = "management_statements_only"
    else:
        category = "no_statements_for_latest_period"
    return category, trace, [d.disagreement_id for d in material]


def _score_fact_factor(
    factor: ScorecardFactorDef,
    *,
    category: str,
    trace: str,
) -> RatingFactorScore:
    band = _category_band(factor, category)
    return RatingFactorScore(
        factor_id=factor.factor_id,
        factor_name=factor.name,
        kind=factor.kind,
        weight=factor.weight,
        input_category=category,
        trace=trace,
        band_id=band.band_id,
        band_label=band.label,
        score=band.score,
        weighted_contribution=(
            factor.weight * band.score
        ).quantize(_CENT, rounding=ROUND_HALF_UP),
    )


# ---------------------------------------------------------------------
# Assignment: the pure builder. Same inputs, same artefact.
# ---------------------------------------------------------------------


def build_rating_assignment(
    definition: ScorecardDefinition,
    *,
    credit_file: CreditFile,
    spread: Spread,
    pack: ComputationPack,
    test_results: list[CovenantTestResult],
    result_standings: dict[str, str],
    breaches: list[BreachCase],
    as_of: date,
) -> RatingAssignment:
    """Score every factor of the definition over the pinned
    artefacts and combine them into the assignment.

    Pure: the same definition, pack, spread, results and breaches
    at the same as_of reproduce the identical assignment, id
    included. The factor set is the definition's — every factor
    is scored, a missing input landing in its named missing-data
    band, never skipped and never zero-filled."""
    years = [
        f.fiscal_year for f in pack.figures if f.fiscal_year is not None
    ]
    latest_period = max(years) if years else None
    factors: list[RatingFactorScore] = []
    for factor in definition.factors:
        if factor.kind == "figure":
            factors.append(_score_figure_factor(factor, pack, latest_period))
        elif factor.fact == "covenant_compliance":
            category, trace, _ids = _compliance_category(
                test_results, result_standings
            )
            factors.append(_score_fact_factor(
                factor, category=category, trace=trace
            ))
        elif factor.fact == "breach_history":
            category, trace, _ids = _breach_category(
                breaches, as_of=as_of
            )
            factors.append(_score_fact_factor(
                factor, category=category, trace=trace
            ))
        elif factor.fact == "statement_quality":
            category, trace, _ids = _quality_category(
                spread, latest_period
            )
            factors.append(_score_fact_factor(
                factor, category=category, trace=trace
            ))
        else:  # pragma: no cover - registration validation bars this
            raise ValidationError(
                f"factor {factor.factor_id!r} names unknown fact "
                f"{factor.fact!r}"
            )
    composite = sum(
        (f.weighted_contribution for f in factors), Decimal("0")
    ).quantize(_CENT, rounding=ROUND_HALF_UP)
    grade = int(
        composite.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    grade = max(1, min(10, grade))
    descriptor = {
        "scorecard_id": definition.scorecard_id,
        "scorecard_version": definition.version,
        "credit_file_id": credit_file.credit_file_id,
        "computation_pack_id": pack.pack_id,
        "spread_id": spread.spread_id,
        "as_of": as_of.isoformat(),
        "factors": [f.model_dump(mode="json") for f in factors],
        "composite_score": format(composite, "f"),
        "assigned_grade": grade,
    }
    return RatingAssignment(
        assignment_id=f"rating-{_canonical_hash(descriptor)[:16]}",
        credit_file_id=credit_file.credit_file_id,
        borrower_name=credit_file.borrower.legal_name,
        scorecard_id=definition.scorecard_id,
        scorecard_version=definition.version,
        as_of=as_of,
        spread_id=spread.spread_id,
        computation_pack_id=pack.pack_id,
        latest_period=latest_period,
        factors=factors,
        composite_score=composite,
        assigned_grade=grade,
        assigned_band=grade_band_label(definition, grade),
    )


# ---------------------------------------------------------------------
# Overrides: validation and the final grade.
# ---------------------------------------------------------------------


def validate_override_reasons(reason_code: str, note: str) -> None:
    """One coded reason from the current vocabulary — or no
    override. A free-text reason is refused: the record must say,
    mechanically, why the grade moved."""
    if reason_code not in OVERRIDE_REASON_CODES:
        raise ValidationError(
            f"unknown rating override reason code "
            f"{reason_code!r}; the vocabulary is "
            f"{OVERRIDE_VOCABULARY_VERSION}: "
            f"{', '.join(sorted(OVERRIDE_REASON_CODES))}"
        )
    if reason_code == "other" and not (note or "").strip():
        raise ValidationError(
            "override reason 'other' requires a note stating the "
            "reason — 'other' without one is a free-text override"
        )


def final_grade_of(
    assignment: RatingAssignment,
    overrides: list[RatingOverride],
) -> tuple[int, RatingOverride | None]:
    """The grade that currently stands: the latest override's new
    grade when the chain has one, else the assigned grade. The
    chain is the override artefacts' own supersession order."""
    mine = [
        o for o in overrides if o.assignment_id == assignment.assignment_id
    ]
    if not mine:
        return assignment.assigned_grade, None
    superseded = {
        o.supersedes_override_id for o in mine if o.supersedes_override_id
    }
    heads = [o for o in mine if o.override_id not in superseded]
    head = heads[-1] if heads else mine[-1]
    return head.new_grade, head


def validate_override(
    *,
    assignment: RatingAssignment,
    overrides: list[RatingOverride],
    new_grade: int,
    reason_code: str,
    note: str,
    officer_id: str,
    officer_role: str,
    countersigner_id: str | None,
    countersigner_role: str | None,
) -> RatingOverride:
    """Validate one override and return it as it will be stored.

    Refusals: an unnamed officer or one without a credit-officer
    role; an unknown or note-less coded reason; a target grade
    outside the 1–10 scale; a move of more than one grade from
    the ASSIGNED grade (the bound is on the assignment, not on
    the last move — a chain of one-grade steps may not walk a
    grade two steps from where the scorecard put it); no move at
    all; a missing countersigner; a countersigner who is the
    officer themselves or who holds no officer role."""
    require_officer(officer_id, officer_role, what="a rating override")
    validate_override_reasons(reason_code, note)
    if not 1 <= new_grade <= 10:
        raise ValidationError(
            f"an override grade must stand on the 1–10 scale, "
            f"not {new_grade}"
        )
    previous_grade, current = final_grade_of(assignment, overrides)
    if new_grade == previous_grade:
        raise ValidationError(
            f"an override that leaves the grade at {new_grade} "
            "changes nothing — there is nothing to record"
        )
    if abs(new_grade - assignment.assigned_grade) > 1:
        raise ValidationError(
            f"an override may move the final grade at most one "
            f"grade from the assigned grade "
            f"{assignment.assigned_grade}; grade {new_grade} "
            "stands further away — a larger disagreement with "
            "the scorecard is a new assignment under a reviewed "
            "definition, not an override"
        )
    if not countersigner_id:
        raise ValidationError(
            "a rating override requires a countersigning credit "
            "officer: one officer does not move a grade alone"
        )
    require_officer(
        countersigner_id, countersigner_role or "",
        what="countersigning a rating override",
    )
    if countersigner_id == officer_id:
        raise ValidationError(
            "the countersigner must be a different officer than "
            "the officer moving the grade: a countersignature by "
            "the same person countersigns nothing"
        )
    descriptor = {
        "assignment_id": assignment.assignment_id,
        "new_grade": new_grade,
        "reason_code": reason_code,
        "officer_id": officer_id,
        "countersigner_id": countersigner_id,
        "supersedes_override_id": (
            current.override_id if current else None
        ),
        "note": note or "",
    }
    return RatingOverride(
        override_id=f"ratingovr-{_canonical_hash(descriptor)[:16]}",
        assignment_id=assignment.assignment_id,
        credit_file_id=assignment.credit_file_id,
        assigned_grade=assignment.assigned_grade,
        previous_grade=previous_grade,
        new_grade=new_grade,
        reason_code=reason_code,
        reason_vocabulary=OVERRIDE_VOCABULARY_VERSION,
        officer_id=officer_id,
        officer_role=officer_role,
        countersigner_id=countersigner_id,
        countersigner_role=countersigner_role or "",
        supersedes_override_id=(
            current.override_id if current else None
        ),
        note=note or "",
    )
