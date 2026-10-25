"""Document disagreements: what happens when the sources do not
agree (round 8; design law 5).

A borrower's figures arrive from more than one kind of source —
the audited statements, the tax return, the management accounts —
and the kinds do not always tell the same story. The spreading
engine's older answer to two versions of one period was
supersession, and it stays the answer *within* a source type: a
corrected audited statement replaces the earlier audited statement.
Across source types there is nothing to supersede. A tax return is
not a newer version of the audited statements; it is a different
witness. This module is the discipline for witnesses:

- The spread works from ONE figure per line and period, chosen by
  a stated precedence (below) — never by averaging, never by
  whichever document happened to be filed last.
- Where two source types report different figures for a line,
  beyond the tolerances this policy states, the difference is a
  Disagreement record on the spread: every source's value, with
  its provenance, and the materiality verdict.
- The record rides with the spread into everything downstream:
  the computation engine flags figures computed over a materially
  disagreed line, and the memo carries material disagreements in
  a named section the verifier checks structurally.

The policy itself is data, versioned like the policy pack: the
tolerances and the materiality thresholds a disagreement was
judged under are recorded on every record it produced.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from pydantic import BaseModel, Field

from .models import Disagreement, DisagreementSource, ExtractedValue

# ---------------------------------------------------------------------
# The source-type vocabulary and the precedence between them.
#
# audited statements — the independent witness, and the figure the
# spread works from wherever it speaks. tax return — filed under
# penalty with the IRS, but prepared on tax rules, so it outranks
# management's own accounts without displacing the audit.
# management statements — the borrower's own telling, the first
# draft of the story the other two check.
# ---------------------------------------------------------------------

SOURCE_AUDITED = "audited_statement"
SOURCE_TAX = "tax_return"
SOURCE_MANAGEMENT = "management_statement"

SOURCE_PRECEDENCE: tuple[str, ...] = (
    SOURCE_AUDITED,
    SOURCE_TAX,
    SOURCE_MANAGEMENT,
)

SOURCE_TYPE_LABELS: dict[str, str] = {
    SOURCE_AUDITED: "audited statements",
    SOURCE_TAX: "tax return",
    SOURCE_MANAGEMENT: "management statements",
}


def source_type_for_document(kind: str, basis: str | None = None) -> str:
    """The source type of a document, from its kind on the credit
    file and — for financial statements — the basis the document
    declares for itself. A financial statement that does not
    declare a management basis is treated as the audited (or
    reviewed) statement the checklist requires."""
    if kind == "tax_return":
        return SOURCE_TAX
    if kind == "financial_statement" and (basis or "").strip().lower() == "management":
        return SOURCE_MANAGEMENT
    return SOURCE_AUDITED


def precedence_rank(source_type: str) -> int:
    """Lower outranks higher; an unlisted source type ranks last
    rather than crashing the spread — its figures still carry,
    and still disagree, as themselves."""
    try:
        return SOURCE_PRECEDENCE.index(source_type)
    except ValueError:
        return len(SOURCE_PRECEDENCE)


# ---------------------------------------------------------------------
# The disagreement policy, as versioned data.
# ---------------------------------------------------------------------


class DisagreementPolicy(BaseModel):
    """The tolerances and materiality thresholds disagreements are
    judged under, as data.

    `zero_tolerance_lines` names the lines on which the two most
    independent witnesses — the audited statements and the tax
    return — must agree exactly: any difference at all between
    them on a named line is a disagreement on the record. Every
    other pair, and every other line, gets `relative_tolerance`:
    differences inside it are the ordinary noise between bases of
    preparation and are not recorded. A recorded disagreement is
    material when its distance from the spread figure reaches
    `materiality_relative` of that figure, or
    `materiality_absolute` in dollars — the two tests a credit
    officer would apply by eye, stated so code applies them the
    same way every time.
    """

    policy_id: str = "aldergrove-disagreements"
    version: str = "v1"
    relative_tolerance: Decimal = Decimal("0.001")
    materiality_relative: Decimal = Decimal("0.02")
    materiality_absolute: Decimal = Decimal("50000")
    zero_tolerance_pair: tuple[str, str] = (SOURCE_AUDITED, SOURCE_TAX)
    zero_tolerance_lines: list[str] = Field(
        default_factory=lambda: ["revenue", "ebit", "net_income"]
    )


DISAGREEMENT_POLICY = DisagreementPolicy()


def get_disagreement_policy(version: str | None = None) -> DisagreementPolicy:
    """The shipped disagreement policy. One version exists; a
    later policy is a new version beside this one, never an edit
    to it — records name the version that judged them."""
    if version in (None, "", DISAGREEMENT_POLICY.version):
        return DISAGREEMENT_POLICY
    raise ValueError(f"unknown disagreement policy version {version!r}")


# ---------------------------------------------------------------------
# Detection: one line, one period, every source that reported it.
# ---------------------------------------------------------------------


def _beyond_tolerance(
    policy: DisagreementPolicy,
    *,
    field: str,
    working_type: str,
    other_type: str,
    delta_abs: Decimal,
    delta_rel: Decimal | None,
) -> bool:
    pair = {working_type, other_type}
    if (
        pair == set(policy.zero_tolerance_pair)
        and field in policy.zero_tolerance_lines
    ):
        return delta_abs > 0
    if delta_rel is None:  # the working figure is zero
        return delta_abs > 0
    return delta_rel > policy.relative_tolerance


def _is_material(
    policy: DisagreementPolicy,
    *,
    delta_abs: Decimal,
    delta_rel: Decimal | None,
) -> bool:
    if delta_abs >= policy.materiality_absolute:
        return True
    return delta_rel is not None and delta_rel >= policy.materiality_relative


def detect_disagreement(
    *,
    credit_file_id: str,
    field: str,
    line_label: str,
    fiscal_year: int,
    reported: list[tuple[str, ExtractedValue, str]],
    policy: DisagreementPolicy = DISAGREEMENT_POLICY,
) -> Disagreement | None:
    """Judge one line in one period across the source types that
    reported it.

    `reported` is one (source type, value, document SHA-256) per
    reporting source type — the value its current version states,
    at its first occurrence. Returns the Disagreement record, or
    None when every source agrees within tolerance (including the
    ordinary case of a single source: nothing to disagree with).

    The working figure is the highest-precedence reporter's; the
    record is created when ANY other reporter differs from it
    beyond tolerance, and carries every reporter's figure — a
    source inside tolerance still appears in the record when a
    sibling source forced it onto the record.
    """
    if len(reported) < 2:
        return None
    ordered = sorted(reported, key=lambda item: precedence_rank(item[0]))
    working_type, working_value, _ = ordered[0]
    working_amount = working_value.value

    sources: list[DisagreementSource] = []
    any_beyond = False
    material = False
    for source_type, value, sha256 in ordered:
        delta_abs = abs(value.value - working_amount)
        delta_rel = (
            delta_abs / abs(working_amount) if working_amount != 0 else None
        )
        sources.append(DisagreementSource(
            source_type=source_type,
            document_id=value.provenance.document_id,
            document_sha256=sha256,
            value=value.value,
            provenance=value.provenance,
            delta_abs=delta_abs if source_type != working_type else Decimal("0"),
            delta_rel=delta_rel if source_type != working_type else Decimal("0"),
        ))
        if source_type == working_type:
            continue
        if _beyond_tolerance(
            policy,
            field=field,
            working_type=working_type,
            other_type=source_type,
            delta_abs=delta_abs,
            delta_rel=delta_rel,
        ):
            any_beyond = True
            if _is_material(policy, delta_abs=delta_abs, delta_rel=delta_rel):
                material = True
    if not any_beyond:
        return None

    descriptor = {
        "credit_file_id": credit_file_id,
        "field": field,
        "fiscal_year": fiscal_year,
        "policy_version": policy.version,
        "sources": [
            {
                "source_type": source_type,
                "document_id": value.provenance.document_id,
                "value": str(value.value),
            }
            for source_type, value, _ in ordered
        ],
    }
    blob = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
    disagreement_id = f"dis-{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}"
    return Disagreement(
        disagreement_id=disagreement_id,
        credit_file_id=credit_file_id,
        line_key=field,
        line_label=line_label,
        fiscal_year=fiscal_year,
        working_source_type=working_type,
        working_value=working_amount,
        sources=sources,
        material=material,
        policy_version=policy.version,
    )
