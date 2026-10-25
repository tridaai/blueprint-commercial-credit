"""Committee readiness: design law 7, as an engine.

A credit file is committee-ready only when every required input is
on file AND fresh. Three requirement sources exist by round 9 — the
policy-cited checklist (round 1), the policy pack's statement-age
and appraisal limits (round 4), and the computation pack's
not-computable figures (round 3) — and this module consolidates
them into ONE content-addressed ReadinessReport, not three lists an
officer has to reconcile by eye.

Freshness rules are data, never constants inside the checker:

- financial statements: the fiscal-period age limit comes from the
  pinned lending policy pack's `financial_statement_age` test;
- appraisals: the age limit comes from the pack's `appraisal_age`
  test;
- insurance certificates, tax returns and interim statements: the
  receipt-age limits live in the versioned readiness policy below.

A readiness entry is a BLOCKER when its input is missing, present
but stale past the limit its rule names, or not evaluable for want
of a source input (a figure the computation could not produce, or a
policy test that could not be evaluated because an input is absent).
Figure entries are recorded for every not-computable figure, but
they block only for the pack's LATEST fiscal period — the period
the policy tests and the memo read — and for period-independent
figures. An earlier period's gap (the first year's debt service,
which needs a pre-window year the checklist does not require) is
recorded with its missing input named, and does not block: no
standard three-year file could ever convene if it did.
A policy test that FAILS is not a blocker: it is credit judgment,
carried into the memo as an exception for the committee to weigh.
A test that does not apply to this file at all — a global test on a
facility that names no guarantors, an appraisal test on a facility
type with no appraisal requirement — is not entered either.

Blockers clear only when the input itself refreshes: a new document
version received, a figure that becomes computable. There is no
waiver path — no override flag, no analyst note, no "excepted"
state anywhere in this module. The only policy-level exception
route is the committee's own conditional approval or deferral
(round 7), which is a credit decision about a ready file, never a
readiness override.

Everything here is pure: the evaluation date is passed in, nothing
reads the wall clock, and the same artefacts at the same date
produce the same report id.
"""

from __future__ import annotations

from datetime import date

from .memo import _canonical_hash
from .models import (
    ChecklistItem,
    ComputationPack,
    CreditFile,
    DocumentKind,
    PolicyReport,
    PolicyTest,
    ReadinessEntry,
    ReadinessReport,
)
from .policy import get_policy_pack

READINESS_POLICY_ID = "aldergrove-readiness"
READINESS_POLICY_VERSION = "v1"

# The readiness policy's own receipt-age rules, in complete calendar
# months from the date the bank received the document to the
# evaluation date. Statement age and appraisal age are deliberately
# NOT here: the lending policy pack sets them, and readiness reads
# them from the pinned pack so the two can never drift apart.
DOCUMENT_AGE_LIMITS_MONTHS: dict[str, int] = {
    "insurance_certificate": 12,
    "tax_return": 18,
    "interim_financial_statements": 6,
}

STATE_FRESH = "present_fresh"
STATE_STALE = "present_stale"
STATE_MISSING = "missing"
STATE_NOT_EVALUABLE = "not_evaluable_input"

# Input classes (freshness-rule keys).
_CLASS_STATEMENTS = "financial_statements"
_CLASS_INTERIM = "interim_financial_statements"
_CLASS_APPRAISAL = "appraisal"
_CLASS_DOCUMENT = "document"


def input_class(item: ChecklistItem) -> str:
    """The freshness-rule class of a checklist item.

    Financial statements split in two: the three-year fiscal
    statements age by fiscal period against the policy pack's
    statement-age limit, while interim statements age by receipt
    against the readiness policy. Every other kind ages by receipt
    when the readiness policy names a rule for it, and is
    presence-only otherwise.
    """
    if item.kind == DocumentKind.FINANCIAL_STATEMENT:
        if "interim" in item.requirement_source:
            return _CLASS_INTERIM
        return _CLASS_STATEMENTS
    if item.kind == DocumentKind.APPRAISAL:
        return _CLASS_APPRAISAL
    if item.kind.value in DOCUMENT_AGE_LIMITS_MONTHS:
        return item.kind.value
    return _CLASS_DOCUMENT


def _months_between(start: date, end: date) -> int:
    """Complete calendar months from `start` to `end` (never < 0)."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(months, 0)


def _test_by_id(pack_tests: list[PolicyTest], test_id: str) -> PolicyTest | None:
    for test in pack_tests:
        if test.test_id == test_id:
            return test
    return None


def _limit_months(test: PolicyTest | None) -> int | None:
    if test is None or test.limit is None:
        return None
    return int(test.limit)


def _latest_document(credit_file: CreditFile, kind: DocumentKind):
    docs = [d for d in credit_file.documents if d.kind == kind]
    if not docs:
        return None
    return max(docs, key=lambda d: d.received_at)


def _age_verdict(
    entry: ReadinessEntry, *, age: int, limit: int | None, rule_source: str
) -> ReadinessEntry:
    """Apply an age rule to an entry whose document is on file."""
    entry.age_months = age
    entry.limit_months = limit
    entry.rule_source = rule_source
    if limit is not None and age > limit:
        entry.state = STATE_STALE
        entry.blocker = True
        entry.detail += (
            f" It is {age} months old against a limit of {limit} "
            f"months ({rule_source}) — stale."
        )
    else:
        entry.state = STATE_FRESH
        entry.blocker = False
        entry.detail += (
            f" {age} months old, within the {limit}-month limit "
            f"({rule_source})." if limit is not None else
            f" {age} months old; no age limit applies."
        )
    return entry


def _checklist_entries(
    credit_file: CreditFile,
    *,
    as_of: date,
    policy_version: str,
    pack: ComputationPack | None,
    statement_limit: int | None,
    appraisal_limit: int | None,
) -> list[ReadinessEntry]:
    entries: list[ReadinessEntry] = []
    kind_counts: dict[DocumentKind, int] = {}
    for item in credit_file.checklist:
        if item.required:
            kind_counts[item.kind] = kind_counts.get(item.kind, 0) + 1
    for item in credit_file.checklist:
        if not item.required:
            continue
        cls = input_class(item)
        entry = ReadinessEntry(
            input_key=item.item_id,
            label=item.label,
            source="checklist",
            state=STATE_MISSING,
            blocker=True,
            detail=(
                f"Required item is not on file. "
                f"Requirement: {item.requirement_source}."
            ),
        )
        if not item.satisfied:
            entries.append(entry)
            continue
        entry.document_id = item.satisfied_by_document_id
        if cls == _CLASS_STATEMENTS:
            years = (
                [f.fiscal_year for f in pack.figures if f.fiscal_year]
                if pack is not None else []
            )
            if not years:
                entry.state = STATE_NOT_EVALUABLE
                entry.missing_inputs = [
                    "computation pack: no fiscal period has been "
                    "computed from the statements on file"
                ]
                entry.detail = (
                    "Statements are on file, but no computation pack "
                    "covers a fiscal period, so the statement age "
                    "cannot be evaluated."
                )
                entries.append(entry)
                continue
            latest = max(years)
            age = _months_between(date(latest, 12, 31), as_of)
            entry.detail = (
                f"On file; latest fiscal period FY{latest} ended "
                f"{latest}-12-31."
            )
            entries.append(_age_verdict(
                entry, age=age, limit=statement_limit,
                rule_source=(
                    f"policy pack {policy_version}, "
                    f"financial_statement_age test"
                ),
            ))
            continue
        # Which document ages the item? When the checklist holds
        # ONE item of this kind, the document is versioned — a
        # refresh arrives as a newer document of the kind, and the
        # item ages by the latest one. When it holds SEVERAL (the
        # business and personal tax returns), each item ages by
        # the document that satisfied it: an old business return
        # is not made fresh by a new personal one.
        satisfying = next(
            (
                d for d in credit_file.documents
                if d.document_id == item.satisfied_by_document_id
            ),
            None,
        )
        if kind_counts.get(item.kind, 0) > 1:
            doc = satisfying or _latest_document(credit_file, item.kind)
        else:
            doc = _latest_document(credit_file, item.kind) or satisfying
        if doc is None:
            # Satisfied without a document of the item's kind cannot
            # happen through the intake path; treat as missing
            # rather than blessing it.
            entries.append(entry)
            continue
        entry.document_id = doc.document_id
        age = _months_between(doc.received_at.date(), as_of)
        if cls == _CLASS_APPRAISAL:
            entry.detail = f"On file ({doc.document_id}), received {doc.received_at.date()}."
            entries.append(_age_verdict(
                entry, age=age, limit=appraisal_limit,
                rule_source=(
                    f"policy pack {policy_version}, appraisal_age test"
                ),
            ))
        elif cls in DOCUMENT_AGE_LIMITS_MONTHS:
            entry.detail = f"On file ({doc.document_id}), received {doc.received_at.date()}."
            entries.append(_age_verdict(
                entry, age=age,
                limit=DOCUMENT_AGE_LIMITS_MONTHS[cls],
                rule_source=(
                    f"readiness policy {READINESS_POLICY_ID} "
                    f"{READINESS_POLICY_VERSION}"
                ),
            ))
        else:
            entry.state = STATE_FRESH
            entry.blocker = False
            entry.detail = (
                f"On file ({doc.document_id}); no freshness rule "
                f"applies to this input class."
            )
            entries.append(entry)
    return entries


def _figure_entries(
    pack: ComputationPack | None, *, scope_word: str
) -> list[ReadinessEntry]:
    if pack is None:
        return []
    prefix = "figure:" if pack.scope != "global" else "figure:global:"
    years = [f.fiscal_year for f in pack.figures if f.fiscal_year]
    latest_year = max(years) if years else None
    entries: list[ReadinessEntry] = []
    for figure in pack.figures:
        if figure.status != "not_computable":
            continue
        label = figure.label
        if figure.fiscal_year:
            label = f"{figure.label} (FY{figure.fiscal_year})"
        # The underwriting view is the latest period: a gap there
        # blocks. A prior period's gap is recorded, not blocking
        # (see the module docstring).
        blocks = (
            figure.fiscal_year is None
            or latest_year is None
            or figure.fiscal_year == latest_year
        )
        detail = (
            f"The computation pack could not produce this "
            f"{scope_word} figure; it is waiting on: "
            + "; ".join(figure.missing_inputs) + "."
        )
        if not blocks:
            detail += (
                f" Prior-period figure (FY{figure.fiscal_year}; "
                f"the pack's underwriting period is FY{latest_year}) "
                f"— recorded, not blocking."
            )
        key = f"{prefix}{figure.key}"
        if figure.fiscal_year:
            key = f"{key}@FY{figure.fiscal_year}"
        entries.append(ReadinessEntry(
            input_key=key,
            label=label,
            source="computation_pack",
            state=STATE_NOT_EVALUABLE,
            blocker=blocks,
            detail=detail,
            missing_inputs=list(figure.missing_inputs),
        ))
    return entries


def _test_applies(
    test: PolicyTest, credit_file: CreditFile
) -> bool:
    """Structural applicability, mirroring the policy engine."""
    if test.requires_guarantors and not credit_file.facility.guarantors:
        return False
    if (
        test.applies_to_facility_types
        and credit_file.facility.facility_type.value
        not in test.applies_to_facility_types
    ):
        return False
    return True


def build_readiness_report(
    credit_file: CreditFile,
    *,
    as_of: date,
    policy_version: str,
    pack: ComputationPack | None = None,
    global_pack: ComputationPack | None = None,
    policy_report: PolicyReport | None = None,
) -> ReadinessReport:
    """Consolidate every readiness requirement into one report.

    Pure: same file, same pinned artefacts, same date → same
    report, id included. `pack` / `global_pack` / `policy_report`
    are the artefacts the caller has pinned (a memo's, for the
    convene gate; the file's latest, for the standing view).
    """
    pack_tests = get_policy_pack(policy_version).tests
    statement_limit = _limit_months(
        _test_by_id(pack_tests, "financial_statement_age")
    )
    appraisal_limit = _limit_months(
        _test_by_id(pack_tests, "appraisal_age")
    )

    entries = _checklist_entries(
        credit_file, as_of=as_of, policy_version=policy_version,
        pack=pack, statement_limit=statement_limit,
        appraisal_limit=appraisal_limit,
    )
    entries += _figure_entries(pack, scope_word="borrower")
    entries += _figure_entries(global_pack, scope_word="global")

    if policy_report is not None:
        by_key = {e.input_key: e for e in entries}
        items_by_id = {i.item_id: i for i in credit_file.checklist}
        for outcome in policy_report.outcomes:
            test = _test_by_id(pack_tests, outcome.test_id)
            if test is None:
                continue
            if test.fact == "required_documents":
                if outcome.outcome == "fail":
                    for entry in entries:
                        if (
                            entry.source == "checklist"
                            and entry.state == STATE_MISSING
                        ):
                            entry.related_test_ids.append(outcome.test_id)
                continue
            if test.fact == "statement_age":
                for entry in entries:
                    item = items_by_id.get(entry.input_key)
                    if (
                        entry.source == "checklist"
                        and item is not None
                        and input_class(item) == _CLASS_STATEMENTS
                    ):
                        entry.related_test_ids.append(outcome.test_id)
                continue
            if test.fact == "appraisal_age":
                for entry in entries:
                    item = items_by_id.get(entry.input_key)
                    if (
                        entry.source == "checklist"
                        and item is not None
                        and item.kind == DocumentKind.APPRAISAL
                    ):
                        entry.related_test_ids.append(outcome.test_id)
                continue
            if test.kind != "figure" or outcome.outcome != "not_evaluable":
                # A failed figure test is credit judgment for the
                # committee (a memo exception), never a blocker.
                continue
            wanted = (
                f"figure:global:{test.figure_key}"
                if test.scope == "global"
                else f"figure:{test.figure_key}"
            )
            matched = [
                e for e in entries
                if e.source == "computation_pack"
                and e.input_key.split("@")[0] == wanted
            ]
            if matched:
                for entry in matched:
                    entry.related_test_ids.append(outcome.test_id)
                continue
            if not _test_applies(test, credit_file):
                # The test does not apply to this file at all — a
                # global test with no guarantors, an appraisal test
                # on the wrong facility type. Not a blocker: there is
                # no input this file could produce to satisfy it.
                continue
            key = f"policy:{outcome.test_id}"
            if key in by_key:
                continue
            entry = ReadinessEntry(
                input_key=key,
                label=test.name,
                source="policy_report",
                state=STATE_NOT_EVALUABLE,
                blocker=True,
                detail=(
                    f"Policy test {outcome.test_id} could not be "
                    f"evaluated: " + (outcome.detail or "")
                ).strip(),
                missing_inputs=list(outcome.missing_inputs),
                related_test_ids=[outcome.test_id],
            )
            entries.append(entry)
            by_key[key] = entry

    descriptor = {
        "kind": "readiness-report",
        "credit_file_id": credit_file.credit_file_id,
        "as_of": as_of.isoformat(),
        "policy_version": policy_version,
        "readiness_policy_version": READINESS_POLICY_VERSION,
        "policy_report_id": (
            policy_report.report_id if policy_report else None
        ),
        "computation_pack_id": pack.pack_id if pack else None,
        "global_pack_id": global_pack.pack_id if global_pack else None,
        "entries": [e.model_dump(mode="json") for e in entries],
    }
    summary = {
        "present_fresh": 0, "present_stale": 0,
        "missing": 0, "not_evaluable_input": 0,
    }
    for entry in entries:
        summary[entry.state] = summary.get(entry.state, 0) + 1
    summary["blockers"] = sum(1 for e in entries if e.blocker)
    policy_pack = get_policy_pack(policy_version)
    return ReadinessReport(
        report_id=f"readiness-{_canonical_hash(descriptor)[:16]}",
        credit_file_id=credit_file.credit_file_id,
        as_of=as_of,
        policy_pack_id=policy_pack.pack_id,
        policy_version=policy_version,
        readiness_policy_id=READINESS_POLICY_ID,
        readiness_policy_version=READINESS_POLICY_VERSION,
        policy_report_id=(
            policy_report.report_id if policy_report else None
        ),
        computation_pack_id=pack.pack_id if pack else None,
        global_pack_id=global_pack.pack_id if global_pack else None,
        entries=entries,
        summary=summary,
    )


def readiness_refusal_message(
    report: ReadinessReport, *, memo_id: str
) -> str:
    """The convene refusal: every blocker, named, with its state.

    This text is the refusal — the API surfaces it verbatim in the
    422 body — so it must carry the whole block, not a summary that
    sends the officer hunting for the rest.
    """
    blockers = report.blockers
    named = "; ".join(
        f"[{e.input_key}] {e.label} — {e.state}: {e.detail}"
        for e in blockers
    )
    return (
        f"memo {memo_id} cannot go before the committee: the credit "
        f"file is not committee-ready at {report.as_of} — "
        f"{len(blockers)} readiness blocker(s) on readiness report "
        f"{report.report_id}: {named}. A blocker clears only when "
        f"the input itself is received or refreshed; readiness "
        f"blockers cannot be waived."
    )


def render_outstanding_items(
    report: ReadinessReport, credit_file: CreditFile
) -> str:
    """The borrower-facing 'what we still need' list.

    Generated from the report's blocker entries — labels, states
    and missing inputs exactly as the engine recorded them — never
    hand-typed, so the list an RM sends can never drift from the
    block the committee gate enforces.
    """
    lines = [
        f"Outstanding items for {credit_file.borrower.legal_name} "
        f"({credit_file.facility.facility_type.value} facility) — "
        f"as of {report.as_of}, from readiness report "
        f"{report.report_id}:",
    ]
    blockers = report.blockers
    if not blockers:
        lines.append(
            "No outstanding items: every required input is on file "
            "and within its freshness limit."
        )
        return "\n".join(lines)
    for entry in blockers:
        if entry.state == STATE_MISSING:
            lines.append(f"- {entry.label}: not on file.")
        elif entry.state == STATE_STALE:
            lines.append(
                f"- {entry.label}: on file but stale — "
                f"{entry.age_months} months old against a limit of "
                f"{entry.limit_months} months; a current version is "
                f"required."
            )
        else:
            lines.append(
                f"- {entry.label}: cannot be evaluated yet — "
                f"waiting on: {'; '.join(entry.missing_inputs)}."
            )
    return "\n".join(lines)
