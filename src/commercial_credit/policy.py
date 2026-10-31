"""The policy pack: lending policy as versioned data + code.

Design law 2: policy is code, versioned. A policy pack is an
identified, dated set of coded tests over the computation engine's
figures and the credit file's own facts. Evaluating a file pins ONE
pack version; the run produces a `PolicyReport` — a stored,
content-addressed artefact like the computation pack it reads —
recording per test: the outcome, the values read, the threshold
applied, and the policy citation.

The honesty rule of the computation engine carries over unchanged:
a test whose input figure is not computable is *not_evaluable*,
naming the missing input. It is never a silent pass — a DSCR the
statements cannot support does not become a pass because the policy
run needed an answer. Applicability is a file fact too: the global
DSCR test reads the global pack where the facility names
guarantors, and says so when there are none or no combination was
computed; the appraisal test reads an appraisal where the facility
type requires one, and says so where it does not.

Statement age is measured in complete calendar months from the
fiscal year-end (31 December of the pack's most recent fiscal year)
to the evaluation's as-of date; appraisal age likewise from the
most recent appraisal document's received date. Both conventions
are stated here because an age nobody defined is a stale input
nobody can argue with (law 7's machinery is round 9; the policy
tests that name staleness live here).

Two versions ship in this round. v2 tightens three thresholds —
the minimum DSCR, the hospitality leverage ceiling, and the
statement-age limit — precisely so version discipline is exercised
by fixtures, not just asserted: a borrower at 1.30× DSCR passes
v1 and fails v2, and its v1 report stays on the record, untouched.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .errors import NotFoundError
from .models import (
    BorrowerSegment,
    ComputedFigure,
    ComputationPack,
    CreditFile,
    DocumentKind,
    PolicyInputValue,
    PolicyPack,
    PolicyReport,
    PolicyTest,
    PolicyTestOutcome,
)

POLICY_PACK_ID = "aldergrove-policy"

_RATIO_PLACES = Decimal("0.0001")

_OPERATORS = {
    ">=": lambda v, t: v >= t,
    ">": lambda v, t: v > t,
    "<=": lambda v, t: v <= t,
    "<": lambda v, t: v < t,
}


def _tests(
    *,
    leverage: dict[str, str],
    min_dscr: str,
    min_global_dscr: str,
    min_fccr: str,
    max_concentration: str,
    statement_age_months: str,
    appraisal_age_months: str,
) -> list[PolicyTest]:
    """The Aldergrove test set. The two shipped versions differ only
    in the numbers passed in — the tests, their inputs and their
    citations are the policy's structure, the thresholds its tuning."""
    return [
        PolicyTest(
            test_id="max_leverage_by_segment",
            name="Maximum leverage by borrower segment",
            kind="figure",
            severity="blocker",
            citation=(
                "Aldergrove Commercial Lending Policy §4.2, segment "
                "table A: total liabilities to tangible net worth "
                "must not exceed the ceiling for the borrower's "
                "segment."
            ),
            figure_key="debt_to_tangible_net_worth",
            scope="borrower",
            operator="<=",
            segment_thresholds={
                segment: Decimal(limit) for segment, limit in leverage.items()
            },
        ),
        PolicyTest(
            test_id="min_dscr",
            name="Minimum debt service coverage (borrower)",
            kind="figure",
            severity="blocker",
            citation=(
                "Aldergrove Commercial Lending Policy §4.3: debt "
                "service coverage — EBITDA over debt service — of at "
                f"least {min_dscr}× on the most recent fiscal year."
            ),
            figure_key="dscr",
            scope="borrower",
            operator=">=",
            threshold=Decimal(min_dscr),
        ),
        PolicyTest(
            test_id="min_global_dscr",
            name="Minimum debt service coverage (global, with guarantors)",
            kind="figure",
            severity="blocker",
            citation=(
                "Aldergrove Commercial Lending Policy §4.4: where "
                "owners guarantee the facility, global debt service "
                "coverage across borrower and guarantors of at least "
                f"{min_global_dscr}×."
            ),
            figure_key="global_dscr",
            scope="global",
            operator=">=",
            threshold=Decimal(min_global_dscr),
            requires_guarantors=True,
        ),
        PolicyTest(
            test_id="min_fixed_charge_coverage",
            name="Minimum fixed-charge coverage",
            kind="figure",
            severity="warning",
            citation=(
                "Aldergrove Commercial Lending Policy §4.5: "
                "fixed-charge coverage guideline of at least "
                f"{min_fccr}× after capital expenditures."
            ),
            figure_key="fixed_charge_coverage",
            scope="borrower",
            operator=">=",
            threshold=Decimal(min_fccr),
        ),
        PolicyTest(
            test_id="max_single_borrower_concentration",
            name="Maximum single-borrower concentration",
            kind="file_fact",
            severity="blocker",
            citation=(
                "Aldergrove Commercial Lending Policy §3.1: one "
                "borrower's facility must not exceed "
                f"{(Decimal(max_concentration) * 100)}% of the "
                "bank's stated capital base."
            ),
            fact="concentration",
            limit=Decimal(max_concentration),
        ),
        PolicyTest(
            test_id="required_documents_complete",
            name="Required documents on file, by facility type",
            kind="file_fact",
            severity="blocker",
            citation=(
                "Aldergrove Commercial Lending Policy §2.1 and the "
                "facility-type schedules: every required document "
                "on the credit file's checklist must be on file."
            ),
            fact="required_documents",
        ),
        PolicyTest(
            test_id="financial_statement_age",
            name="Financial statement age limit",
            kind="file_fact",
            severity="blocker",
            citation=(
                "Aldergrove Commercial Lending Policy §2.3: the "
                "most recent fiscal year-end statements must be no "
                f"more than {statement_age_months} months old at "
                "evaluation."
            ),
            fact="statement_age",
            limit=Decimal(statement_age_months),
        ),
        PolicyTest(
            test_id="appraisal_age",
            name="Appraisal age limit (CRE and construction)",
            kind="file_fact",
            severity="warning",
            citation=(
                "Aldergrove Commercial Lending Policy §5.2: the "
                "appraisal relied on must be no more than "
                f"{appraisal_age_months} months old at evaluation."
            ),
            fact="appraisal_age",
            limit=Decimal(appraisal_age_months),
            applies_to_facility_types=["cre_loan", "construction_loan"],
        ),
    ]


_LEVERAGE_V1 = {
    BorrowerSegment.MANUFACTURING.value: "2.00",
    BorrowerSegment.DISTRIBUTION.value: "2.25",
    BorrowerSegment.HEALTHCARE.value: "2.50",
    BorrowerSegment.HOSPITALITY.value: "2.00",
    BorrowerSegment.RETAIL.value: "2.00",
    BorrowerSegment.PROFESSIONAL_SERVICES.value: "3.00",
    BorrowerSegment.REAL_ESTATE.value: "3.00",
    BorrowerSegment.OTHER.value: "2.00",
}

# v2 tightens the hospitality ceiling from 2.00× to 1.75×.
_LEVERAGE_V2 = {**_LEVERAGE_V1, BorrowerSegment.HOSPITALITY.value: "1.75"}

_POLICY_PACKS: dict[str, PolicyPack] = {
    "v1": PolicyPack(
        pack_id=POLICY_PACK_ID,
        version="v1",
        effective_from=date(2025, 1, 1),
        effective_to=date(2026, 3, 31),
        issuing_note=(
            "Aldergrove commercial lending policy, 2025 annual "
            "issue. Coverage floors and segment leverage ceilings "
            "as approved by the credit policy committee for 2025."
        ),
        capital_base=Decimal("250000000"),
        tests=_tests(
            leverage=_LEVERAGE_V1,
            min_dscr="1.25",
            min_global_dscr="1.20",
            min_fccr="1.10",
            max_concentration="0.10",
            statement_age_months="18",
            appraisal_age_months="12",
        ),
    ),
    "v2": PolicyPack(
        pack_id=POLICY_PACK_ID,
        version="v2",
        effective_from=date(2026, 4, 1),
        effective_to=None,
        issuing_note=(
            "2026 policy refresh: after the 2025 portfolio review "
            "the minimum DSCR rises to 1.35×, the hospitality "
            "leverage ceiling falls to 1.75×, and statements may "
            "be no more than 15 months old. Files already evaluated "
            "under v1 keep their v1 reports."
        ),
        capital_base=Decimal("250000000"),
        tests=_tests(
            leverage=_LEVERAGE_V2,
            min_dscr="1.35",
            min_global_dscr="1.20",
            min_fccr="1.10",
            max_concentration="0.10",
            statement_age_months="15",
            appraisal_age_months="12",
        ),
    ),
}

# The version new pipeline runs evaluate under. Pinning a version is
# always explicit in the report; this only chooses the default.
CURRENT_POLICY_VERSION = "v2"


def list_policy_packs() -> list[PolicyPack]:
    """Every shipped pack version, oldest first."""
    return sorted(
        _POLICY_PACKS.values(), key=lambda p: (p.effective_from, p.version)
    )


def get_policy_pack(version: str) -> PolicyPack:
    pack = _POLICY_PACKS.get(version)
    if pack is None:
        raise NotFoundError(
            f"no policy pack version {version!r}; shipped versions: "
            + ", ".join(sorted(_POLICY_PACKS))
        )
    return pack


# ---------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------


def _months_between(start: date, end: date) -> int:
    """Complete calendar months from start to end (negative when
    end precedes start, clamped by the callers that read ages)."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return months


def _latest_figure(pack: ComputationPack, key: str) -> ComputedFigure | None:
    candidates = [
        f for f in pack.figures if f.key == key and f.fiscal_year is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.fiscal_year)


def _outcome_base(test: PolicyTest) -> PolicyTestOutcome:
    return PolicyTestOutcome(
        test_id=test.test_id,
        name=test.name,
        severity=test.severity,
        citation=test.citation,
        outcome="not_evaluable",
        operator=test.operator,
    )


def _not_evaluable(
    outcome: PolicyTestOutcome,
    missing: list[str],
    *,
    detail: str = "",
    values_read: list[PolicyInputValue] | None = None,
) -> PolicyTestOutcome:
    outcome.outcome = "not_evaluable"
    outcome.missing_inputs = missing
    outcome.detail = detail
    if values_read:
        outcome.values_read = values_read
    return outcome


def _eval_figure_test(
    test: PolicyTest,
    credit_file: CreditFile,
    borrower_pack: ComputationPack,
    global_pack: ComputationPack | None,
) -> PolicyTestOutcome:
    outcome = _outcome_base(test)
    values_read: list[PolicyInputValue] = []

    threshold = test.threshold
    if test.segment_thresholds is not None:
        segment = credit_file.borrower.segment.value
        values_read.append(PolicyInputValue(
            input="fact:borrower.segment",
            value=segment,
            source="credit file",
        ))
        threshold = test.segment_thresholds.get(segment)
        if threshold is None:
            return _not_evaluable(
                outcome,
                [f"segment threshold: the pack sets no ceiling for "
                 f"segment {segment!r}"],
                detail="The pack's segment table does not cover this "
                       "borrower's segment, so the test cannot be run.",
                values_read=values_read,
            )
    outcome.threshold = threshold

    if test.scope == "global":
        if test.requires_guarantors and not credit_file.facility.guarantors:
            return _not_evaluable(
                outcome,
                ["guarantors: the facility names no guarantors, so "
                 "there is no global view to test"],
                detail="The global test applies only where the "
                       "facility is guaranteed; this one is not.",
                values_read=values_read,
            )
        if global_pack is None:
            return _not_evaluable(
                outcome,
                ["global computation pack: no borrower + guarantor "
                 "combination has been computed for this file"],
                detail="The test reads the global pack; none was "
                       "handed to this evaluation.",
                values_read=values_read,
            )
        pack = global_pack
    else:
        pack = borrower_pack

    figure = _latest_figure(pack, test.figure_key or "")
    if figure is None:
        return _not_evaluable(
            outcome,
            [f"figure {test.figure_key!r}: not present in "
             f"computation pack {pack.pack_id}"],
            values_read=values_read,
        )
    outcome.fiscal_year = figure.fiscal_year
    if figure.status != "computed" or figure.value is None:
        missing = [
            f"{test.figure_key} (FY{figure.fiscal_year}): {m}"
            for m in figure.missing_inputs
        ] or [
            f"{test.figure_key} (FY{figure.fiscal_year}) is "
            "not computable"
        ]
        return _not_evaluable(
            outcome,
            missing,
            detail="The figure the test reads could not be computed "
                   "from the statements on file; the test does not "
                   "pass by default.",
            values_read=values_read,
        )
    values_read.append(PolicyInputValue(
        input=f"figure:{test.figure_key}",
        value=str(figure.value),
        source=f"computation pack {pack.pack_id}, FY{figure.fiscal_year}",
    ))
    passed = _OPERATORS[test.operator](figure.value, threshold)
    outcome.outcome = "pass" if passed else "fail"
    outcome.values_read = values_read
    outcome.detail = (
        f"{figure.label} is {figure.value} against a "
        f"{test.operator} {threshold} requirement."
    )
    return outcome


def _eval_required_documents(
    test: PolicyTest, credit_file: CreditFile
) -> PolicyTestOutcome:
    outcome = _outcome_base(test)
    if not credit_file.checklist:
        return _not_evaluable(
            outcome,
            ["credit file checklist: not generated for this file"],
        )
    required = [i for i in credit_file.checklist if i.required]
    missing = [i for i in required if not i.satisfied]
    values_read = [PolicyInputValue(
        input="fact:required_documents_satisfied",
        value=f"{len(required) - len(missing)}/{len(required)}",
        source="credit file checklist",
    )]
    if missing:
        values_read.append(PolicyInputValue(
            input="fact:missing_required_items",
            value=", ".join(i.item_id for i in missing),
            source="credit file checklist",
        ))
        outcome.outcome = "fail"
        outcome.values_read = values_read
        outcome.detail = (
            "Still outstanding: "
            + "; ".join(f"{i.item_id} ({i.label})" for i in missing)
            + "."
        )
        return outcome
    outcome.outcome = "pass"
    outcome.values_read = values_read
    outcome.detail = (
        f"All {len(required)} required documents are on file."
    )
    return outcome


def _eval_statement_age(
    test: PolicyTest,
    borrower_pack: ComputationPack,
    as_of: date,
) -> PolicyTestOutcome:
    outcome = _outcome_base(test)
    outcome.operator = "<="
    outcome.threshold = test.limit
    years = [
        f.fiscal_year for f in borrower_pack.figures
        if f.fiscal_year is not None
    ]
    if not years:
        return _not_evaluable(
            outcome,
            ["financial statements: the computation pack covers "
             "no fiscal period"],
        )
    latest = max(years)
    year_end = date(latest, 12, 31)
    age = max(0, _months_between(year_end, as_of))
    outcome.fiscal_year = latest
    outcome.values_read = [
        PolicyInputValue(
            input="fact:latest_statement_fiscal_year",
            value=str(latest),
            source=f"computation pack {borrower_pack.pack_id}",
        ),
        PolicyInputValue(
            input="fact:statement_year_end",
            value=year_end.isoformat(),
            source="fiscal year-end convention (31 December)",
        ),
        PolicyInputValue(
            input="fact:as_of", value=as_of.isoformat(),
            source="evaluation",
        ),
        PolicyInputValue(
            input="fact:age_months", value=str(age),
            source="complete calendar months, year-end to as-of",
        ),
    ]
    passed = Decimal(age) <= test.limit
    outcome.outcome = "pass" if passed else "fail"
    outcome.detail = (
        f"The most recent statements (FY{latest}, year-end "
        f"{year_end.isoformat()}) are {age} months old at "
        f"{as_of.isoformat()}, against a limit of {test.limit}."
    )
    return outcome


def _eval_appraisal_age(
    test: PolicyTest, credit_file: CreditFile, as_of: date
) -> PolicyTestOutcome:
    outcome = _outcome_base(test)
    outcome.operator = "<="
    outcome.threshold = test.limit
    facility_type = credit_file.facility.facility_type.value
    if facility_type not in test.applies_to_facility_types:
        return _not_evaluable(
            outcome,
            [f"appraisal: facility type {facility_type!r} carries "
             "no appraisal requirement under this pack"],
            detail="The appraisal test applies only to "
                   + ", ".join(test.applies_to_facility_types)
                   + " facilities.",
        )
    appraisals = [
        d for d in credit_file.documents if d.kind == DocumentKind.APPRAISAL
    ]
    if not appraisals:
        return _not_evaluable(
            outcome,
            ["appraisal document: none on file for this credit file"],
            detail="Presence is the completeness test's question; "
                   "this test ages an appraisal that exists.",
        )
    latest_doc = max(appraisals, key=lambda d: d.received_at)
    received = latest_doc.received_at.date()
    age = max(0, _months_between(received, as_of))
    outcome.values_read = [
        PolicyInputValue(
            input="fact:appraisal_document",
            value=latest_doc.document_id,
            source="credit file documents",
        ),
        PolicyInputValue(
            input="fact:appraisal_received_at",
            value=received.isoformat(),
            source="credit file documents",
        ),
        PolicyInputValue(
            input="fact:as_of", value=as_of.isoformat(),
            source="evaluation",
        ),
        PolicyInputValue(
            input="fact:age_months", value=str(age),
            source="complete calendar months, received to as-of",
        ),
    ]
    passed = Decimal(age) <= test.limit
    outcome.outcome = "pass" if passed else "fail"
    outcome.detail = (
        f"The most recent appraisal ({latest_doc.document_id}, "
        f"received {received.isoformat()}) is {age} months old at "
        f"{as_of.isoformat()}, against a limit of {test.limit}."
    )
    return outcome


def _eval_concentration(
    test: PolicyTest, pack: PolicyPack, credit_file: CreditFile
) -> PolicyTestOutcome:
    outcome = _outcome_base(test)
    outcome.operator = "<="
    outcome.threshold = test.limit
    amount = credit_file.facility.amount
    exact_share = amount / pack.capital_base
    share = exact_share.quantize(
        _RATIO_PLACES, rounding=ROUND_HALF_UP
    )
    outcome.values_read = [
        PolicyInputValue(
            input="fact:facility.amount",
            value=str(amount),
            source="credit file facility request",
        ),
        PolicyInputValue(
            input="policy:capital_base",
            value=str(pack.capital_base),
            source=f"policy pack {pack.version}",
        ),
        PolicyInputValue(
            input="fact:concentration_share",
            value=str(share),
            source="facility amount / stated capital base",
        ),
    ]
    # The limit is judged on the exact share, never on the
    # rounded display figure: rounding first would wave through
    # every facility whose true share exceeds the limit by less
    # than half a display quantum (at the stated capital base,
    # amounts up to $25,012,499 against the 10% limit).
    passed = exact_share <= test.limit
    outcome.outcome = "pass" if passed else "fail"
    outcome.detail = (
        f"The facility is {share} of the stated capital base "
        f"({pack.capital_base}), against a limit of {test.limit}."
    )
    if exact_share != share:
        outcome.detail += (
            f" The unrounded share is {exact_share}; the limit "
            f"is judged on the unrounded figure."
        )
    return outcome


def _canonical_hash(descriptor: dict) -> str:
    blob = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _facts_digest(
    credit_file: CreditFile, borrower_pack: ComputationPack
) -> str:
    """A digest of exactly the credit-file facts the tests read, so
    the report id moves when — and only when — an input moves."""
    years = [
        f.fiscal_year for f in borrower_pack.figures
        if f.fiscal_year is not None
    ]
    facts = {
        "segment": credit_file.borrower.segment.value,
        "facility_type": credit_file.facility.facility_type.value,
        "facility_amount": str(credit_file.facility.amount),
        "guarantors": sorted(
            g.name for g in credit_file.facility.guarantors
        ),
        "checklist": [
            [i.item_id, i.required, i.satisfied_by_document_id is not None]
            for i in credit_file.checklist
        ],
        "documents": sorted(
            [d.document_id, d.kind.value, d.received_at.date().isoformat()]
            for d in credit_file.documents
        ),
        "latest_fiscal_year": max(years) if years else None,
    }
    return _canonical_hash(facts)


def evaluate_policy(
    credit_file: CreditFile,
    borrower_pack: ComputationPack,
    *,
    policy_version: str,
    global_pack: ComputationPack | None = None,
    as_of: date,
) -> PolicyReport:
    """Evaluate one credit file against ONE pinned pack version.

    Pure and deterministic: the same file, the same computation
    packs, the same pack version and the same as-of date reproduce
    the identical report, id included. Nothing here reaches for a
    newer pack on its own — re-judging an in-flight file under new
    policy is a decision someone makes by pinning the new version.
    """
    pack = get_policy_pack(policy_version)
    outcomes: list[PolicyTestOutcome] = []
    for test in pack.tests:
        if test.kind == "figure":
            outcomes.append(
                _eval_figure_test(test, credit_file, borrower_pack, global_pack)
            )
        elif test.fact == "required_documents":
            outcomes.append(_eval_required_documents(test, credit_file))
        elif test.fact == "statement_age":
            outcomes.append(_eval_statement_age(test, borrower_pack, as_of))
        elif test.fact == "appraisal_age":
            outcomes.append(_eval_appraisal_age(test, credit_file, as_of))
        elif test.fact == "concentration":
            outcomes.append(_eval_concentration(test, pack, credit_file))
        else:  # pragma: no cover - pack data is validated at build
            outcomes.append(_not_evaluable(
                _outcome_base(test),
                [f"file fact {test.fact!r}: no evaluator"],
            ))
    summary = {
        "pass": sum(1 for o in outcomes if o.outcome == "pass"),
        "fail": sum(1 for o in outcomes if o.outcome == "fail"),
        "not_evaluable": sum(
            1 for o in outcomes if o.outcome == "not_evaluable"
        ),
        "blocker_failures": sum(
            1 for o in outcomes
            if o.outcome == "fail" and o.severity == "blocker"
        ),
        "blocker_not_evaluable": sum(
            1 for o in outcomes
            if o.outcome == "not_evaluable" and o.severity == "blocker"
        ),
    }
    descriptor = {
        "policy_pack_id": pack.pack_id,
        "policy_version": pack.version,
        "policy_pack_digest": _canonical_hash(pack.model_dump(mode="json")),
        "credit_file_id": credit_file.credit_file_id,
        "computation_pack_id": borrower_pack.pack_id,
        "global_pack_id": global_pack.pack_id if global_pack else None,
        "as_of": as_of.isoformat(),
        "facts_digest": _facts_digest(credit_file, borrower_pack),
    }
    return PolicyReport(
        report_id=f"policy-{_canonical_hash(descriptor)[:16]}",
        credit_file_id=credit_file.credit_file_id,
        policy_pack_id=pack.pack_id,
        policy_version=pack.version,
        computation_pack_id=borrower_pack.pack_id,
        global_pack_id=global_pack.pack_id if global_pack else None,
        as_of=as_of,
        outcomes=outcomes,
        summary=summary,
    )
