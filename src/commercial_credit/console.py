"""Round 14: the officer console's read models.

The console is a single static page (console_page.CONSOLE_HTML)
served by the API. It holds no state and no rules of its own:
every number it shows comes from the pipeline's existing reads,
and every action it offers posts to an existing gated route —
the console adds NO write semantics. This module holds the two
read aggregates the page needs that no single existing route
carries:

  queue_view      one row per credit file: its pipeline stage
                  (derived from the same artefacts the gates
                  read — memo statuses, committee decisions,
                  covenant sets), its committee-readiness
                  blocker count (the round 9 report, computed
                  fresh), its current risk rating where one
                  stands (round 13), and its open breach count.
  workspace_view  one credit file in credit-officer order: the
                  artefact kinds of rounds 1–13 assembled into a
                  single document the workspace renders section
                  by section. Each section is the artefact as
                  its own route serves it; nothing is recomputed
                  here beyond the derivations the pipeline
                  itself owns.

And the vocabularies the action forms must offer: the coded
reason sets are the gates' own constants, served read-only so a
form can only ever offer codes the gate will recognise.

CONSOLE_READS / CONSOLE_ACTIONS are the console's route
manifest — every route the page calls, declared next to the
page so the test suite and the eval gate can scan the page's
own path literals against it and against the app's route
table. A console action that is not an existing gated route
cannot ship: the scan fails.
"""

from __future__ import annotations

from datetime import date

from .committee import (
    ADVERSE_REASON_CODES,
    ADVERSE_REASONS_VERSION,
    DEFERRAL_REASON_CODES,
    DEFERRAL_REASONS_VERSION,
    OUTCOMES,
)
from .covenant_testing import (
    BREACH_DEFAULT_REASON_CODES,
    BREACH_ESCALATION_REASON_CODES,
    BREACH_REASONS_VERSION,
)
from .errors import CreditError, NotFoundError
from .officer_gate import RETURN_REASON_CODES, RETURN_REASONS_VERSION
from .reviews import (
    REVIEW_REASON_CODES,
    REVIEW_STANCES,
    REVIEW_VOCABULARY_VERSION,
)
from .scorecard import OVERRIDE_REASON_CODES, OVERRIDE_VOCABULARY_VERSION

# The stages the queue groups by, in pipeline order. `declined`
# stands beside `approved`: both are committee endpoints, and a
# queue that hid its declines would be lying by omission.
STAGES = (
    "intake",
    "spreading",
    "memo draft",
    "officer review",
    "committee",
    "approved",
    "declined",
    "monitoring",
)

# Every route the console page calls, as (method, path template)
# — the surface the round 14 scan grades. Reads first.
CONSOLE_READS: list[tuple[str, str]] = [
    ("GET", "/console/queue"),
    ("GET", "/console/workspace/{credit_file_id}"),
    ("GET", "/console/vocabularies"),
    ("GET", "/covenant-due-work"),
    ("GET", "/annual-review-due-work"),
    ("GET", "/breach-cases"),
]

# Every write the console offers. Each one is an existing gated
# route from an earlier round — the officer gate (round 6), the
# committee gate (round 7), breach resolution (round 11),
# annual reviews (round 12), the rating override (round 13).
# The console invents none of them.
CONSOLE_ACTIONS: list[tuple[str, str]] = [
    ("POST", "/applications/{credit_file_id}/memos/{memo_id}/submit"),
    ("POST", "/applications/{credit_file_id}/memos/{memo_id}/approve"),
    ("POST", "/applications/{credit_file_id}/memos/{memo_id}/return"),
    ("POST", "/applications/{credit_file_id}/memos/{memo_id}/flag"),
    ("POST", "/applications/{credit_file_id}/memos/{memo_id}/committee/convene"),
    ("POST", "/applications/{credit_file_id}/memos/{memo_id}/committee/decide"),
    ("POST", "/applications/{credit_file_id}/risk-rating"),
    (
        "POST",
        "/applications/{credit_file_id}/risk-ratings/{assignment_id}/override",
    ),
    ("POST", "/applications/{credit_file_id}/breach-cases/{breach_id}/review"),
    ("POST", "/applications/{credit_file_id}/breach-cases/{breach_id}/escalate"),
    ("POST", "/applications/{credit_file_id}/breach-cases/{breach_id}/cure"),
    ("POST", "/applications/{credit_file_id}/breach-cases/{breach_id}/waive"),
    ("POST", "/applications/{credit_file_id}/breach-cases/{breach_id}/default"),
    ("POST", "/applications/{credit_file_id}/annual-reviews/{review_number}/pack"),
    (
        "POST",
        "/applications/{credit_file_id}/annual-reviews/{review_number}/outcome",
    ),
]


def route_manifest() -> list[tuple[str, str]]:
    """Every route the console page may call: reads + actions."""
    return list(CONSOLE_READS) + list(CONSOLE_ACTIONS)


def vocabularies_view() -> dict:
    """The coded vocabularies the console's forms offer.

    Read-only and verbatim from the gates' own modules: a code
    the console offers is a code the gate recognises, because
    both read the same constant. Versions ride along so the
    form can name the vocabulary it picked from.
    """
    return {
        "officer_return_reasons": {
            "version": RETURN_REASONS_VERSION,
            "codes": dict(RETURN_REASON_CODES),
        },
        "committee_outcomes": sorted(OUTCOMES),
        "adverse_action_reasons": {
            "version": ADVERSE_REASONS_VERSION,
            "codes": dict(ADVERSE_REASON_CODES),
        },
        "committee_deferral_reasons": {
            "version": DEFERRAL_REASONS_VERSION,
            "codes": dict(DEFERRAL_REASON_CODES),
        },
        "breach_reasons": {
            "version": BREACH_REASONS_VERSION,
            "escalation_codes": dict(BREACH_ESCALATION_REASON_CODES),
            "default_codes": dict(BREACH_DEFAULT_REASON_CODES),
        },
        "annual_review": {
            "version": REVIEW_VOCABULARY_VERSION,
            "stances": sorted(REVIEW_STANCES),
            "codes": dict(REVIEW_REASON_CODES),
        },
        "rating_override_reasons": {
            "version": OVERRIDE_VOCABULARY_VERSION,
            "codes": dict(OVERRIDE_REASON_CODES),
        },
    }


def _stage_of(pipeline, credit_file) -> str:  # noqa: ANN001, ANN202
    """A file's pipeline stage, derived from stored artefacts.

    The derivation reads what the gates read: a covenant set
    means the credit closed and is being monitored; otherwise
    the most advanced memo status decides; a file with no memo
    is spreading if a spread stands, intake if not. Nothing is
    stored — the stage is recomputed from the record each time,
    so it can never disagree with the record.
    """
    credit_file_id = credit_file.credit_file_id
    if pipeline.store.list_covenant_sets(credit_file_id):
        return "monitoring"
    memos = pipeline.list_memos(credit_file_id)
    if memos:
        status = memos[-1].status
        if status in ("committee_approved", "committee_approved_conditional"):
            return "approved"
        if status == "declined":
            return "declined"
        if status in ("in_committee", "deferred", "officer_approved"):
            return "committee"
        if status == "in_review":
            return "officer review"
        return "memo draft"  # draft | returned
    if pipeline.store.list_spreads(credit_file_id):
        return "spreading"
    return "intake"


def queue_view(pipeline, *, as_of: date) -> list[dict]:  # noqa: ANN001
    """The pipeline queue: one row per credit file.

    The readiness blocker count is the round 9 report computed
    fresh at `as_of` — the same number the committee gate would
    meet. A file whose readiness cannot be computed yet (no
    spread, no pack) carries None, stated as such, never a
    borrowed zero.
    """
    rows: list[dict] = []
    for credit_file in pipeline.store.list_credit_files():
        credit_file_id = credit_file.credit_file_id
        try:
            blockers: int | None = pipeline.compute_readiness(
                credit_file_id, as_of=as_of
            ).blocker_count
        except CreditError:
            blockers = None
        rating = None
        current = pipeline.current_rating(credit_file_id)
        if current is not None:
            rating = {
                "assignment_id": current["assignment"].assignment_id,
                "assigned_grade": current["assigned_grade"],
                "assigned_band": current["assigned_band"],
                "final_grade": current["final_grade"],
                "final_band": current["final_band"],
            }
        breaches = pipeline.list_breach_cases(
            as_of=as_of, credit_file_id=credit_file_id
        )
        open_breaches = sum(
            1
            for b in breaches
            if b["state"] not in ("cured", "waived", "defaulted_path")
        )
        rows.append(
            {
                "credit_file_id": credit_file_id,
                "borrower_name": credit_file.borrower.legal_name,
                "segment": credit_file.borrower.segment,
                "facility_type": credit_file.facility.facility_type,
                "facility_amount": str(credit_file.facility.amount),
                "facility_term_months": credit_file.facility.term_months,
                "stage": _stage_of(pipeline, credit_file),
                "readiness_blockers": blockers,
                "rating": rating,
                "open_breaches": open_breaches,
                "updated_at": credit_file.updated_at.isoformat(),
            }
        )
    return rows


def _dump(model):  # noqa: ANN001, ANN202
    return model.model_dump(mode="json") if model is not None else None


def _schedule_json(pipeline, credit_file_id: str, *, as_of: date) -> dict:  # noqa: ANN001
    schedule = pipeline.covenant_schedule(credit_file_id, as_of=as_of)
    return {
        "credit_file_id": schedule["credit_file_id"],
        "borrower": schedule["borrower"],
        "as_of": schedule["as_of"].isoformat(),
        "sets": [
            {
                "covenant_set": _dump(view["covenant_set"]),
                "current_covenants": [
                    _dump(c) for c in view["current_covenants"]
                ],
                "instances": [_dump(i) for i in view["instances"]],
            }
            for view in schedule["sets"]
        ],
    }


def _review_schedule_json(pipeline, credit_file_id: str, *, as_of: date) -> dict:  # noqa: ANN001
    schedule = pipeline.annual_review_schedule(credit_file_id, as_of=as_of)
    return {
        "credit_file_id": schedule["credit_file_id"],
        "borrower": schedule["borrower"],
        "as_of": schedule["as_of"].isoformat(),
        "sets": [
            {
                "covenant_set": _dump(view["covenant_set"]),
                "anchor_date": view["anchor_date"].isoformat(),
                "anchor_basis": view["anchor_basis"],
                "instances": [
                    {
                        "credit_file_id": i.credit_file_id,
                        "set_id": i.set_id,
                        "review_number": i.review_number,
                        "anchor_date": i.anchor_date.isoformat(),
                        "period_start": i.period_start.isoformat(),
                        "due_date": i.due_date.isoformat(),
                        "state": i.state,
                        "days_until_due": i.days_until_due,
                        "outcome_id": i.outcome_id,
                        "pack_id": i.pack_id,
                    }
                    for i in view["instances"]
                ],
            }
            for view in schedule["sets"]
        ],
    }


def workspace_view(pipeline, credit_file_id: str, *, as_of: date) -> dict:  # noqa: ANN001
    """One credit file in credit-officer order, every artefact
    kind the earlier rounds produce, as its own route serves it.

    Sections that cannot exist yet for a young file are present
    and empty — the workspace never invents a section's content,
    and an absent artefact reads as an empty list, not an error.
    """
    credit_file = pipeline.store.get_credit_file(credit_file_id)
    if credit_file is None:
        raise NotFoundError(f"credit file {credit_file_id} not found")
    store = pipeline.store

    try:
        spread = _dump(pipeline.get_spread(credit_file_id))
    except CreditError:
        spread = None
    try:
        readiness = _dump(pipeline.compute_readiness(credit_file_id, as_of=as_of))
    except CreditError:
        readiness = None

    packs = [_dump(p) for p in store.list_computation_packs(credit_file_id)]
    reports = [_dump(r) for r in store.list_policy_reports(credit_file_id)]
    memos = pipeline.list_memos(credit_file_id)
    memo_views = []
    for memo in memos:
        history = pipeline.memo_history(credit_file_id, memo.memo_id)
        committee = pipeline.committee_state(credit_file_id, memo.memo_id)
        memo_views.append(
            {
                "memo": _dump(memo),
                "status": memo.status,
                "verification": _dump(memo.verification),
                "events": [_dump(e) for e in history["events"]],
                "committee": {
                    "status": committee["status"],
                    "decisions": [
                        _dump(d) for d in committee["decisions"]
                    ],
                    "latest_decision": _dump(committee["latest_decision"]),
                },
            }
        )

    rating_views = []
    for assignment in pipeline.list_rating_assignments(credit_file_id):
        view = pipeline.rating_view(assignment)
        rating_views.append(
            {
                "assignment": _dump(assignment),
                "assigned_grade": view["assigned_grade"],
                "assigned_band": view["assigned_band"],
                "final_grade": view["final_grade"],
                "final_band": view["final_band"],
                "override": _dump(view["override"]),
                "overrides": [_dump(o) for o in view["overrides"]],
            }
        )

    due_work = [
        entry
        for entry in pipeline.covenant_due_work(as_of=as_of)
        if entry["credit_file_id"] == credit_file_id
    ]
    due_work_json = []
    for entry in due_work:
        item = {
            "borrower": entry["borrower"],
            "credit_file_id": entry["credit_file_id"],
            "kind": entry.get("kind", "covenant_schedule"),
            "instance": _dump(entry.get("instance")),
            "review_instance": None,
        }
        if entry.get("review_instance") is not None:
            i = entry["review_instance"]
            item["review_instance"] = {
                "credit_file_id": i.credit_file_id,
                "set_id": i.set_id,
                "review_number": i.review_number,
                "due_date": i.due_date.isoformat(),
                "state": i.state,
                "days_until_due": i.days_until_due,
                "outcome_id": i.outcome_id,
                "pack_id": i.pack_id,
            }
        due_work_json.append(item)

    return {
        "credit_file_id": credit_file_id,
        "as_of": as_of.isoformat(),
        "stage": _stage_of(pipeline, credit_file),
        "file": _dump(credit_file),
        "checklist": [_dump(i) for i in credit_file.checklist],
        "documents": [_dump(d) for d in credit_file.documents],
        "readiness": readiness,
        "spread": spread,
        "adjustments": [
            _dump(a) for a in store.list_adjustments(credit_file_id)
        ],
        "computation_packs": packs,
        "policy_reports": reports,
        "disagreements": [
            _dump(d) for d in pipeline.list_disagreements(credit_file_id)
        ],
        "memos": memo_views,
        "ratings": rating_views,
        "covenant_sets": [
            _dump(s) for s in store.list_covenant_sets(credit_file_id)
        ],
        "covenant_schedule": _schedule_json(
            pipeline, credit_file_id, as_of=as_of
        ),
        "covenant_test_results": pipeline.covenant_test_results(
            credit_file_id
        ),
        "breach_cases": pipeline.list_breach_cases(
            as_of=as_of, credit_file_id=credit_file_id
        ),
        "annual_review_schedule": _review_schedule_json(
            pipeline, credit_file_id, as_of=as_of
        ),
        "annual_review_packs": [
            _dump(p) for p in pipeline.list_review_packs(credit_file_id)
        ],
        "annual_review_outcomes": [
            _dump(o) for o in pipeline.list_review_outcomes(credit_file_id)
        ],
        "committee_referrals": [
            _dump(r) for r in pipeline.list_committee_referrals(credit_file_id)
        ],
        "due_work": due_work_json,
    }
