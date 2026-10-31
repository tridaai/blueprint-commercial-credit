"""Round 19: portfolio views — the book, read whole.

Every earlier surface reads one file at a time, or lists one
artefact kind across files. What none of them could say is what
the book looks like: how much exposure stands in each segment,
how much of it stands under a failing policy report, which cure
clocks run out first, which inputs block the book's readiness
and on which files, and what the committee has actually decided
— by outcome and by coded reason. Those are the questions a
portfolio reader (a chief credit officer, a committee chair, an
auditor sampling the book) brings, and until this round the
only way to answer them was to open every file and add by hand.

The views here are read models in the round 14 console's
discipline: computed fresh from the artefacts of record on each
request, stored nowhere, every figure the sum or count of code
over stored artefacts — no model touches a number (law 1), and
`verify_portfolio_view` re-performs every figure by an
independent walk so the fold itself is graded, not trusted.

Access is the round 17 policy, followed exactly:

- The builder receives ONLY the caller's visible book (the API
  scope-filters with `access.visible_files` first). A file the
  caller may not see contributes nothing — not to a total, not
  to a count, not even to `files_in_scope`. Existence hiding
  holds at the portfolio level because the builder never sees
  the hidden files at all.
- Each section is governed by one artefact family, declared in
  SECTIONS below. A caller whose role does not hold the family
  gets the section withheld under the coded reason the policy
  itself uses (`role_not_permitted`); the API records that
  refusal in the denial list, like every refusal since round
  17. The view states its discipline on its face: the access
  policy version, the caller's scope and role, and how many
  files the view ran over.

Sections:

  exposure_by_segment       facility amounts summed by borrower
                            segment (family: files)
  exposure_by_policy_outcome  the same book classified by each
                            file's standing policy report — the
                            last report saved — from its own
                            summary (family: policy)
  covenant_watch            open breach cases across the book
                            ordered by the cure clock, not by
                            failure date, with the overdue count
                            and a census of every case by state
                            (family: breach_cases)
  staleness_census          the book's committee-readiness at
                            the as-of date: ready, blocked (each
                            file named with its blocker inputs),
                            and not-yet-computable, with the
                            blockers histogrammed by input
                            (family: files — readiness rides the
                            files family on its own routes)
  adverse_rollup            the latest committee decision per
                            memo across the book: counts by
                            outcome, declines by adverse reason
                            code, deferrals by deferral code —
                            the coded vocabularies of round 7
                            (family: decisions)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .access import (
    ACCESS_POLICY_VERSION,
    FAMILY_BREACH_CASES,
    FAMILY_DECISIONS,
    FAMILY_FILES,
    FAMILY_POLICY,
    REASON_ROLE_NOT_PERMITTED,
    ROLE_POLICY,
    Caller,
)
from .committee import (
    ADVERSE_REASONS_VERSION,
    DEFERRAL_REASONS_VERSION,
    OUTCOMES,
)
from .errors import CreditError
from .models import BREACH_TERMINAL_STATES

# The sections of the portfolio view and the artefact family
# that governs each, in the order the view presents them. The
# map is data on purpose: a section's family is a policy fact,
# reviewed like the access policy itself.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("exposure_by_segment", FAMILY_FILES),
    ("exposure_by_policy_outcome", FAMILY_POLICY),
    ("covenant_watch", FAMILY_BREACH_CASES),
    ("staleness_census", FAMILY_FILES),
    ("adverse_rollup", FAMILY_DECISIONS),
)

SECTION_FAMILIES: dict[str, str] = dict(SECTIONS)

# The standing-outcome buckets of exposure_by_policy_outcome,
# in presentation order. The classification reads only the
# standing report's own summary (policy.py builds it): a
# blocker failure outranks an ordinary failure, a failure
# outranks a not-evaluable, and a file with no report at all is
# named not_evaluated — never folded into pass.
POLICY_OUTCOME_BUCKETS: tuple[str, ...] = (
    "pass",
    "fail",
    "blocker_fail",
    "not_fully_evaluable",
    "not_evaluated",
)


def _segment_of(credit_file) -> str:  # noqa: ANN001, ANN202
    segment = credit_file.borrower.segment
    return getattr(segment, "value", segment)


def _amount(value: Decimal) -> str:
    """Amounts ride the view as strings, as the console queue
    carries them: exact decimals, never floats."""
    return str(value)


def classify_policy_report(report) -> str:  # noqa: ANN001
    """A standing policy report's outcome bucket, from its own
    summary counts and nothing else."""
    if report is None:
        return "not_evaluated"
    summary = report.summary
    if summary.get("blocker_failures", 0) > 0:
        return "blocker_fail"
    if summary.get("fail", 0) > 0:
        return "fail"
    if summary.get("not_evaluable", 0) > 0:
        return "not_fully_evaluable"
    return "pass"


def _exposure_by_segment(files: list) -> dict:
    rows: dict[str, dict] = {}
    total_files = 0
    total_amount = Decimal("0")
    for credit_file in files:
        segment = _segment_of(credit_file)
        amount = credit_file.facility.amount
        row = rows.setdefault(
            segment, {"files": 0, "facility_amount": Decimal("0")}
        )
        row["files"] += 1
        row["facility_amount"] += amount
        total_files += 1
        total_amount += amount
    ordered = sorted(
        rows.items(), key=lambda kv: (-kv[1]["facility_amount"], kv[0])
    )
    return {
        "by_segment": [
            {
                "segment": segment,
                "files": row["files"],
                "facility_amount": _amount(row["facility_amount"]),
            }
            for segment, row in ordered
        ],
        "total": {
            "files": total_files,
            "facility_amount": _amount(total_amount),
        },
    }


def _exposure_by_policy_outcome(pipeline, files: list) -> dict:  # noqa: ANN001
    store = pipeline.store
    buckets: dict[str, dict] = {
        bucket: {"files": 0, "facility_amount": Decimal("0")}
        for bucket in POLICY_OUTCOME_BUCKETS
    }
    for credit_file in files:
        reports = store.list_policy_reports(credit_file.credit_file_id)
        # The standing report is the last one saved: list order
        # is save order in every store (the Postgres profile
        # orders by its serial id), the same convention the
        # console's queue reads for memos.
        standing = reports[-1] if reports else None
        bucket = classify_policy_report(standing)
        buckets[bucket]["files"] += 1
        buckets[bucket]["facility_amount"] += credit_file.facility.amount
    return {
        "standing_report": "last saved per file",
        "by_outcome": [
            {
                "outcome": bucket,
                "files": buckets[bucket]["files"],
                "facility_amount": _amount(buckets[bucket]["facility_amount"]),
            }
            for bucket in POLICY_OUTCOME_BUCKETS
        ],
    }


def _covenant_watch(pipeline, files: list, *, as_of: date) -> dict:  # noqa: ANN001
    visible_ids = {f.credit_file_id for f in files}
    views = [
        view
        for view in pipeline.list_breach_cases(as_of=as_of)
        if view["credit_file_id"] in visible_ids
    ]
    by_state: dict[str, int] = {}
    for view in views:
        by_state[view["state"]] = by_state.get(view["state"], 0) + 1
    open_views = [
        view
        for view in views
        if view["state"] not in BREACH_TERMINAL_STATES
    ]
    # The watch is a clock, not an archive: the case whose cure
    # deadline lands first stands first, overdue at the head.
    open_views.sort(
        key=lambda v: (v["cure_deadline"], v["test_date"], v["breach_id"])
    )
    return {
        "open": [
            {
                "breach_id": view["breach_id"],
                "credit_file_id": view["credit_file_id"],
                "borrower_name": view["borrower_name"],
                "covenant_name": view["covenant_name"],
                "metric_key": view["metric_key"],
                "state": view["state"],
                "test_date": view["test_date"],
                "cure_deadline": view["cure_deadline"],
                "days_to_cure_deadline": view["days_to_cure_deadline"],
                "cure_overdue": view["cure_overdue"],
            }
            for view in open_views
        ],
        "open_count": len(open_views),
        "overdue_count": sum(1 for v in open_views if v["cure_overdue"]),
        "by_state": [
            {"state": state, "cases": by_state[state]}
            for state in sorted(by_state)
        ],
    }


def _staleness_census(pipeline, files: list, *, as_of: date) -> dict:  # noqa: ANN001
    ready = 0
    blocked: list[dict] = []
    not_computable: list[str] = []
    by_input: dict[str, dict[str, int]] = {}
    for credit_file in files:
        credit_file_id = credit_file.credit_file_id
        try:
            report = pipeline.compute_readiness(
                credit_file_id, as_of=as_of
            )
        except CreditError:
            # A file whose readiness cannot compute yet is named
            # as such — the console queue's null, stated as a
            # census bucket, never a borrowed zero.
            not_computable.append(credit_file_id)
            continue
        if report.ready:
            ready += 1
            continue
        blockers = [
            {"input_key": e.input_key, "state": e.state}
            for e in report.blockers
        ]
        blocked.append(
            {
                "credit_file_id": credit_file_id,
                "borrower_name": credit_file.borrower.legal_name,
                "blockers": blockers,
            }
        )
        for entry in report.blockers:
            row = by_input.setdefault(
                entry.input_key, {"files": 0, "occurrences": 0}
            )
            row["occurrences"] += 1
        for input_key in {e.input_key for e in report.blockers}:
            by_input[input_key]["files"] += 1
    blocked.sort(key=lambda row: row["credit_file_id"])
    not_computable.sort()
    histogram = sorted(
        by_input.items(), key=lambda kv: (-kv[1]["files"], kv[0])
    )
    return {
        "ready_files": ready,
        "blocked_files": len(blocked),
        "not_computable_files": len(not_computable),
        "blocked": blocked,
        "not_computable": not_computable,
        "blockers_by_input": [
            {
                "input_key": input_key,
                "files": row["files"],
                "occurrences": row["occurrences"],
            }
            for input_key, row in histogram
        ],
    }


def _adverse_rollup(pipeline, files: list) -> dict:  # noqa: ANN001
    store = pipeline.store
    by_outcome: dict[str, int] = {outcome: 0 for outcome in sorted(OUTCOMES)}
    decline_codes: dict[str, int] = {}
    deferral_codes: dict[str, int] = {}
    decided_memos = 0
    for credit_file in files:
        credit_file_id = credit_file.credit_file_id
        for memo in store.list_memos(credit_file_id):
            decisions = store.list_committee_decisions(
                credit_file_id, memo.memo_id
            )
            if not decisions:
                continue
            # A memo's standing decision is its latest: a
            # deferral superseded on re-presentment counts
            # once, as what it became.
            latest = decisions[-1]
            decided_memos += 1
            by_outcome[latest.outcome] = (
                by_outcome.get(latest.outcome, 0) + 1
            )
            if latest.outcome == "decline":
                for reason in latest.adverse_reasons:
                    decline_codes[reason.code] = (
                        decline_codes.get(reason.code, 0) + 1
                    )
            if latest.outcome == "defer":
                for code in latest.deferral_reasons:
                    deferral_codes[code] = deferral_codes.get(code, 0) + 1
    return {
        "decided_memos": decided_memos,
        "by_outcome": [
            {"outcome": outcome, "decisions": by_outcome[outcome]}
            for outcome in sorted(OUTCOMES)
        ],
        "declines_by_reason_code": [
            {"code": code, "decisions": count}
            for code, count in sorted(
                decline_codes.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ],
        "deferrals_by_reason_code": [
            {"code": code, "decisions": count}
            for code, count in sorted(
                deferral_codes.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ],
        "vocabularies": {
            "adverse_reasons": ADVERSE_REASONS_VERSION,
            "deferral_reasons": DEFERRAL_REASONS_VERSION,
        },
    }


_BUILDERS = {
    "exposure_by_segment": lambda pipeline, files, as_of: (
        _exposure_by_segment(files)
    ),
    "exposure_by_policy_outcome": lambda pipeline, files, as_of: (
        _exposure_by_policy_outcome(pipeline, files)
    ),
    "covenant_watch": lambda pipeline, files, as_of: _covenant_watch(
        pipeline, files, as_of=as_of
    ),
    "staleness_census": lambda pipeline, files, as_of: _staleness_census(
        pipeline, files, as_of=as_of
    ),
    "adverse_rollup": lambda pipeline, files, as_of: _adverse_rollup(
        pipeline, files
    ),
}


def withheld_sections(caller: Caller) -> list[dict]:
    """The sections the caller's role may not read, with the
    family each belongs to and the policy's coded reason. The
    API records one denial per withheld section."""
    policy = ROLE_POLICY.get(caller.role)
    held = policy["families"] if policy is not None else frozenset()
    return [
        {
            "section": section,
            "family": family,
            "reason_code": REASON_ROLE_NOT_PERMITTED,
        }
        for section, family in SECTIONS
        if family not in held
    ]


def portfolio_view(  # noqa: ANN001
    pipeline, files: list, *, caller: Caller, as_of: date
) -> dict:
    """The portfolio view over exactly `files` — the caller's
    visible book, scope-filtered by the API before this builder
    runs. Sections the caller's role does not hold are withheld
    under the coded reason, in place, so the view's shape is the
    same for every reader and what is missing is named."""
    policy = ROLE_POLICY.get(caller.role)
    held = policy["families"] if policy is not None else frozenset()
    scope = policy["scope"] if policy is not None else ""
    sections: dict[str, dict] = {}
    for section, family in SECTIONS:
        if family not in held:
            sections[section] = {
                "withheld": True,
                "family": family,
                "reason_code": REASON_ROLE_NOT_PERMITTED,
            }
            continue
        sections[section] = _BUILDERS[section](pipeline, files, as_of)
    return {
        "as_of": as_of.isoformat(),
        "access_policy_version": ACCESS_POLICY_VERSION,
        "scope": scope,
        "caller_role": caller.role,
        "files_in_scope": len(files),
        "sections": sections,
    }


# ---------------------------------------------------------------------
# Verification: the fold, re-performed by an independent walk.
#
# The walk below reads the store's raw artefacts and derives
# every figure again — ageing computed from the breach models
# themselves rather than the pipeline's views, readiness folded
# from the reports' own entries, decisions walked memo by memo —
# and names every place the served view disagrees. What it
# shares with the builders is the artefacts of record and
# nothing else: a fold bug has to be made twice, in two shapes,
# to pass.
# ---------------------------------------------------------------------


def verify_portfolio_view(  # noqa: ANN001
    view: dict, pipeline, files: list, *, caller: Caller, as_of: date
) -> list[str]:
    """Re-perform `view` over `files`; return the mismatches
    (an empty list is a verified view). Withheld sections are
    graded for shape: a withheld section that carries figures,
    or a held section withheld, is itself a mismatch."""
    problems: list[str] = []
    store = pipeline.store
    policy = ROLE_POLICY.get(caller.role)
    held = policy["families"] if policy is not None else frozenset()

    if view.get("files_in_scope") != len(files):
        problems.append(
            f"files_in_scope reads {view.get('files_in_scope')}, "
            f"the walk ran over {len(files)}"
        )
    sections = view.get("sections", {})

    def _check(section: str, expected: dict) -> None:
        got = sections.get(section)
        if got != expected:
            problems.append(
                f"section {section} disagrees with the re-performance"
            )

    for section, family in SECTIONS:
        got = sections.get(section)
        if family not in held:
            if got != {
                "withheld": True,
                "family": family,
                "reason_code": REASON_ROLE_NOT_PERMITTED,
            }:
                problems.append(
                    f"section {section} is not withheld as the "
                    "policy requires"
                )
            continue
        if got is None or got.get("withheld"):
            problems.append(f"section {section} withheld in error")
            continue
        if section == "exposure_by_segment":
            totals: dict[str, list] = {}
            grand = Decimal("0")
            for credit_file in files:
                segment = _segment_of(credit_file)
                count, amount = totals.get(segment, [0, Decimal("0")])
                totals[segment] = [
                    count + 1,
                    amount + credit_file.facility.amount,
                ]
                grand += credit_file.facility.amount
            expected_rows = [
                {
                    "segment": segment,
                    "files": count,
                    "facility_amount": str(amount),
                }
                for segment, (count, amount) in sorted(
                    totals.items(), key=lambda kv: (-kv[1][1], kv[0])
                )
            ]
            _check(
                section,
                {
                    "by_segment": expected_rows,
                    "total": {
                        "files": len(files),
                        "facility_amount": str(grand),
                    },
                },
            )
        elif section == "exposure_by_policy_outcome":
            counts: dict[str, list] = {
                bucket: [0, Decimal("0")] for bucket in POLICY_OUTCOME_BUCKETS
            }
            for credit_file in files:
                reports = store.list_policy_reports(
                    credit_file.credit_file_id
                )
                standing = reports[-1] if reports else None
                if standing is None:
                    bucket = "not_evaluated"
                elif standing.summary.get("blocker_failures", 0) > 0:
                    bucket = "blocker_fail"
                elif standing.summary.get("fail", 0) > 0:
                    bucket = "fail"
                elif standing.summary.get("not_evaluable", 0) > 0:
                    bucket = "not_fully_evaluable"
                else:
                    bucket = "pass"
                counts[bucket][0] += 1
                counts[bucket][1] += credit_file.facility.amount
            _check(
                section,
                {
                    "standing_report": "last saved per file",
                    "by_outcome": [
                        {
                            "outcome": bucket,
                            "files": counts[bucket][0],
                            "facility_amount": str(counts[bucket][1]),
                        }
                        for bucket in POLICY_OUTCOME_BUCKETS
                    ],
                },
            )
        elif section == "covenant_watch":
            visible_ids = {f.credit_file_id for f in files}
            cases = [
                breach
                for breach in store.list_breach_cases()
                if breach.credit_file_id in visible_ids
            ]
            by_state: dict[str, int] = {}
            for breach in cases:
                by_state[breach.state] = by_state.get(breach.state, 0) + 1
            open_cases = [
                breach
                for breach in cases
                if breach.state not in BREACH_TERMINAL_STATES
            ]
            open_cases.sort(
                key=lambda b: (b.cure_deadline, b.test_date, b.breach_id)
            )
            _check(
                section,
                {
                    "open": [
                        {
                            "breach_id": b.breach_id,
                            "credit_file_id": b.credit_file_id,
                            "borrower_name": b.borrower_name,
                            "covenant_name": b.covenant_name,
                            "metric_key": b.metric_key,
                            "state": b.state,
                            "test_date": b.test_date.isoformat(),
                            "cure_deadline": b.cure_deadline.isoformat(),
                            "days_to_cure_deadline": (
                                b.cure_deadline - as_of
                            ).days,
                            "cure_overdue": bool(
                                b.state not in BREACH_TERMINAL_STATES
                                and as_of > b.cure_deadline
                            ),
                        }
                        for b in open_cases
                    ],
                    "open_count": len(open_cases),
                    "overdue_count": sum(
                        1
                        for b in open_cases
                        if as_of > b.cure_deadline
                    ),
                    "by_state": [
                        {"state": state, "cases": by_state[state]}
                        for state in sorted(by_state)
                    ],
                },
            )
        elif section == "staleness_census":
            ready = 0
            blocked_rows: list[dict] = []
            unknown: list[str] = []
            histogram: dict[str, dict[str, int]] = {}
            for credit_file in files:
                credit_file_id = credit_file.credit_file_id
                try:
                    report = pipeline.compute_readiness(
                        credit_file_id, as_of=as_of
                    )
                except CreditError:
                    unknown.append(credit_file_id)
                    continue
                blockers = [e for e in report.entries if e.blocker]
                if not blockers:
                    ready += 1
                    continue
                blocked_rows.append(
                    {
                        "credit_file_id": credit_file_id,
                        "borrower_name": credit_file.borrower.legal_name,
                        "blockers": [
                            {"input_key": e.input_key, "state": e.state}
                            for e in blockers
                        ],
                    }
                )
                seen: set[str] = set()
                for entry in blockers:
                    row = histogram.setdefault(
                        entry.input_key, {"files": 0, "occurrences": 0}
                    )
                    row["occurrences"] += 1
                    seen.add(entry.input_key)
                for input_key in seen:
                    histogram[input_key]["files"] += 1
            blocked_rows.sort(key=lambda row: row["credit_file_id"])
            unknown.sort()
            _check(
                section,
                {
                    "ready_files": ready,
                    "blocked_files": len(blocked_rows),
                    "not_computable_files": len(unknown),
                    "blocked": blocked_rows,
                    "not_computable": unknown,
                    "blockers_by_input": [
                        {
                            "input_key": input_key,
                            "files": row["files"],
                            "occurrences": row["occurrences"],
                        }
                        for input_key, row in sorted(
                            histogram.items(),
                            key=lambda kv: (-kv[1]["files"], kv[0]),
                        )
                    ],
                },
            )
        elif section == "adverse_rollup":
            outcomes: dict[str, int] = {
                outcome: 0 for outcome in sorted(OUTCOMES)
            }
            declines: dict[str, int] = {}
            deferrals: dict[str, int] = {}
            decided = 0
            for credit_file in files:
                credit_file_id = credit_file.credit_file_id
                for memo in store.list_memos(credit_file_id):
                    decisions = store.list_committee_decisions(
                        credit_file_id, memo.memo_id
                    )
                    if not decisions:
                        continue
                    latest = decisions[-1]
                    decided += 1
                    outcomes[latest.outcome] = (
                        outcomes.get(latest.outcome, 0) + 1
                    )
                    if latest.outcome == "decline":
                        for reason in latest.adverse_reasons:
                            declines[reason.code] = (
                                declines.get(reason.code, 0) + 1
                            )
                    if latest.outcome == "defer":
                        for code in latest.deferral_reasons:
                            deferrals[code] = deferrals.get(code, 0) + 1
            _check(
                section,
                {
                    "decided_memos": decided,
                    "by_outcome": [
                        {
                            "outcome": outcome,
                            "decisions": outcomes[outcome],
                        }
                        for outcome in sorted(OUTCOMES)
                    ],
                    "declines_by_reason_code": [
                        {"code": code, "decisions": count}
                        for code, count in sorted(
                            declines.items(),
                            key=lambda kv: (-kv[1], kv[0]),
                        )
                    ],
                    "deferrals_by_reason_code": [
                        {"code": code, "decisions": count}
                        for code, count in sorted(
                            deferrals.items(),
                            key=lambda kv: (-kv[1], kv[0]),
                        )
                    ],
                    "vocabularies": {
                        "adverse_reasons": ADVERSE_REASONS_VERSION,
                        "deferral_reasons": DEFERRAL_REASONS_VERSION,
                    },
                },
            )
    return problems
