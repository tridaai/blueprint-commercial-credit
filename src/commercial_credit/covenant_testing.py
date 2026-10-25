"""Covenant testing and breach cases (round 11; design law 6).

Round 10 derived the schedule — the clocks. This module is the
math half of law 6, and it is deliberately thin: a covenant test
is executed by the pipeline through the engines that already
exist (the spread is rebuilt by the spreading engine, the figure
is computed by the computation engine and read out of the stored
pack's own covenant result), and this module holds the
*discipline* around that execution:

- **Identity.** A test result is content-addressed over the
  instance, the covenant version tested, the spread and pack the
  figure came from, and the outcome — the same test over the
  same inputs is the same artefact, so re-running stores nothing
  twice and the tick can run as often as the bank likes.
- **Restatement standing.** Every result pins the period sources
  it rested on (the exact document versions of the tested
  period). When the borrower restates that period, the current
  spread's sources move and the result's standing derives as
  superseded_by_restatement: the original record stands — it was
  the truth on the inputs of its day — and a re-test is owed.
- **Breach discipline.** A failure opens a breach case. A breach
  is resolved only by a coded act of a named officer: cured by a
  later passing re-test of the same covenant (linked by result
  id), waived only on the authority of a committee decision of
  this credit file (a free-text waiver names no authority and is
  refused), escalated and defaulted under coded reasons. There
  is no delete and no silent close: a breach that nobody acts on
  stays open, ageing against its cure deadline, on every list.
"""

from __future__ import annotations

from datetime import date

from .computations import _OPERATORS
from .errors import ValidationError
from .memo import _canonical_hash
from .models import (
    BREACH_CURED,
    BREACH_DEFAULTED,
    BREACH_ESCALATED,
    BREACH_OPEN,
    BREACH_TERMINAL_STATES,
    BREACH_UNDER_REVIEW,
    BREACH_WAIVED,
    TEST_PASS,
    Covenant,
    CovenantAmendment,
    CovenantSet,
    CovenantTestResult,
    Spread,
    TestedPeriodSource,
)

# ---------------------------------------------------------------------
# Coded vocabularies (versioned, like the gates'). A breach
# escalation or a move to the default path is a credit act: it
# carries one of these codes and a named officer, never a shrug.
# A cure carries no code — the passing re-test is its evidence —
# and a waiver carries no code either: the committee decision is
# its authority.
# ---------------------------------------------------------------------

BREACH_REASONS_VERSION = "breach-resolution-reasons-v1"

BREACH_ESCALATION_REASON_CODES: dict[str, str] = {
    "cure_period_expiring": (
        "the cure period is running out without a cure in "
        "prospect: the breach goes to the watchlist"
    ),
    "borrower_unresponsive": (
        "the borrower has not engaged on the breach: escalated "
        "for relationship and workout attention"
    ),
    "repeat_breach": (
        "the same covenant has breached before on this facility: "
        "escalated as a pattern, not an event"
    ),
    "projected_further_breach": (
        "the figures in hand project a further or wider breach: "
        "escalated before the next test date"
    ),
    "other": "another reason, stated in the escalation note",
}

BREACH_DEFAULT_REASON_CODES: dict[str, str] = {
    "cure_period_expired_uncured": (
        "the cure period expired with the breach uncured and no "
        "waiver granted: the facility follows the default path"
    ),
    "borrower_default_other_facility": (
        "the borrower is in default under another facility: "
        "cross-default handling applies"
    ),
    "material_adverse_change": (
        "a material adverse change in the borrower's condition "
        "accompanies the breach"
    ),
    "other": "another reason, stated in the default note",
}

# Roles that may act on a breach. The preparer's side of the
# house (the relationship manager who brought the credit) does
# not resolve breaches on it.
BREACH_ACTOR_ROLES = frozenset({
    "credit_officer",
    "senior_credit_officer",
    "chief_credit_officer",
    "committee_chair",
    "committee_member",
})


def compare(value, operator: str, threshold) -> bool:
    """The one comparison a covenant test makes — the engine's
    own operator table, so a test result can never disagree with
    the headroom the computation pack recorded for the same
    definition."""
    return _OPERATORS[operator](value, threshold)


# ---------------------------------------------------------------------
# Result identity + period-source pins
# ---------------------------------------------------------------------


def test_result_id(
    *,
    instance_id: str,
    covenant_id: str,
    spread_id: str | None,
    computation_pack_id: str | None,
    outcome: str,
    supersedes_result_id: str | None,
) -> str:
    """The content address of one test result: the instance, the
    covenant version tested (its id names its version), the
    spread and pack the figure came from, the outcome, and the
    result it supersedes. Awaiting-inputs results pin no spread —
    the absence is the fact — and pin the instance's period
    instead, so a later arrival of the same missing statements
    cannot mint a second awaiting record for the same want."""
    descriptor = {
        "instance": instance_id,
        "covenant": covenant_id,
        "spread": spread_id,
        "pack": computation_pack_id,
        "outcome": outcome,
        "supersedes": supersedes_result_id,
    }
    return f"ctres-{_canonical_hash(descriptor)[:16]}"


def breach_id_for(result_id: str) -> str:
    """One breach per failing result, derived from it."""
    return f"breach-{_canonical_hash({'result': result_id})[:16]}"


def period_sources_for(
    spread: Spread, fiscal_year: int | None
) -> list[TestedPeriodSource]:
    """The period sources of one fiscal year, as the spread
    recorded them — the pin a test result carries."""
    if fiscal_year is None:
        return []
    return [
        TestedPeriodSource(
            fiscal_year=source.fiscal_year,
            source_type=source.source_type,
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            version=source.version,
        )
        for source in spread.period_sources
        if source.fiscal_year == fiscal_year
    ]


def result_standing(
    result: CovenantTestResult, spread: Spread | None
) -> str:
    """A result's standing against the file's current spread.

    A result that ran over figures (pass / fail / not_evaluable)
    is superseded when the tested period's sources have moved
    since — a document version it pinned is no longer the
    version the spread rests on, or the period is gone from the
    spread. An awaiting_inputs result is a record of a want, not
    of figures: restatement standing does not apply to it — it is
    superseded by the arrival of its statements, which the next
    real result records in its place."""
    from .models import RESULT_CURRENT, RESULT_SUPERSEDED_BY_RESTATEMENT

    if result.outcome == "awaiting_inputs" or spread is None:
        return RESULT_CURRENT
    if result.fiscal_year is None or result.fiscal_year not in spread.periods:
        return RESULT_SUPERSEDED_BY_RESTATEMENT
    current = {
        (s.source_type, s.document_id, s.version)
        for s in spread.period_sources
        if s.fiscal_year == result.fiscal_year
    }
    pinned = {
        (s.source_type, s.document_id, s.version)
        for s in result.period_sources
    }
    if pinned != current:
        return RESULT_SUPERSEDED_BY_RESTATEMENT
    return RESULT_CURRENT


# ---------------------------------------------------------------------
# Covenant lineage. A cure must come from a re-test of the SAME
# covenant — which, after an amendment, means the same lineage,
# not the same version id.
# ---------------------------------------------------------------------


def lineage_root(
    covenant_id: str,
    sets: list[CovenantSet],
    amendments: list[CovenantAmendment],
) -> str | None:
    """The root covenant id of the lineage `covenant_id` belongs
    to, walking supersession back across the set and its
    amendments — or None when the id names no covenant here."""
    by_id: dict[str, Covenant] = {}
    for covenant_set in sets:
        for covenant in covenant_set.covenants:
            by_id[covenant.covenant_id] = covenant
    for amendment in amendments:
        by_id[amendment.covenant.covenant_id] = amendment.covenant
    node = by_id.get(covenant_id)
    if node is None:
        return None
    seen: set[str] = set()
    while node.supersedes_covenant_id and node.supersedes_covenant_id not in seen:
        seen.add(node.supersedes_covenant_id)
        parent = by_id.get(node.supersedes_covenant_id)
        if parent is None:
            break
        node = parent
    return node.covenant_id


def same_lineage(
    covenant_id_a: str,
    covenant_id_b: str,
    sets: list[CovenantSet],
    amendments: list[CovenantAmendment],
) -> bool:
    root_a = lineage_root(covenant_id_a, sets, amendments)
    return root_a is not None and root_a == lineage_root(
        covenant_id_b, sets, amendments
    )


# ---------------------------------------------------------------------
# Breach state machine. open -> under_review -> cured | waived |
# escalated | defaulted_path; escalated -> cured | waived |
# defaulted_path. Cured, waived and defaulted are terminal. The
# checks here are pure: the pipeline supplies the facts (the
# cure's result, the waiver's decision) and this function refuses
# or allows.
# ---------------------------------------------------------------------

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    BREACH_OPEN: frozenset({
        BREACH_UNDER_REVIEW, BREACH_ESCALATED, BREACH_CURED,
        BREACH_WAIVED,
    }),
    BREACH_UNDER_REVIEW: frozenset({
        BREACH_ESCALATED, BREACH_CURED, BREACH_WAIVED,
        BREACH_DEFAULTED,
    }),
    BREACH_ESCALATED: frozenset({
        BREACH_CURED, BREACH_WAIVED, BREACH_DEFAULTED,
    }),
}


def require_breach_actor(actor_id: str, actor_role: str, *, what: str) -> None:
    """A named officer in a role that may act on breaches."""
    if not actor_id or not actor_id.strip():
        raise ValidationError(
            f"{what} requires a named actor: the officer id is empty"
        )
    if actor_role not in BREACH_ACTOR_ROLES:
        raise ValidationError(
            f"actor {actor_id!r} carries role {actor_role!r}, "
            f"which may not act on a breach; roles are "
            f"{sorted(BREACH_ACTOR_ROLES)}"
        )


def validate_breach_transition(
    *,
    state: str,
    target: str,
    what: str,
) -> None:
    """The state machine's wall: a terminal breach moves for
    nobody, and every live state names the moves it allows."""
    if state in BREACH_TERMINAL_STATES:
        raise ValidationError(
            f"the breach is {state} — a terminal state: {what} is "
            "refused, the resolution stands as recorded"
        )
    allowed = _ALLOWED_TRANSITIONS.get(state, frozenset())
    if target not in allowed:
        raise ValidationError(
            f"a breach in state {state!r} cannot move to "
            f"{target!r} ({what}); allowed moves are "
            f"{sorted(allowed)}"
        )


def validate_cure(
    *,
    cure_result: CovenantTestResult | None,
    breach_covenant_id: str,
    breach_result_id: str,
    breach_test_date: date,
    sets: list[CovenantSet],
    amendments: list[CovenantAmendment],
    credit_file_id: str,
) -> CovenantTestResult:
    """A cure is a passing re-test of the same covenant, recorded
    after the failing test — linked by id, or it is not a cure.
    Anything else offered as one is refused by name: a result
    that failed, a result for another covenant, the failing
    result itself, a result from before the breach."""
    if cure_result is None:
        raise ValidationError(
            "a cure names the passing re-test result that cures "
            "the breach: no result by that id exists on this "
            "credit file"
        )
    if cure_result.credit_file_id != credit_file_id:
        raise ValidationError(
            f"cure result {cure_result.result_id} belongs to "
            f"credit file {cure_result.credit_file_id}, not "
            f"{credit_file_id}"
        )
    if cure_result.result_id == breach_result_id:
        raise ValidationError(
            "the failing result cannot cure its own breach: a "
            "cure is a LATER re-test that passes"
        )
    if cure_result.outcome != TEST_PASS:
        raise ValidationError(
            f"cure result {cure_result.result_id} has outcome "
            f"{cure_result.outcome!r}: only a passing re-test "
            "cures a breach"
        )
    if not same_lineage(
        cure_result.covenant_id, breach_covenant_id, sets, amendments
    ):
        raise ValidationError(
            f"cure result {cure_result.result_id} tests covenant "
            f"{cure_result.covenant_id}, which is not the "
            f"breached covenant {breach_covenant_id} or any "
            "version of it: a cure must re-test the same covenant"
        )
    if cure_result.test_date < breach_test_date:
        raise ValidationError(
            f"cure result {cure_result.result_id} tests period "
            f"ending {cure_result.test_date.isoformat()}, before "
            f"the breached test date "
            f"{breach_test_date.isoformat()}: a cure is recorded "
            "after the failure it answers"
        )
    return cure_result


def validate_waiver_decision(
    *,
    decision,
    credit_file_id: str,
) -> None:
    """A waiver stands on a committee decision's authority: the
    decision must exist, belong to this credit file, and be an
    approval. A waiver that names nothing — or names a decline
    or a deferral — is a free-text waiver wearing a reference,
    and is refused the same way."""
    if decision is None:
        raise ValidationError(
            "a breach waiver requires the committee decision "
            "that authorised it: a waiver without a decision "
            "reference is a free-text waiver, and is refused"
        )
    if decision.credit_file_id != credit_file_id:
        raise ValidationError(
            f"waiver decision {decision.decision_id} belongs to "
            f"credit file {decision.credit_file_id}, not "
            f"{credit_file_id}"
        )
    if decision.outcome not in ("approve", "approve_with_conditions"):
        raise ValidationError(
            f"waiver decision {decision.decision_id} is "
            f"{decision.outcome!r}: only an approval can authorise "
            "a breach waiver"
        )
