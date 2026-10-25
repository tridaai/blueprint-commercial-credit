"""The committee gate (round 7; design laws 3 and 4).

The officer gate decided that a memo is sound. The committee gate
decides the credit. This module holds the gate's discipline — the
vocabularies and the pure checks; `CreditPipeline` runs the
workflow over the stores.

The discipline, in code:

- **A quorum of named members, or no decision.** Every decision
  records the members who made it — distinct named people, each
  with a role — and the quorum it required. The quorum is at
  least two, always: a committee of one is a sole approver, and
  law 4 forbids one at this gate. The officer who approved the
  memo may sit on the committee, but never as its only member,
  and nobody records a decision they were not part of.
- **Declines carry coded reasons, tied to evidence (law 3).** A
  reason is a code from the versioned adverse-action vocabulary
  PLUS a mechanical tie: a failed or not-evaluable policy test
  of the memo's pinned report (by test id), a figure in the
  memo's verification record (by its trace), or a required
  checklist item missing from the file (by item id). A reason
  citing a test that PASSED, a figure that was never traced, or
  a document that is on file is refused, naming the offender.
  Free-text reasons do not exist in this path: there is no field
  for one. The borrower-facing adverse-action notice is then
  GENERATED from the codes and their ties — accurate because it
  can only say what the evidence already recorded.
- **Conditions are records, not prose.** A conditional approval
  carries structured conditions. A financial covenant's
  threshold must trace into the memo's pinned artefacts — the
  same trace universe the figure verifier uses — and the trace
  is recorded on the condition. Round 10 schedules them.
- **Deferrals say what would change the answer.** A deferral
  carries coded reasons and named re-presentment requirements,
  and returns the memo to the officer: its derived status reads
  `deferred`, it is not a draft and never reopens composition,
  and re-presentment is a fresh convening on the same
  officer-final artefact.
- **Status is derived, never stored.** Officer status first
  (officer_gate.derive_status); committee states extend it from
  the same event stream — convened, then the recorded decision's
  outcome. A decided memo cannot be decided again; a deferred
  one can be re-presented and decided anew.
"""

from __future__ import annotations

from decimal import Decimal

from .errors import ValidationError
from .memo import MemoInputs, build_trace_universe, fmt_ratio
from .models import (
    AdverseActionReason,
    CommitteeCondition,
    CommitteeDecision,
    CommitteeMember,
    CreditFile,
    CreditMemo,
    MemoReviewEvent,
    PolicyReport,
)
from .officer_gate import STATUS_APPROVED, derive_status

# Committee-era memo statuses, extending the officer gate's.
STATUS_IN_COMMITTEE = "in_committee"
STATUS_COMMITTEE_APPROVED = "committee_approved"
STATUS_COMMITTEE_CONDITIONAL = "committee_approved_conditional"
STATUS_DECLINED = "declined"
STATUS_DEFERRED = "deferred"

# Where a committee outcome lands a memo's derived status. A
# deferral lands `deferred`: back with the credit officer, the
# artefact unchanged, awaiting re-presentment.
OUTCOME_STATUSES = {
    "approve": STATUS_COMMITTEE_APPROVED,
    "approve_with_conditions": STATUS_COMMITTEE_CONDITIONAL,
    "decline": STATUS_DECLINED,
    "defer": STATUS_DEFERRED,
}

OUTCOMES = frozenset(OUTCOME_STATUSES)

# Who may sit on the committee. Officers may; dedicated committee
# roles may; a relationship manager (the preparer's side of the
# house) may not — law 4's second gate is not the first gate's
# preparer wearing another hat.
COMMITTEE_MEMBER_ROLES = frozenset({
    "committee_chair",
    "committee_member",
    "credit_officer",
    "senior_credit_officer",
    "chief_credit_officer",
})

CONDITION_TYPES = frozenset({
    "financial_covenant",
    "reporting_requirement",
    "documentation",
    "other",
})

CONDITION_OPERATORS = frozenset({">=", "<=", ">", "<"})

# ---------------------------------------------------------------------
# The adverse-action vocabulary (law 3). Versioned: a decline
# recorded under this version names these codes, and a later
# vocabulary change never reinterprets a recorded decline. Each
# code carries the borrower-facing statement the notice renders —
# specific, and accurate by construction, because a code may be
# recorded only with a tie that proves it (tie_problem below).
# ---------------------------------------------------------------------

ADVERSE_REASONS_VERSION = "adverse-action-reasons-v1"

# Policy tests whose FAILURE speaks to debt service coverage.
_COVERAGE_TESTS = frozenset({
    "min_dscr", "min_global_dscr", "min_fixed_charge_coverage",
})
# Policy tests whose failure is a staleness finding.
_STALENESS_TESTS = frozenset({"financial_statement_age", "appraisal_age"})

ADVERSE_REASON_CODES: dict[str, str] = {
    "debt_service_coverage_below_policy": (
        "Debt service coverage does not meet the bank's policy "
        "minimum for this facility"
    ),
    "leverage_above_policy_limit": (
        "Leverage exceeds the bank's policy limit for the "
        "borrower's segment"
    ),
    "concentration_above_policy_limit": (
        "The requested facility exceeds the bank's single-borrower "
        "concentration limit"
    ),
    "required_documentation_missing": (
        "Required documentation is missing from the credit file"
    ),
    "financial_information_insufficient": (
        "The financial information provided does not allow the "
        "bank to evaluate the application"
    ),
    "statements_or_valuation_stale": (
        "Financial statements or valuation information are outside "
        "the bank's currency limits"
    ),
    "key_figure_shortfall": (
        "A figure recorded in the credit memorandum does not "
        "support approval"
    ),
}

# ---------------------------------------------------------------------
# The deferral vocabulary. A deferral is not a soft decline: it
# names why the committee cannot decide yet and what must change
# before the memo comes back.
# ---------------------------------------------------------------------

DEFERRAL_REASONS_VERSION = "committee-deferral-reasons-v1"

DEFERRAL_REASON_CODES: dict[str, str] = {
    "awaiting_required_documents": (
        "required documents are outstanding and must be on file "
        "before the committee decides"
    ),
    "awaiting_updated_financials": (
        "more current financial statements must be provided"
    ),
    "awaiting_appraisal_or_valuation": (
        "a current appraisal or valuation must be provided"
    ),
    "guarantor_information_pending": (
        "guarantor financial information is incomplete"
    ),
    "further_analysis_required": (
        "the committee requires further analysis before deciding"
    ),
    "other": "another reason, stated in the decision note",
}


def require_member(member: CommitteeMember, *, what: str) -> None:
    """A named committee member with a committee role, or refuse."""
    if not member.member_id or not member.member_id.strip():
        raise ValidationError(
            f"{what} names a committee member with an empty id: "
            "a decision records named members only"
        )
    if member.member_role not in COMMITTEE_MEMBER_ROLES:
        raise ValidationError(
            f"{what}: member {member.member_id!r} carries role "
            f"{member.member_role!r}, which may not sit on the "
            f"committee; roles are {sorted(COMMITTEE_MEMBER_ROLES)}"
        )


def validate_quorum(
    members: list[CommitteeMember],
    quorum_required: int,
    *,
    recorder_id: str,
    officer_id: str | None,
) -> None:
    """The quorum discipline, mechanically: enough distinct named
    members, the recorder among them, nobody deciding alone."""
    if quorum_required < 2:
        raise ValidationError(
            f"a committee quorum of {quorum_required} is refused: "
            "the quorum is at least 2 — a committee of one is a "
            "sole approver (design law 4)"
        )
    for member in members:
        require_member(member, what="a committee decision")
    ids = [m.member_id for m in members]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValidationError(
            "committee members must be distinct people; "
            f"duplicated: {', '.join(duplicates)}"
        )
    if len(ids) < quorum_required:
        raise ValidationError(
            f"a committee decision requires a quorum of "
            f"{quorum_required} named members; {len(ids)} recorded"
        )
    if recorder_id not in ids:
        raise ValidationError(
            f"the decision is recorded by {recorder_id!r}, who is "
            "not one of its recorded members: nobody records a "
            "committee decision they were not part of"
        )
    if officer_id is not None and ids == [officer_id]:
        raise ValidationError(
            f"officer {officer_id} approved this memo and is its "
            "only recorded committee member: the approving officer "
            "cannot be the committee's sole member (design law 4)"
        )


def validate_conditions(
    conditions: list[CommitteeCondition], inputs: MemoInputs
) -> list[CommitteeCondition]:
    """Structure + trace discipline for conditions.

    Returns the conditions as they will be stored — a financial
    covenant comes back carrying the trace its threshold was found
    in. Anything malformed or untraceable refuses the decision.
    """
    universe: dict[Decimal, str] | None = None
    figure_keys: set[str] | None = None
    stored: list[CommitteeCondition] = []
    for condition in conditions:
        if condition.type not in CONDITION_TYPES:
            raise ValidationError(
                f"condition {condition.condition_id!r} has unknown "
                f"type {condition.type!r}; types are "
                f"{sorted(CONDITION_TYPES)}"
            )
        if not condition.due_cadence or not condition.due_cadence.strip():
            raise ValidationError(
                f"condition {condition.condition_id!r} "
                f"({condition.type}) names no due/review cadence: "
                "a condition without a cadence is a wish, not a "
                "condition"
            )
        updates: dict = {}
        if condition.type == "financial_covenant":
            if not condition.metric_key:
                raise ValidationError(
                    f"financial covenant {condition.condition_id!r} "
                    "names no metric"
                )
            if condition.operator not in CONDITION_OPERATORS:
                raise ValidationError(
                    f"financial covenant {condition.condition_id!r} "
                    f"has operator {condition.operator!r}; operators "
                    f"are {sorted(CONDITION_OPERATORS)}"
                )
            if condition.threshold is None:
                raise ValidationError(
                    f"financial covenant {condition.condition_id!r} "
                    "names no threshold"
                )
            if figure_keys is None:
                figure_keys = {
                    f.key
                    for pack in (inputs.pack, inputs.global_pack)
                    if pack is not None
                    for f in pack.figures
                }
            if condition.metric_key not in figure_keys:
                raise ValidationError(
                    f"financial covenant {condition.condition_id!r} "
                    f"names metric {condition.metric_key!r}, which "
                    "is not a figure of the computation pack the "
                    "memo pins"
                )
            if universe is None:
                universe = build_trace_universe(inputs)
            trace = universe.get(condition.threshold)
            if trace is None:
                raise ValidationError(
                    f"financial covenant {condition.condition_id!r} "
                    f"sets a threshold of {condition.threshold} "
                    "that does not trace to the memo's pinned "
                    "artefacts, a spread value, a policy threshold "
                    "or a stated credit-file fact: the figure is "
                    "refused, and with it the decision"
                )
            updates["threshold_trace"] = trace
        elif condition.type == "reporting_requirement":
            if not condition.report_what or not condition.report_what.strip():
                raise ValidationError(
                    f"reporting condition {condition.condition_id!r} "
                    "does not say what must be reported"
                )
        elif condition.type == "documentation":
            if not condition.checklist_item_id:
                raise ValidationError(
                    f"documentation condition {condition.condition_id!r} "
                    "names no checklist item"
                )
            item = inputs.credit_file.checklist_item(
                condition.checklist_item_id
            )
            if item is None:
                raise ValidationError(
                    f"documentation condition {condition.condition_id!r} "
                    f"names checklist item "
                    f"{condition.checklist_item_id!r}, which is not "
                    "on this credit file"
                )
        else:  # other
            if not condition.note or not condition.note.strip():
                raise ValidationError(
                    f"condition {condition.condition_id!r} of type "
                    "'other' requires a note stating the condition"
                )
        stored.append(condition.model_copy(update=updates))
    return stored


def _tie_problem(
    reason: AdverseActionReason,
    report: PolicyReport,
    memo: CreditMemo,
    credit_file: CreditFile,
) -> str | None:
    """Why this reason's tie does not hold, or None when it does.

    The tie is the reason's evidence. Each code names the ties
    that can prove it; anything else — a passed test, an
    untraced figure, a document that is on file, a code wearing
    the wrong evidence — is a refusal naming the offender.
    """
    code = reason.code
    if code not in ADVERSE_REASON_CODES:
        return (
            f"unknown adverse-action reason code {code!r}; the "
            f"vocabulary is {ADVERSE_REASONS_VERSION}: "
            f"{', '.join(sorted(ADVERSE_REASON_CODES))}"
        )
    if reason.tie_kind == "policy_test":
        if not reason.test_id:
            return f"reason {code!r} is tied to a policy test but names none"
        outcome = report.outcome(reason.test_id)
        if outcome is None:
            return (
                f"reason {code!r} cites policy test "
                f"{reason.test_id!r}, which the memo's pinned report "
                f"({report.report_id}) does not contain"
            )
        if outcome.outcome == "pass":
            return (
                f"reason {code!r} cites policy test "
                f"{reason.test_id!r} ({outcome.name}), which the "
                "pinned report records as a PASS: a passed test "
                "cannot ground an adverse-action reason"
            )
        # The outcome is fail or not_evaluable; the code must be
        # the one that evidence speaks to.
        if code == "debt_service_coverage_below_policy":
            if reason.test_id not in _COVERAGE_TESTS:
                return (
                    f"reason {code!r} must be tied to a coverage "
                    f"test {sorted(_COVERAGE_TESTS)}; it cites "
                    f"{reason.test_id!r}"
                )
            if outcome.outcome != "fail":
                return (
                    f"reason {code!r} cites test {reason.test_id!r} "
                    f"as {outcome.outcome}: coverage that could not "
                    "be evaluated is an insufficiency of "
                    "information, not a coverage failure — use "
                    "'financial_information_insufficient'"
                )
        elif code == "leverage_above_policy_limit":
            if reason.test_id != "max_leverage_by_segment" or outcome.outcome != "fail":
                return (
                    f"reason {code!r} must be tied to a FAIL of "
                    f"max_leverage_by_segment; it cites "
                    f"{reason.test_id!r} as {outcome.outcome}"
                )
        elif code == "concentration_above_policy_limit":
            if (
                reason.test_id != "max_single_borrower_concentration"
                or outcome.outcome != "fail"
            ):
                return (
                    f"reason {code!r} must be tied to a FAIL of "
                    f"max_single_borrower_concentration; it cites "
                    f"{reason.test_id!r} as {outcome.outcome}"
                )
        elif code == "required_documentation_missing":
            if reason.test_id != "required_documents_complete":
                return (
                    f"reason {code!r} tied to a policy test must "
                    f"cite required_documents_complete; it cites "
                    f"{reason.test_id!r} (a missing document may "
                    "instead be tied as a file_fact)"
                )
        elif code == "financial_information_insufficient":
            if outcome.outcome != "not_evaluable":
                return (
                    f"reason {code!r} cites test {reason.test_id!r} "
                    f"as {outcome.outcome}: a test that was "
                    "evaluated and failed is a substantive failure "
                    "— cite the code that names it"
                )
        elif code == "statements_or_valuation_stale":
            if reason.test_id not in _STALENESS_TESTS or outcome.outcome != "fail":
                return (
                    f"reason {code!r} must be tied to a FAIL of a "
                    f"staleness test {sorted(_STALENESS_TESTS)}; it "
                    f"cites {reason.test_id!r} as {outcome.outcome}"
                )
        else:  # key_figure_shortfall
            return (
                f"reason {code!r} must be tied to a traced figure "
                "of the memo, not to a policy test"
            )
        return None
    if reason.tie_kind == "figure":
        if code != "key_figure_shortfall":
            return (
                f"reason {code!r} is tied to a figure; only "
                "'key_figure_shortfall' is proven by a figure tie"
            )
        if not reason.figure_text or not reason.figure_value:
            return (
                f"reason {code!r} is tied to a figure but names "
                "neither its text nor its value"
            )
        traced = any(
            t.text == reason.figure_text
            and t.value == reason.figure_value
            and (reason.figure_source is None or t.source == reason.figure_source)
            for t in memo.verification.figures
        )
        if not traced:
            return (
                f"reason {code!r} cites figure "
                f"{reason.figure_text!r} (value "
                f"{reason.figure_value!r}), which the decided "
                "memo's verification record does not trace: an "
                "untraced figure cannot ground an adverse-action "
                "reason"
            )
        return None
    if reason.tie_kind == "file_fact":
        if code != "required_documentation_missing":
            return (
                f"reason {code!r} is tied to a file fact; only "
                "'required_documentation_missing' is proven by a "
                "missing document"
            )
        if not reason.checklist_item_id:
            return (
                f"reason {code!r} is tied to a missing document "
                "but names no checklist item"
            )
        item = credit_file.checklist_item(reason.checklist_item_id)
        if item is None:
            return (
                f"reason {code!r} names checklist item "
                f"{reason.checklist_item_id!r}, which is not on "
                "this credit file"
            )
        if not item.required:
            return (
                f"reason {code!r} names checklist item "
                f"{reason.checklist_item_id!r} ({item.label}), "
                "which is not a required item"
            )
        if item.satisfied:
            return (
                f"reason {code!r} names checklist item "
                f"{reason.checklist_item_id!r} ({item.label}) as "
                "missing, but the file records it as satisfied: "
                "the document is on file"
            )
        return None
    return (
        f"reason {code!r} has tie kind {reason.tie_kind!r}; a "
        "reason must be tied as policy_test, figure or file_fact — "
        "an untied reason is a free-text reason, and free-text "
        "reasons are impossible in the decline path"
    )


def validate_adverse_reasons(
    reasons: list[AdverseActionReason],
    report: PolicyReport,
    memo: CreditMemo,
    credit_file: CreditFile,
) -> None:
    """Every decline reason coded and tied, or the decline is
    refused with the offending reason named."""
    if not reasons:
        raise ValidationError(
            "a decline requires at least one coded adverse-action "
            f"reason from {ADVERSE_REASONS_VERSION}: a decline "
            "without coded reasons is refused (design law 3)"
        )
    for reason in reasons:
        problem = _tie_problem(reason, report, memo, credit_file)
        if problem is not None:
            raise ValidationError(problem)


def validate_deferral(
    reason_codes: list[str], requirements: list[str], note: str
) -> None:
    """Coded deferral reasons + named re-presentment requirements,
    or no deferral."""
    codes = list(reason_codes or [])
    if not codes:
        raise ValidationError(
            "a deferral requires at least one coded reason from "
            f"{DEFERRAL_REASONS_VERSION}; a note alone is not a "
            "reason"
        )
    unknown = [c for c in codes if c not in DEFERRAL_REASON_CODES]
    if unknown:
        raise ValidationError(
            f"unknown deferral reason code(s): "
            f"{', '.join(unknown)}; the vocabulary is "
            f"{DEFERRAL_REASONS_VERSION}: "
            f"{', '.join(sorted(DEFERRAL_REASON_CODES))}"
        )
    if "other" in codes and not (note or "").strip():
        raise ValidationError(
            "deferral reason 'other' requires a note stating the "
            "reason — 'other' without one is a free-text deferral"
        )
    named = [r for r in (requirements or []) if r and r.strip()]
    if not named:
        raise ValidationError(
            "a deferral must name what must change before "
            "re-presentment (its re-presentment requirements); a "
            "deferral that names nothing sends the memo back with "
            "no path forward"
        )


def derive_full_status(
    memo: CreditMemo, events: list[MemoReviewEvent]
) -> str:
    """A memo's status across both gates, from its event stream.

    The officer gate's derivation runs first; a memo that has not
    reached officer approval has no committee state. Past it, the
    committee events decide, in recorded order: convening opens
    the committee state, a recorded decision lands its outcome's
    status, and a re-convening after a deferral opens it again.
    """
    status = derive_status(memo, events)
    if status != STATUS_APPROVED:
        return status
    for event in events:
        if event.memo_id != memo.memo_id:
            continue
        if event.action == "committee_convened":
            status = STATUS_IN_COMMITTEE
        elif event.action == "committee_decided" and event.committee_outcome:
            status = OUTCOME_STATUSES[event.committee_outcome]
    return status


# ---------------------------------------------------------------------
# The adverse-action notice: generated, never hand-typed.
# ---------------------------------------------------------------------


def _reason_evidence_clause(
    reason: AdverseActionReason,
    report: PolicyReport,
    credit_file: CreditFile,
) -> str:
    """The evidence clause of one notice line, from the tie alone."""
    if reason.tie_kind == "policy_test":
        outcome = report.outcome(reason.test_id or "")
        if outcome is None:  # validated before rendering; defensive
            return ""
        verdict = (
            "failed" if outcome.outcome == "fail" else "could not be evaluated"
        )
        clause = (
            f"Policy test {outcome.name} ({outcome.test_id}) "
            f"{verdict} on the bank's pinned policy report"
        )
        figure_reads = [
            r for r in outcome.values_read if r.input.startswith("figure:")
        ]
        if figure_reads and outcome.threshold is not None:
            clause += (
                f": the recorded figure is {figure_reads[0].value} "
                f"against a requirement of {outcome.operator} "
                f"{fmt_ratio(outcome.threshold).rstrip('×')}"
            )
        if outcome.outcome == "not_evaluable" and outcome.missing_inputs:
            clause += (
                " — the missing input is "
                + "; ".join(outcome.missing_inputs)
            )
        return clause + f". Policy citation: {outcome.citation}"
    if reason.tie_kind == "figure":
        return (
            f"The credit memorandum records {reason.figure_text} "
            f"({reason.figure_source})."
        )
    item = credit_file.checklist_item(reason.checklist_item_id or "")
    if item is None:  # validated before rendering; defensive
        return ""
    return (
        f"Required item {item.item_id} ({item.label}) is not on "
        f"file. Requirement: {item.requirement_source}."
    )


def render_adverse_action_notice(
    decision: CommitteeDecision,
    memo: CreditMemo,
    report: PolicyReport,
    credit_file: CreditFile,
) -> str:
    """The borrower-facing adverse-action notice, generated from
    the decline's coded reasons and their ties.

    Nothing in this text is hand-typed at decision time: the
    statements are the vocabulary's, the evidence clauses are
    read off the pinned report, the memo's verification record
    and the credit file. That is what makes the reasons specific
    and accurate in the ECOA/Reg B sense — the notice can only
    say what the record already proves.
    """
    if decision.outcome != "decline":
        raise ValidationError(
            f"decision {decision.decision_id} is "
            f"{decision.outcome!r}, not a decline: there is no "
            "adverse-action notice to render"
        )
    facility = credit_file.facility
    lines = [
        "NOTICE OF ACTION TAKEN",
        f"Borrower: {credit_file.borrower.legal_name}",
        f"Facility requested: {facility.facility_type.value.replace('_', ' ')}",
        f"Decision date: {decision.decided_at.date().isoformat()}",
        f"Memorandum: `{memo.memo_id}`",
        "",
        "The credit committee has declined this application. "
        "The principal reason(s) for this decision are:",
        "",
    ]
    for idx, reason in enumerate(decision.adverse_reasons, start=1):
        statement = ADVERSE_REASON_CODES[reason.code]
        clause = _reason_evidence_clause(reason, report, credit_file)
        lines.append(f"{idx}. {statement}. {clause}")
    lines += [
        "",
        f"Reasons recorded under {decision.reason_vocabulary} as "
        "coded adverse-action reasons, each tied to the evidence "
        "cited. Recorded decision: "
        f"`{decision.decision_id}`.",
    ]
    return "\n".join(lines)
