"""Access control: one role policy governs retrieval (round 17).

Until this round, identity governed writes — the gates have
always required a named actor — but reads were open to whoever
could reach the API. That is not a posture a bank runs: a credit
file carries a borrower's full financial life, and who may read
which artefact is a control, not a convenience.

The policy is data (below), versioned like the lending-policy
packs. One module judges every read:

- Roles: `credit_officer` and `system` read the whole book,
  every family. `committee_member` reads the whole book except
  the cost ledger and the audit trail. `analyst_preparer` reads
  only their own book — files assigned to them, plus the
  unassigned pickup queue — and never the committee's decision
  record, the ledger, or the audit trail. `auditor_readonly`
  reads everything and writes nothing.
- Existence hiding: a caller with no read right on a file at
  all is answered exactly as if the file did not exist (the
  ordinary 404). A caller who may see the file but not the
  family they asked for gets a coded 403.
- Every refusal is recorded — an `access_denied` audit event on
  the file (when it exists) and an AccessDenial record in the
  store — and the denial list is itself a governed read.

A deployment puts its identity provider in front of the API and
sets the caller headers on every request; a request that names
no caller is the system itself (the local/demo posture the
console uses), which the policy grants in full.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CreditFile

#: The access policy's version, recorded in the module's data.
#: A policy change is a new version, reviewed like a lending-
#: policy pack change.
ACCESS_POLICY_VERSION = "2026.10.1"

# Artefact families a read can ask for.
FAMILY_FILES = "files"
FAMILY_DOCUMENTS = "documents"
FAMILY_EXTRACTIONS = "extractions"
FAMILY_SPREADS = "spreads"
FAMILY_COMPUTATIONS = "computations"
FAMILY_POLICY = "policy"
FAMILY_MEMOS = "memos"
FAMILY_DECISIONS = "decisions"
FAMILY_RATINGS = "ratings"
FAMILY_COVENANTS = "covenants"
FAMILY_BREACH_CASES = "breach_cases"
FAMILY_REVIEWS = "reviews"
FAMILY_LEDGER = "ledger"
FAMILY_AUDIT = "audit"

ALL_FAMILIES: frozenset[str] = frozenset(
    {
        FAMILY_FILES,
        FAMILY_DOCUMENTS,
        FAMILY_EXTRACTIONS,
        FAMILY_SPREADS,
        FAMILY_COMPUTATIONS,
        FAMILY_POLICY,
        FAMILY_MEMOS,
        FAMILY_DECISIONS,
        FAMILY_RATINGS,
        FAMILY_COVENANTS,
        FAMILY_BREACH_CASES,
        FAMILY_REVIEWS,
        FAMILY_LEDGER,
        FAMILY_AUDIT,
    }
)

_PREPARER_FAMILIES: frozenset[str] = frozenset(
    {
        FAMILY_FILES,
        FAMILY_DOCUMENTS,
        FAMILY_EXTRACTIONS,
        FAMILY_SPREADS,
        FAMILY_COMPUTATIONS,
        FAMILY_POLICY,
        FAMILY_MEMOS,
        FAMILY_RATINGS,
        FAMILY_COVENANTS,
        FAMILY_BREACH_CASES,
        FAMILY_REVIEWS,
    }
)

_COMMITTEE_FAMILIES: frozenset[str] = _PREPARER_FAMILIES | {FAMILY_DECISIONS}

SCOPE_WHOLE_BOOK = "whole_book"
SCOPE_ASSIGNED = "assigned"

# The role policy, as data: for each role, the families it may
# read, the scope those reads run over, and whether the role may
# write at all.
ROLE_POLICY: dict[str, dict] = {
    "system": {
        "families": ALL_FAMILIES,
        "scope": SCOPE_WHOLE_BOOK,
        "write": True,
    },
    "credit_officer": {
        "families": ALL_FAMILIES,
        "scope": SCOPE_WHOLE_BOOK,
        "write": True,
    },
    "committee_member": {
        "families": _COMMITTEE_FAMILIES,
        "scope": SCOPE_WHOLE_BOOK,
        "write": True,
    },
    "analyst_preparer": {
        "families": _PREPARER_FAMILIES,
        "scope": SCOPE_ASSIGNED,
        "write": True,
    },
    "auditor_readonly": {
        "families": ALL_FAMILIES,
        "scope": SCOPE_WHOLE_BOOK,
        "write": False,
    },
}

ROLES: frozenset[str] = frozenset(ROLE_POLICY)

# Coded refusal reasons — the full vocabulary a denial can carry.
REASON_ROLE_NOT_PERMITTED = "role_not_permitted"
REASON_NOT_ASSIGNEE = "not_assignee"
REASON_READONLY_ROLE = "readonly_role"
REASON_UNKNOWN_ROLE = "unknown_role"
REASON_CALLER_REQUIRED = "caller_required"


@dataclass(frozen=True)
class Caller:
    """Who is asking: a name and the role they act under."""

    caller_id: str
    role: str


#: The caller a request acts as when it names none: the system
#: itself (local/demo posture — see the module docstring).
SYSTEM_CALLER = Caller(caller_id="system", role="system")


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason_code: str = ""
    # True when the refusal must be answered as non-existence:
    # the caller has no read right on the file at all.
    hide_existence: bool = False


def _caller_problems(caller: Caller) -> str:
    """A coded refusal for a malformed caller, or "" if sound."""
    if caller.role not in ROLE_POLICY:
        return REASON_UNKNOWN_ROLE
    if caller.role != "system" and not caller.caller_id.strip():
        return REASON_CALLER_REQUIRED
    return ""


def file_visible(caller: Caller, credit_file: CreditFile) -> bool:
    """Whether the caller may know this file exists at all."""
    policy = ROLE_POLICY.get(caller.role)
    if policy is None:
        return False
    if policy["scope"] == SCOPE_WHOLE_BOOK:
        return True
    return credit_file.assigned_to in (None, caller.caller_id)


def visible_files(
    caller: Caller, files: list[CreditFile]
) -> list[CreditFile]:
    """The scope filter for list surfaces: what the caller sees."""
    return [f for f in files if file_visible(caller, f)]


def authorize_read(
    caller: Caller, credit_file: CreditFile, family: str
) -> AccessDecision:
    """Judge one read of one family of one file."""
    problem = _caller_problems(caller)
    if problem:
        return AccessDecision(False, problem)
    policy = ROLE_POLICY[caller.role]
    if not file_visible(caller, credit_file):
        # No read right on the file at all: the answer is
        # non-existence, whatever family was asked for.
        return AccessDecision(
            False, REASON_NOT_ASSIGNEE, hide_existence=True
        )
    if family not in policy["families"]:
        return AccessDecision(False, REASON_ROLE_NOT_PERMITTED)
    return AccessDecision(True)


def authorize_global(caller: Caller, family: str) -> AccessDecision:
    """Judge a portfolio-level read (no single file in scope).

    Portfolio surfaces belong to whole-book roles: an
    assigned-scope caller asking for one is refused by code,
    even for a family their role otherwise holds.
    """
    problem = _caller_problems(caller)
    if problem:
        return AccessDecision(False, problem)
    policy = ROLE_POLICY[caller.role]
    if family not in policy["families"]:
        return AccessDecision(False, REASON_ROLE_NOT_PERMITTED)
    if policy["scope"] != SCOPE_WHOLE_BOOK:
        return AccessDecision(False, REASON_ROLE_NOT_PERMITTED)
    return AccessDecision(True)


def authorize_write(caller: Caller) -> AccessDecision:
    """Judge a write before any gate logic runs.

    The read-only role is refused here, on every write path —
    gate logic never sees the attempt. Other roles pass this
    judge and face the gates' own identity and role checks, as
    they always have.
    """
    problem = _caller_problems(caller)
    if problem:
        return AccessDecision(False, problem)
    if not ROLE_POLICY[caller.role]["write"]:
        return AccessDecision(False, REASON_READONLY_ROLE)
    return AccessDecision(True)
