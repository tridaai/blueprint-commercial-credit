"""The credit officer gate (round 6; design laws 4 and 8).

A composed draft memo decides nothing. It goes to a named credit
officer, who may approve it as-is, approve it with edits, return
it for rework, or flag items on it for the committee. This module
holds the gate's discipline — the vocabulary and the pure checks;
`CreditPipeline` runs the workflow over the stores.

The discipline, in code:

- **Named identity, always.** Every gate action records who acted
  and in what role. Approvals, returns and flags require a credit
  officer role; an anonymous or role-less action is refused before
  anything else is looked at.
- **Status is derived, never stored on the artefact.** A memo's
  review status — draft, in_officer_review, returned,
  officer_approved — is a function of its recorded review events.
  Artefacts stay immutable; the event stream is the authority.
- **Edits face the verifier again.** An officer's edited memo is
  a candidate until `verify_memo` has cleared the full edited
  text: an introduced figure that does not trace refuses the
  approval, exactly as it refused the draft.
- **The exceptions section is not narrative.** It is the pinned
  policy report rendered verbatim, so it is locked at this gate:
  an officer discusses exceptions in the risks section, but the
  exception list itself — every fail, every not_evaluable — is
  not theirs to edit, drop or soften.
- **Returns are coded.** A return for rework names reasons from
  the versioned vocabulary below. A free-text note may ride
  alongside; it may never substitute for a code.
- **A declared conflict recuses.** An officer who flags a
  conflict of interest on a memo cannot then approve it; the
  refusal names the flag, and a different officer must decide.
"""

from __future__ import annotations

from .errors import ValidationError
from .memo import _canonical_hash
from .models import (
    CreditMemo,
    FigureChange,
    FigureTrace,
    MemoDiff,
    MemoReviewEvent,
    MemoSection,
    SectionChange,
)

# Memo review statuses. `officer_approved` is also the born status
# of an officer-final artefact; the others are derived for drafts.
STATUS_DRAFT = "draft"
STATUS_IN_REVIEW = "in_officer_review"
STATUS_RETURNED = "returned"
STATUS_APPROVED = "officer_approved"

# Roles that may decide at this gate. Submission for review is
# open to any named actor (the relationship manager who prepared
# the file, say); deciding is not.
OFFICER_ROLES = frozenset({
    "credit_officer",
    "senior_credit_officer",
    "chief_credit_officer",
})

FLAG_KINDS = frozenset({"committee_attention", "conflict_of_interest"})

# Sections whose text the officer may not edit. The exceptions
# section renders the pinned policy report; editing it could only
# drop or soften an outcome the report recorded.
LOCKED_SECTIONS = frozenset({"policy_exceptions"})

# The coded return vocabulary, versioned: a return recorded under
# this version names these codes, and a later vocabulary change
# never reinterprets a recorded return. `other` is a code like
# any other, but it is only lawful with a note saying what it is.
RETURN_REASONS_VERSION = "officer-return-reasons-v1"
RETURN_REASON_CODES: dict[str, str] = {
    "insufficient_analysis": (
        "the financial analysis does not support a decision on "
        "the artefacts pinned to the draft"
    ),
    "narrative_inaccurate": (
        "a statement in the narrative does not match the pinned "
        "artefacts it cites"
    ),
    "exceptions_unaddressed": (
        "a policy exception is carried without the officer-level "
        "discussion the committee needs"
    ),
    "mitigants_unsupported": (
        "a mitigant is relied on that the credit file does not "
        "record"
    ),
    "recommendation_unsupported": (
        "the recommendation does not follow from the analysis "
        "and the policy outcomes"
    ),
    "conditions_incomplete": (
        "the conditions do not address the open exceptions or "
        "the outstanding required items"
    ),
    "stale_inputs": (
        "an input the draft relies on is stale and must be "
        "refreshed before the memo can be decided"
    ),
    "other": "another reason, stated in the return note",
}


def require_identity(actor_id: str, actor_role: str, *, what: str) -> None:
    """A named actor with a named role, or the action is refused.

    Anonymous actions are refused mechanically — an empty or
    blank id or role names nobody, and the gate records nobody.
    """
    if not actor_id or not actor_id.strip():
        raise ValidationError(
            f"{what} requires a named actor: the officer id is empty"
        )
    if not actor_role or not actor_role.strip():
        raise ValidationError(
            f"{what} requires the actor's role: the role is empty"
        )


def require_officer(officer_id: str, officer_role: str, *, what: str) -> None:
    """A named credit officer, or the action is refused."""
    require_identity(officer_id, officer_role, what=what)
    if officer_role not in OFFICER_ROLES:
        raise ValidationError(
            f"{what} requires a credit officer role; "
            f"{officer_role!r} is not one of {sorted(OFFICER_ROLES)}"
        )


def validate_return_reasons(reason_codes: list[str], note: str) -> None:
    """Coded reasons, from the current vocabulary — or no return.

    A return with no codes is a free-text return, and free-text
    returns are refused: the preparer (and the record) must be
    able to tell exactly what to rework.
    """
    codes = list(reason_codes or [])
    if not codes:
        raise ValidationError(
            "a return for rework requires at least one coded "
            f"reason from {RETURN_REASONS_VERSION}; a note alone "
            "is not a reason"
        )
    unknown = [c for c in codes if c not in RETURN_REASON_CODES]
    if unknown:
        raise ValidationError(
            f"unknown return reason code(s): {', '.join(unknown)}; "
            f"the vocabulary is {RETURN_REASONS_VERSION}: "
            f"{', '.join(sorted(RETURN_REASON_CODES))}"
        )
    if "other" in codes and not (note or "").strip():
        raise ValidationError(
            "return reason 'other' requires a note stating the "
            "reason — 'other' without one is a free-text return"
        )


def derive_status(memo: CreditMemo, events: list[MemoReviewEvent]) -> str:
    """A memo's review status, from its recorded review events.

    An officer-final artefact is born `officer_approved`. For a
    draft, the events decide, in recorded order: submission opens
    the review, a return closes it as returned, an approval
    closes it as approved. Flags and recompositions do not move
    the status.
    """
    if memo.status == STATUS_APPROVED:
        return STATUS_APPROVED
    status = STATUS_DRAFT
    for event in events:
        if event.memo_id != memo.memo_id:
            continue
        if event.action == "submitted_for_review":
            status = STATUS_IN_REVIEW
        elif event.action == "returned_for_rework":
            status = STATUS_RETURNED
        elif event.action in ("approved_as_is", "approved_with_edits"):
            status = STATUS_APPROVED
    return status


def apply_edits(
    draft: CreditMemo, edits: dict[str, list[str]]
) -> list[MemoSection]:
    """The draft's sections with the officer's paragraph edits in.

    An edit replaces one section's paragraphs, whole. Tables are
    machine-rendered from the spread and are not editable;
    neither is any section the gate locks, nor a section the memo
    does not have. The candidate this returns is not a memo yet —
    it becomes one only when the figure verifier clears it.
    """
    known = {s.key for s in draft.sections}
    for key in edits:
        if key not in known:
            raise ValidationError(
                f"edit names section {key!r}, which memo "
                f"{draft.memo_id} does not have; its sections are "
                f"{[s.key for s in draft.sections]}"
            )
        if key in LOCKED_SECTIONS:
            raise ValidationError(
                f"section {key!r} is not editable at the officer "
                "gate: it renders the pinned policy report "
                "verbatim, so an exception cannot be dropped or "
                "softened by editing it — discuss the exception "
                "in risks_mitigants instead"
            )
    sections: list[MemoSection] = []
    for section in draft.sections:
        if section.key in edits:
            sections.append(section.model_copy(
                update={"paragraphs": list(edits[section.key])}
            ))
        else:
            sections.append(section.model_copy(deep=True))
    return sections


def _traces_by_section(memo: CreditMemo) -> dict[str, list[FigureTrace]]:
    grouped: dict[str, list[FigureTrace]] = {}
    for trace in memo.verification.figures:
        grouped.setdefault(trace.section_key, []).append(trace)
    return grouped


def compute_memo_diff(
    draft: CreditMemo,
    final: CreditMemo,
    *,
    officer_id: str,
    officer_role: str,
    action: str,
    note: str = "",
) -> MemoDiff:
    """The draft-vs-final record of an approval (design law 8).

    Section changes carry the before/after paragraphs of every
    section whose text moved. Figure changes walk each section's
    figure occurrences in reading order, draft against final: an
    occurrence that changed in place is `altered` (an edit that
    swaps one traced figure for another lands here, as one
    removal plus one addition would hide the swap); occurrences
    beyond the shorter side are `removed` or `added`. Each change
    names the artefact + field the figure traced to on its side —
    its trace status.
    """
    section_changes: list[SectionChange] = []
    final_sections = {s.key: s for s in final.sections}
    for before in draft.sections:
        after = final_sections.get(before.key)
        if after is None:
            section_changes.append(SectionChange(
                section_key=before.key,
                paragraphs_before=list(before.paragraphs),
                paragraphs_after=[],
                tables_changed=bool(before.tables),
            ))
            continue
        if before.paragraphs != after.paragraphs or before.tables != after.tables:
            section_changes.append(SectionChange(
                section_key=before.key,
                paragraphs_before=list(before.paragraphs),
                paragraphs_after=list(after.paragraphs),
                tables_changed=before.tables != after.tables,
            ))

    draft_traces = _traces_by_section(draft)
    final_traces = _traces_by_section(final)
    section_order = [s.key for s in draft.sections]
    section_order += [
        key for key in final_traces if key not in section_order
    ]
    if "header" in draft_traces or "header" in final_traces:
        section_order = ["header"] + section_order
    figure_changes: list[FigureChange] = []
    for key in section_order:
        old_seq = draft_traces.get(key, [])
        new_seq = final_traces.get(key, [])
        for idx in range(max(len(old_seq), len(new_seq))):
            old = old_seq[idx] if idx < len(old_seq) else None
            new = new_seq[idx] if idx < len(new_seq) else None
            if old is not None and new is not None:
                if (old.text, old.value) == (new.text, new.value):
                    continue
                figure_changes.append(FigureChange(
                    section_key=key, kind="altered",
                    old_text=old.text, old_value=old.value,
                    old_trace=old.source,
                    new_text=new.text, new_value=new.value,
                    new_trace=new.source,
                ))
            elif old is not None:
                figure_changes.append(FigureChange(
                    section_key=key, kind="removed",
                    old_text=old.text, old_value=old.value,
                    old_trace=old.source,
                ))
            elif new is not None:
                figure_changes.append(FigureChange(
                    section_key=key, kind="added",
                    new_text=new.text, new_value=new.value,
                    new_trace=new.source,
                ))

    descriptor = {
        "from_memo_id": draft.memo_id,
        "to_memo_id": final.memo_id,
        "officer_id": officer_id,
        "officer_role": officer_role,
        "action": action,
        "section_changes": [c.model_dump(mode="json") for c in section_changes],
        "figure_changes": [c.model_dump(mode="json") for c in figure_changes],
        "note": note,
    }
    return MemoDiff(
        diff_id=f"diff-{_canonical_hash(descriptor)[:16]}",
        credit_file_id=draft.credit_file_id,
        from_memo_id=draft.memo_id,
        to_memo_id=final.memo_id,
        officer_id=officer_id,
        officer_role=officer_role,
        action=action,
        section_changes=section_changes,
        figure_changes=figure_changes,
        note=note,
    )
