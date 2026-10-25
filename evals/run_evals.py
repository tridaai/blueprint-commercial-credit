#!/usr/bin/env python3
"""Eval gate for the Commercial Credit Memo & Covenant Monitoring Agent.

Round 1 counters — the ones this round's machinery can be graded on:

  provenance_completeness   every extracted value carries complete
                            provenance (document, page, line/cell).
                            REQUIRED: 100%. A single untraceable
                            figure fails the gate (design law 1).
  extraction_correctness    extracted values equal the ground truth
                            the documents were generated from.
  checklist_coverage        every facility type generates its full
                            rule-cited checklist, incl. its
                            facility-specific items.
  provenance_rejection      a batch containing a value with
                            incomplete provenance is rejected whole.

Round 2 counters — the spreading engine:

  spread_values_vs_truth    every spread value for the seed borrower,
                            all periods, equals ground truth exactly
                            (Decimal equality — near misses fail).
  spread_determinism        rebuilding the spread from the same
                            inputs reproduces it byte-identically.
  adjustment_rejection      adjustments without provenance, without
                            a reason, or naming a non-template line
                            are all rejected; a valid add-back moves
                            only the adjusted view, by exactly its
                            amount.
  unmapped_line_counter     statement lines the taxonomy does not
                            map are carried as named unmapped entries.
                            REQUIRED: 0 for the seed borrower (its
                            taxonomy fixture is fully mapped); a
                            control document with extra lines must
                            report them, proving the counter works.
  restatement_supersession  a corrected statement version supersedes
                            the earlier extraction for its period;
                            the spread rests on the new version and
                            the prior version stays retrievable.

Round 3 counters — the computation engine:

  computation_correctness   engine figures for a hand-built fixture
                            borrower equal values computed by hand in
                            this file (exact, to the cent / 4dp);
                            spot checks on the seed borrower's pack
                            likewise.
  derivation_trace_         every computed figure's derivation
    completeness            chain resolves to provenanced spread
                            lines / supplemental figures, and every
                            step re-performs. REQUIRED: 100%.
  computation_determinism   recomputing the same inputs returns the
                            identical artefact — same pack id, same
                            bytes.
  elimination_rejection     a global-view elimination without a
                            stated reason is refused, in code.
  global_dscr_vs_hand       borrower + guarantor with eliminations,
                            against a hand-computed global DSCR.

Round 4 counters — the policy pack:

  policy_outcomes_vs_       per-test outcomes on three fixtures
    expectations            equal the hand-decided maps: the seed
                            borrower (DSCR not_evaluable, naming the
                            missing cash-flow figures), a clean-pass
                            file (every applicable test passes), and
                            a leverage-fail file (blocker failure at
                            5.0000 against a 2.00 ceiling).
  policy_version_           a file evaluated under v1 keeps its v1
    discipline              report — same id, same bytes, same
                            outcomes — after a v2 evaluation ships;
                            the file's policy history records both
                            pinned versions. The coverage fixture
                            (DSCR exactly 1.3000) passes v1's 1.25
                            floor and fails v2's tightened 1.35.
  policy_silent_pass_       every "pass" outcome on a figure test
    counter                 is re-checked against the pack it read:
                            the figure must exist, be computed, and
                            satisfy the threshold. REQUIRED: 0 —
                            a not_evaluable recorded as a pass
                            anywhere fails the gate.
  policy_determinism        re-evaluating the same file under the
                            same pinned version over unchanged
                            inputs reproduces the identical report.

Round 5 counters — memo drafting + the figure verifier:

  memo_figure_trace_        every figure in every composed memo's
    completeness            rendered text carries a trace into the
                            pinned artefacts, re-extracted here from
                            the rendered text itself. REQUIRED:
                            100% — one untraced figure fails.
  memo_untraceable_         tamper cases — a figure altered by one
    acceptance              cent, a transposed ratio, an invented
                            figure — must each be REFUSED by the
                            verifier. REQUIRED: 0 accepted.
  memo_exceptions_omission  the exceptions section of every graded
                            memo accounts for exactly its report's
                            non-pass outcomes, by test id; a memo
                            tampered to drop one must be refused.
                            REQUIRED: 0 omissions.
  memo_determinism          composing over identical inputs
                            reproduces the identical memo id.
  memo_composer_figure_     property check across the fixtures:
    discipline              every number the offline composer emits
                            exists in the inputs it composed from.

Round 6 counters — the credit officer gate:

  officer_anonymous_        gate actions with an empty officer id
    action_accepted         or role (submit, approve, return) must
                            each be REFUSED. REQUIRED: 0 accepted.
  officer_illegal_          deciding a memo that is not in officer
    transition_accepted     review, submitting one twice, and
                            approving a returned memo must each be
                            REFUSED. REQUIRED: 0 accepted.
  officer_untraceable_      an approval whose edit introduces a
    edit_stored             figure the verifier cannot trace must
                            store NOTHING — no final memo, no diff.
                            REQUIRED: 0 stored.
  officer_exception_        an approval whose edit targets the
    drop_stored             exceptions section (to drop or soften
                            an outcome) must store NOTHING.
                            REQUIRED: 0 stored.
  officer_diff_missing_     every approval — with edits and
    for_edited_approval     as-is — records its MemoDiff.
                            REQUIRED: 0 missing.

Round 7 counters — the committee gate:

  committee_single_         a committee of one — quorum stated
    member_accepted         as 1, one member under a quorum of
                            2, or the approving officer alone —
                            must each be REFUSED: a quorum is at
                            least two, mechanically (law 4).
                            REQUIRED: 0 accepted.
  committee_decline_        a decline with no adverse-action
    without_reasons_        reasons, or with only a free-text
    accepted                note, must be REFUSED: the decline
                            path carries coded reasons or it does
                            not exist (law 3). REQUIRED: 0.
  committee_decline_        a decline reason citing a policy
    passed_test_accepted    test the memo's pinned report
                            records as a PASS must be REFUSED,
                            the offender named. REQUIRED: 0.
  committee_untraceable_    a decline tied to a figure the memo's
    accepted                verification record never traced or
                            to a document that is on file, and a
                            conditional approval whose covenant
                            threshold does not trace, must each be
                            REFUSED. REQUIRED: 0 accepted.
  committee_decision_on_    convening or deciding the superseded
    superseded_accepted     draft instead of the officer-final
                            memo must be REFUSED. REQUIRED: 0.
  committee_positive_paths  quorum approval; conditional
                            approval with traced conditions;
                            decline with tied reasons and a
                            rendered adverse-action notice; and
                            defer → re-present → approve on the
                            same artefact, two decisions on record.

Round 8 counters — document disagreements (law 5), graded on the
seed borrower with its FY2024 tax return and management accounts
registered beside the audited statements (exactly seven
disagreements: five material, two immaterial):

  disagreement_detection_   the records the spread carries, by
    exactness               line and materiality, against the
                            hand-graded fixture.
  disagreement_material_    a memo with the disagreements section
    omitted_accepted        dropped, with one material
                            disagreement's statement removed, or
                            with a material disagreement reworded
                            into smaller language, must each be
                            REFUSED by the verifier. REQUIRED: 0.
  disagreement_averaged_    the spread's working figure on a
    or_dropped              contested line must be the audited
                            figure itself — never the mean of the
                            sources — and every source's figure
                            must ride in the record. REQUIRED: 0.
  disagreement_cross_type_  the tax return, registered after the
    restatement_accepted    audited statements, must not
                            supersede them: the audited period
                            source stands, the tax source stands
                            beside it. REQUIRED: 0.
  disagreement_flagged_     figures computed over a materially
    figure_clean            disagreed line must carry the flag,
                            figures over immaterial-only lines
                            must not, and a flagged figure's memo
                            trace must name the disagreement.
                            REQUIRED: 0 clean presentations.

Round 9 counters — committee readiness (law 7), graded on the
seed borrower (blocked), a purpose-built committee-ready file,
and a CRE file whose appraisal goes stale:

  readiness_convene_over_   convening an officer-final memo while
    blocker_accepted        any readiness blocker stands — missing
                            documents, a non-computable DSCR, a
                            stale appraisal — must be REFUSED.
                            REQUIRED: 0 accepted.
  readiness_stale_blessed_  a readiness report minted while the
    by_old_report           file was fresh must not bless a
                            convene at a later date when the
                            inputs have gone stale: readiness is
                            recomputed at convene. REQUIRED: 0.
  readiness_blocker_       a blocker must persist across
    cleared_without_refresh recomputation, and no waiver parameter,
                            method or field may exist to clear it.
                            REQUIRED: 0 clearances.
  readiness_blocker_        every blocker on the refusal's report
    unnamed_in_refusal      must be named — key and label — in the
                            refusal text itself. REQUIRED: 0.
  readiness_positive_paths  a fresh file convenes; a stale
                            appraisal blocks, a refreshed appraisal
                            clears, the convene lands; missing tax
                            returns block by name; the officer
                            approval record carries the readiness
                            report id and blocker count.

Round 10 counters — covenants (law 6), graded on the purpose-built
committee-ready file taken through a conditional approval:

  covenant_schedule_date_   every date in the graded table — pure
    wrong                   schedule arithmetic (quarterly/annual
                            cadences, leap years, fiscal-year
                            offsets, the maturity cap, reporting
                            lags) and the end-to-end set's DSCR
                            test dates. REQUIRED: 0.
  covenant_threshold_       the stored covenant set is
    mutated_in_place        byte-identical after an amendment: a
                            threshold changes only by a superseding
                            version recorded beside the set.
                            REQUIRED: 0 mutations.
  covenant_due_missing_     a covenant test standing at
    from_queue              awaiting_inputs, its required inputs
                            named, must appear in the due-work
                            queue. REQUIRED: 0 missing.
  covenant_tick_duplication re-running the scheduler tick at the
                            same as_of records no second crossing.
                            REQUIRED: 0.
  covenant_positive_paths   the set is created from the
                            conditional approval (pinning decision
                            + policy version), the schedule
                            computes, the tick marks the due tests,
                            the queue lists the awaiting test with
                            inputs named, and the amendment lands
                            as version 2 in the derived schedule.

Round 12 counters — annual reviews, graded on approved files
taken through their first review year:

  review_schedule_date_     the derived review anniversaries —
    wrong                   the pure leap-day table and the
                            approved set's five due dates.
                            REQUIRED: 0.
  review_stale_pack_        a pack assembled over stale inputs
    marked_complete         must be produced incomplete, its
                            blockers named; the fresh pack must
                            read complete. REQUIRED: 0.
  review_pack_figure_       an invented figure and a dropped
    untraceable             locked fact in the review summary
                            are both refused. REQUIRED: 0
                            accepted.
  review_summary_breach_    a breach denied in summary prose is
    softened                refused, and the locked record states
                            the failure. REQUIRED: 0.
  review_closed_without_    the review may not close anonymously,
    officer_or_stance       unroled, uncoded, or by the decided
                            memo's approver acting alone.
                            REQUIRED: 0 accepted.
  review_positive_paths     the pack assembles complete over the
                            breach year, affirm lands
                            countersigned and the instance
                            completes, watchlist lands, escalation
                            records its committee referral, and
                            the v1-to-v2 differences are named.

Round 13 counters — the risk-rating scorecard, a coded stand-in
for the bank's own rating model, graded on the approved file and
the Hartline file whose cash-flow page is missing:

  rating_factor_score_      every factor's band, score and
    wrong                   weighted contribution on the graded
                            fixture, plus the composite and the
                            grade — and the breach year's
                            re-grade. REQUIRED: 0 wrong.
  rating_missing_input_     a figure that cannot compute must
    zero_filled             land in its named missing-data band
                            with the missing input named — never
                            zero-filled, never skipped.
                            REQUIRED: 0.
  rating_override_          an override beyond one grade from
    unlawful_accepted       the assigned grade, uncoded,
                            uncountersigned or self-countersigned
                            must be refused. REQUIRED: 0 accepted.
  rating_used_as_approval   no code path from a rating to a
                            decision: the gates neither import
                            nor consult the scorecard, and a rated
                            file with no gate action stands
                            undecided. REQUIRED: 0.
  rating_history_rewritten  registering a second scorecard
                            version must not alter an existing
                            assignment by a byte, and the history
                            labels the comparison cross-version.
                            REQUIRED: 0.
  rating_positive_paths     assignments land (the approved file
                            and the file with missing figures),
                            a one-grade override lands with both
                            grades visible, and the memo and the
                            annual review pack carry the rating.

Later rounds add counters as their machinery lands.
Exit code 0 = GATE: PASS.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commercial_credit.checklist import generate_checklist  # noqa: E402
from commercial_credit.computations import (  # noqa: E402
    compute_entity_pack,
    compute_global_pack,
    verify_trace,
)
from commercial_credit.covenants import (  # noqa: E402
    add_months,
    covenant_test_dates,
    deliverable_due_dates,
    fiscal_year_end,
)
from commercial_credit.documents import (  # noqa: E402
    LocalDocumentStore,
    read_document_verified,
    register_document,
)
from commercial_credit.errors import (  # noqa: E402
    CreditError,
    MemoVerificationError,
    ProvenanceError,
    ValidationError,
)
from commercial_credit.memo import (  # noqa: E402
    MemoInputs,
    build_trace_universe,
    compose_memo,
    extract_figures,
    render_disagreement_paragraph,
    render_memo,
    verify_memo,
)
from commercial_credit.extraction import receive_extraction  # noqa: E402
from commercial_credit.models import (  # noqa: E402
    AdjustmentReasonCode,
    AdverseActionReason,
    Borrower,
    BorrowerSegment,
    CommitteeCondition,
    CommitteeMember,
    CreditFile,
    DocumentKind,
    Elimination,
    ExtractedValue,
    FacilityRequest,
    FacilityType,
    Guarantor,
    PeriodSource,
    Provenance,
    Spread,
    SpreadAdjustment,
    SpreadLine,
)
from commercial_credit.pipeline import CreditPipeline  # noqa: E402
from commercial_credit.policy import evaluate_policy, get_policy_pack  # noqa: E402
from commercial_credit.seed import (  # noqa: E402
    ReferenceLosFeed,
    ground_truth,
    hartline_disagreement_documents,
    statement_document,
)
from commercial_credit.spreading import TEMPLATE_LINES  # noqa: E402
from commercial_credit.store import InMemoryCreditStore  # noqa: E402

EXPECTED_SPECIFICS: dict[FacilityType, set[DocumentKind]] = {
    FacilityType.TERM_LOAN: {DocumentKind.PROJECTIONS},
    FacilityType.REVOLVING_LINE: {
        DocumentKind.AR_AGING,
        DocumentKind.BORROWING_BASE_CERTIFICATE,
    },
    FacilityType.CRE_LOAN: {DocumentKind.APPRAISAL, DocumentKind.RENT_ROLL},
    FacilityType.CONSTRUCTION_LOAN: {DocumentKind.CONSTRUCTION_BUDGET, DocumentKind.PERMITS},
    FacilityType.EQUIPMENT_FINANCE: {DocumentKind.EQUIPMENT_INVOICE},
    FacilityType.SBA_7A: {DocumentKind.SBA_FORM},
}


def _modified_statement(year: int, *, extra_bs_lines=(), overrides=None) -> bytes:
    """A seed statement with lines added or values overridden — the
    shape a richer or corrected borrower filing takes."""
    payload = json.loads(statement_document(year))
    bs_page = payload["pages"][0]
    next_line = max(line["line"] for line in bs_page["lines"]) + 1
    for label, value in extra_bs_lines:
        bs_page["lines"].append({"line": next_line, "label": label, "value": value})
        next_line += 1
    for label, value in (overrides or {}).items():
        for page in payload["pages"]:
            for line in page["lines"]:
                if line["label"] == label:
                    line["value"] = value
    return json.dumps(payload, indent=2).encode("utf-8")


def _register_and_extract(pipeline, credit_file_id: str, data: bytes,
                          filename: str, period_label: str):
    credit_file = pipeline.store.get_credit_file(credit_file_id)
    doc = register_document(
        pipeline.store,
        pipeline.documents,
        credit_file,
        kind=DocumentKind.FINANCIAL_STATEMENT,
        filename=filename,
        data=data,
        period_label=period_label,
    )
    content = read_document_verified(pipeline.documents, doc)
    extracted = receive_extraction(pipeline.extractor.extract(doc, content))
    pipeline.store.save_extractions(credit_file_id, extracted)
    return doc


_SECTION_PAGE = {
    "balance_sheet": 1,
    "income_statement": 2,
    "cash_flow_statement": 3,
}


def _fixture_spread(credit_file_id: str, doc_prefix: str,
                    figures: dict[int, dict[str, str]], *, adjustments=()):
    """A spread + extractions built by hand from raw figures, for
    grading the computation engine against hand computation (the
    pytest suite carries the full fixture set)."""
    periods = sorted(figures)
    lines: list[SpreadLine] = []
    extractions: list[ExtractedValue] = []
    counters: dict[str, int] = {}
    for template in TEMPLATE_LINES:
        counters[template.section] = counters.get(template.section, 0) + 1
        raw: dict[int, Decimal | None] = {}
        adjusted: dict[int, Decimal | None] = {}
        for year in periods:
            value = figures[year].get(template.field)
            raw[year] = Decimal(value) if value is not None else None
            delta = sum(
                (a.amount for a in adjustments
                 if a.line_key == template.key and a.fiscal_year == year),
                Decimal("0"),
            )
            adjusted[year] = (
                (raw[year] if raw[year] is not None else Decimal("0")) + delta
                if raw[year] is not None or delta != 0
                else None
            )
            if value is not None:
                extractions.append(ExtractedValue(
                    field=template.field,
                    value=Decimal(value),
                    period_label=f"FY{year}",
                    fiscal_year=year,
                    provenance=Provenance(
                        document_id=f"{doc_prefix}-{year}",
                        page=_SECTION_PAGE[template.section],
                        line=f"L{counters[template.section]}",
                    ),
                ))
        lines.append(SpreadLine(
            key=template.key, label=template.label, section=template.section,
            values=raw, adjusted_values=adjusted,
            changes={year: None for year in periods},
        ))
    spread = Spread(
        spread_id=f"spread-eval-{doc_prefix}",
        credit_file_id=credit_file_id,
        template_version="aldergrove-spread-v1",
        periods=periods,
        period_sources=[
            PeriodSource(
                fiscal_year=year, period_label=f"FY{year}",
                document_id=f"{doc_prefix}-{year}",
                document_sha256="cd" * 32, version=1,
            )
            for year in periods
        ],
        lines=lines,
        adjustments=list(adjustments),
    )
    return spread, extractions


def main() -> int:
    failures: list[str] = []

    # -- extraction over the seed borrower --------------------------
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    borrower, facility = ReferenceLosFeed().fetch_application("LOS-2026-0117")
    docs = [
        {
            "kind": DocumentKind.FINANCIAL_STATEMENT.value,
            "filename": f"hartline-fy{year}-statements.json",
            "data_hex": statement_document(year).hex(),
            "period_label": f"FY{year}",
        }
        for year in (2022, 2023, 2024)
    ]
    credit_file = pipeline.open_application(borrower, facility, documents=docs)
    values = pipeline.store.get_extractions(credit_file.credit_file_id)

    complete = 0
    for v in values:
        p = v.provenance
        if p.document_id and p.page >= 1 and ((p.line or "").strip() or (p.cell or "").strip()):
            complete += 1
    completeness = (complete / len(values)) if values else 0.0
    print(f"provenance_completeness: {complete}/{len(values)} = {completeness:.3f} (required 1.000)")
    if completeness != 1.0 or not values:
        failures.append("provenance completeness below 100%")

    truth = ground_truth()
    correct = sum(1 for v in values if truth.get((v.fiscal_year, v.field)) == v.value)
    print(f"extraction_correctness: {correct}/{len(values)}")
    if correct != len(values):
        failures.append("extracted values diverge from ground truth")

    # -- checklist coverage ------------------------------------------
    covered = 0
    for ft, specifics in EXPECTED_SPECIFICS.items():
        items = generate_checklist(
            FacilityRequest(facility_type=ft, amount=Decimal("1"), purpose="eval", term_months=12)
        )
        kinds = {i.kind for i in items}
        if specifics <= kinds and all(i.requirement_source.startswith("policy:") for i in items):
            covered += 1
        else:
            failures.append(f"checklist for {ft.value} missing {specifics - kinds}")
    print(f"checklist_coverage: {covered}/{len(EXPECTED_SPECIFICS)} facility types")

    # -- provenance rejection (negative control) ----------------------
    smuggled = ExtractedValue.model_construct(
        field="revenue", value=Decimal("1"), unit="USD", period_label="FY2024",
        fiscal_year=2024,
        provenance=Provenance.model_construct(document_id="", page=1, line=None, cell=None),
    )
    try:
        receive_extraction([smuggled])
        print("provenance_rejection: FAIL (batch accepted)")
        failures.append("incomplete-provenance batch was accepted")
    except ProvenanceError:
        print("provenance_rejection: rejected as required")

    # -- round 2: spread vs ground truth ------------------------------
    spread = pipeline.build_spread(credit_file.credit_file_id)
    lines_by_key = {line.key: line for line in spread.lines}
    matched = 0
    for (year, field), expected in truth.items():
        line = lines_by_key.get(field)
        if line is not None and line.values.get(year) == expected:
            matched += 1
    print(f"spread_values_vs_truth: {matched}/{len(truth)} exact across "
          f"{len(spread.periods)} periods")
    if matched != len(truth):
        failures.append("spread values diverge from ground truth")

    rebuilt = pipeline.build_spread(credit_file.credit_file_id)
    deterministic = rebuilt.model_dump_json() == spread.model_dump_json()
    print(f"spread_determinism: {'byte-identical rebuild' if deterministic else 'MISMATCH'} "
          f"({spread.spread_id})")
    if not deterministic:
        failures.append("spread rebuild is not byte-identical")

    seed_unmapped = len(spread.unmapped)
    print(f"unmapped_line_counter (seed borrower): {seed_unmapped} (required 0)")
    if seed_unmapped != 0:
        failures.append("seed borrower spread carries unmapped lines")

    # -- round 2: adjustments ------------------------------------------
    fy2024_doc = next(
        d for d in credit_file.documents if d.period_label == "FY2024"
    )
    valid = SpreadAdjustment(
        credit_file_id=credit_file.credit_file_id,
        line_key="ebit",
        fiscal_year=2024,
        amount=Decimal("150000"),
        reason_code=AdjustmentReasonCode.NON_RECURRING_EXPENSE,
        reason="One-time plant relocation cost inside operating expenses",
        source=Provenance(document_id=fy2024_doc.document_id, page=2, line="L5"),
        proposed_by="credit-officer:j.lee",
    )
    adjusted_spread = pipeline.add_adjustment(credit_file.credit_file_id, valid)
    ebit = next(line for line in adjusted_spread.lines if line.key == "ebit")
    views_ok = (
        ebit.values[2024] == truth[(2024, "ebit")]
        and ebit.adjusted_values[2024] == truth[(2024, "ebit")] + Decimal("150000")
    )
    print(f"adjustment_views: unadjusted {ebit.values[2024]}, "
          f"adjusted {ebit.adjusted_values[2024]} "
          f"({'ok' if views_ok else 'WRONG'})")
    if not views_ok:
        failures.append("adjusted/unadjusted spread views are wrong")

    rejected = 0
    attempts = [
        SpreadAdjustment.model_construct(
            **{**valid.model_dump(), "adjustment_id": "adj-eval-nosource",
               "source": None}
        ),
        SpreadAdjustment.model_construct(
            adjustment_id="adj-eval-noreason",
            credit_file_id=credit_file.credit_file_id,
            line_key="ebit", fiscal_year=2024, amount=Decimal("1"),
            reason_code=AdjustmentReasonCode.OTHER, reason="   ",
            source=valid.source, proposed_by="credit-officer:j.lee",
        ),
        SpreadAdjustment(
            credit_file_id=credit_file.credit_file_id,
            line_key="not_a_spread_line", fiscal_year=2024, amount=Decimal("1"),
            reason_code=AdjustmentReasonCode.OTHER, reason="eval control",
            source=valid.source, proposed_by="credit-officer:j.lee",
        ),
    ]
    for attempt in attempts:
        try:
            pipeline.add_adjustment(credit_file.credit_file_id, attempt)
        except CreditError:
            rejected += 1
    print(f"adjustment_rejection: {rejected}/{len(attempts)} rejected")
    if rejected != len(attempts):
        failures.append("an invalid adjustment was accepted")

    # -- round 2: unmapped control + restatement ------------------------
    control = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    control_file = control.open_application(borrower, facility, documents=[])
    rich = _modified_statement(
        2024, extra_bs_lines=[("Goodwill", "250000"), ("Other assets", "61500")]
    )
    v1_doc = _register_and_extract(
        control, control_file.credit_file_id, rich,
        "hartline-fy2024-rich.json", "FY2024",
    )
    control_spread = control.build_spread(control_file.credit_file_id)
    carried = sorted(entry.name for entry in control_spread.unmapped)
    print(f"unmapped_line_counter (control): {len(carried)} carried {carried}")
    if carried != ["Goodwill", "Other assets"]:
        failures.append("control document's unmapped lines were not carried")

    corrected = _modified_statement(
        2024,
        extra_bs_lines=[("Goodwill", "250000"), ("Other assets", "61500")],
        overrides={"Revenue": "11980000"},
    )
    v2_doc = _register_and_extract(
        control, control_file.credit_file_id, corrected,
        "hartline-fy2024-corrected.json", "FY2024",
    )
    restated = control.build_spread(control_file.credit_file_id)
    revenue = next(line for line in restated.lines if line.key == "revenue")
    source = next(s for s in restated.period_sources if s.fiscal_year == 2024)
    prior_kept = any(
        v.field == "revenue" and v.value == Decimal("12374000")
        and v.provenance.document_id == v1_doc.document_id
        for v in control.store.get_extractions(control_file.credit_file_id)
    )
    restated_ok = (
        revenue.values[2024] == Decimal("11980000")
        and source.version == 2
        and source.document_id == v2_doc.document_id
        and source.superseded_document_ids == [v1_doc.document_id]
        and prior_kept
    )
    print(f"restatement_supersession: FY2024 rests on version {source.version} "
          f"({v2_doc.document_id}); prior version "
          f"{'retained' if prior_kept else 'LOST'}")
    if not restated_ok:
        failures.append("restatement did not supersede cleanly")

    # -- round 3: computation engine vs hand computation ---------------
    # Fresh pipeline: the seed borrower's pack, spot-checked by hand.
    #   current ratio FY2024 = 3,951,000 / 1,749,000 = 2.2590
    #   working capital      = 3,951,000 − 1,749,000 = 2,202,000
    #   funded debt          = 360,000 + 2,760,000   = 3,120,000
    #   gross margin         = 3,712,200 / 12,374,000 = 0.3000
    r3 = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    r3_file = r3.open_application(borrower, facility, documents=docs)
    hartline_pack = r3.compute_metrics(r3_file.credit_file_id)

    def _fig(pack, key, year):
        return next(
            f for f in pack.figures
            if f.key == key and f.fiscal_year == year
        )

    spot = {
        "current_ratio": Decimal("2.2590"),
        "working_capital": Decimal("2202000.00"),
        "total_debt": Decimal("3120000.00"),
        "gross_margin": Decimal("0.3000"),
    }
    spot_ok = all(
        _fig(hartline_pack, key, 2024).value == expected
        for key, expected in spot.items()
    )
    ebitda_fig = _fig(hartline_pack, "ebitda", 2024)
    honest_nc = (
        ebitda_fig.status == "not_computable"
        and any("depreciation_and_amortization" in m
                for m in ebitda_fig.missing_inputs)
    )
    print(f"computation_spot_checks (seed borrower): "
          f"{'ok' if spot_ok else 'WRONG'}; EBITDA not computable "
          f"without a cash-flow statement: "
          f"{'named' if honest_nc else 'NOT NAMED'}")
    if not spot_ok:
        failures.append("seed borrower computation spot checks diverge")
    if not honest_nc:
        failures.append("missing D&A was not named as a missing input")

    # A hand-built fixture borrower, full statements, hand-computed:
    #   EBITDA = (500,000 + 50,000 add-back) + 120,000 = 670,000
    #   debt service = 200,000 + 90,000 + 10,000 leases = 300,000
    #   DSCR = 670,000 / 300,000 = 2.2333
    #   debt/EBITDA = 1,000,000 / 670,000 = 1.4925
    fixture_figs = {
        2023: {"current_portion_ltd": "200000"},
        2024: {
            "ebit": "500000", "depreciation_and_amortization": "120000",
            "interest_expense": "90000", "current_portion_ltd": "220000",
            "long_term_debt": "780000", "total_current_assets": "900000",
            "total_current_liabilities": "600000",
            "capital_expenditures": "200000",
            "net_cash_from_operations": "560000",
        },
    }
    fx_spread, fx_extractions = _fixture_spread(
        r3_file.credit_file_id, "fxdoc", fixture_figs,
        adjustments=[SpreadAdjustment(
            credit_file_id=r3_file.credit_file_id,
            line_key="ebit", fiscal_year=2024, amount=Decimal("50000"),
            reason_code=AdjustmentReasonCode.NON_RECURRING_EXPENSE,
            reason="One-time relocation cost",
            source=Provenance(document_id="fxdoc-2024", page=2, line="L5"),
            proposed_by="credit-officer:eval",
        )],
    )
    fx_pack = compute_entity_pack(
        credit_file_id=r3_file.credit_file_id, entity="Eval Fixture",
        spread=fx_spread, extractions=fx_extractions,
        supplemental=[ExtractedValue(
            field="capital_lease_payments", value=Decimal("10000"),
            period_label="FY2024", fiscal_year=2024,
            provenance=Provenance(document_id="fxdoc-debt", page=1, line="L3"),
        )],
    )
    hand = {
        "ebitda": Decimal("670000.00"),
        "debt_service": Decimal("300000.00"),
        "dscr": Decimal("2.2333"),
        "debt_to_ebitda": Decimal("1.4925"),
        "fixed_charge_coverage": Decimal("1.5667"),
        "free_cash_flow": Decimal("360000.00"),
    }
    matched = sum(
        1 for key, expected in hand.items()
        if _fig(fx_pack, key, 2024).value == expected
    )
    print(f"computation_correctness: {matched}/{len(hand)} vs hand computation")
    if matched != len(hand):
        failures.append("engine figures diverge from hand computation")

    trace = verify_trace(fx_pack, spread=fx_spread)
    hartline_trace = verify_trace(
        hartline_pack, spread=r3.get_spread(r3_file.credit_file_id)
    )
    trace_total = trace.figures_checked + hartline_trace.figures_checked
    trace_complete = trace.figures_complete + hartline_trace.figures_complete
    completeness = (trace_complete / trace_total) if trace_total else 0.0
    print(f"derivation_trace_completeness: {trace_complete}/{trace_total} "
          f"= {completeness:.3f} (required 1.000)")
    if completeness != 1.0:
        failures.append(f"derivation traces incomplete: {trace.gaps + hartline_trace.gaps}")

    recomputed = compute_entity_pack(
        credit_file_id=r3_file.credit_file_id, entity="Eval Fixture",
        spread=fx_spread, extractions=fx_extractions,
        supplemental=fx_pack.supplemental,
    )
    deterministic = (
        recomputed.pack_id == fx_pack.pack_id
        and recomputed.model_dump_json() == fx_pack.model_dump_json()
        and hartline_pack.pack_id
        == r3.compute_metrics(r3_file.credit_file_id).pack_id
    )
    print(f"computation_determinism: "
          f"{'identical artefact on recompute' if deterministic else 'MISMATCH'}")
    if not deterministic:
        failures.append("recomputation did not return the identical artefact")

    # Global view: guarantor EBITDA 170,000 (150,000 + 20,000), debt
    # service 70,000 (40,000 + 30,000). Global cash flow =
    # 670,000 + 170,000 − 50,000 rent elimination = 790,000; global
    # debt service = 300,000 + 70,000 = 370,000; global DSCR =
    # 790,000 / 370,000 = 2.1351.
    g_spread, g_extractions = _fixture_spread(
        r3_file.credit_file_id, "fxguar",
        {
            2023: {"current_portion_ltd": "40000"},
            2024: {
                "ebit": "150000", "depreciation_and_amortization": "20000",
                "interest_expense": "30000", "current_portion_ltd": "55000",
            },
        },
    )
    g_pack = compute_entity_pack(
        credit_file_id=r3_file.credit_file_id, entity="Eval Guarantor",
        spread=g_spread, extractions=g_extractions,
    )
    global_pack = compute_global_pack(
        credit_file_id=r3_file.credit_file_id,
        borrower_pack=fx_pack,
        entity_packs={"Eval Guarantor": g_pack},
        eliminations=[Elimination(
            name="intercompany_rent", fiscal_year=2024,
            amount=Decimal("50000"), target="cash_flow",
            reason="Rent counted in both entities' figures",
        )],
        expected_guarantors=["Eval Guarantor"],
    )
    global_dscr = _fig(global_pack, "global_dscr", 2024).value
    print(f"global_dscr_vs_hand: {global_dscr} (hand-computed 2.1351)")
    if global_dscr != Decimal("2.1351"):
        failures.append("global DSCR diverges from hand computation")

    try:
        compute_global_pack(
            credit_file_id=r3_file.credit_file_id,
            borrower_pack=fx_pack,
            eliminations=[Elimination.model_construct(
                name="quiet_removal", fiscal_year=2024,
                amount=Decimal("1000"), target="cash_flow", reason="  ",
            )],
        )
        print("elimination_rejection: FAIL (accepted)")
        failures.append("an unexplained elimination was accepted")
    except CreditError:
        print("elimination_rejection: rejected as required")

    # -- round 4: the policy pack --------------------------------------
    # Hand-decided expectations. The seed borrower (Hartline): no
    # cash-flow page, so DSCR and fixed-charge coverage are
    # not_evaluable (the missing input is depreciation &
    # amortisation); leverage 4,509,000 / 2,940,000 = 1.5337 passes
    # the manufacturing ceiling of 2.00; the file is short most of
    # its checklist, so completeness fails; FY2024 statements are
    # 5 complete months old at 2025-06-30, within the 18-month v1
    # limit; guarantors are named but no global pack is handed to a
    # pipeline evaluation, so the global test is not_evaluable; a
    # term loan carries no appraisal test.
    as_of = date(2025, 6, 30)
    graded: list[tuple] = []  # (report, borrower pack, global pack)

    hartline_report = r3.evaluate_policy(
        r3_file.credit_file_id, pack_version="v1", as_of=as_of
    )
    graded.append((hartline_report, hartline_pack, None))
    hartline_expected = {
        "max_leverage_by_segment": "pass",
        "min_dscr": "not_evaluable",
        "min_global_dscr": "not_evaluable",
        "min_fixed_charge_coverage": "not_evaluable",
        "max_single_borrower_concentration": "pass",
        "required_documents_complete": "fail",
        "financial_statement_age": "pass",
        "appraisal_age": "not_evaluable",
    }
    hartline_actual = {o.test_id: o.outcome for o in hartline_report.outcomes}
    dscr_outcome = hartline_report.outcome("min_dscr")
    names_gap = any(
        "depreciation_and_amortization" in m
        for m in dscr_outcome.missing_inputs
    )
    print(f"policy_outcomes_vs_expectations (seed borrower): "
          f"{sum(1 for k, v in hartline_expected.items() if hartline_actual.get(k) == v)}"
          f"/{len(hartline_expected)} as hand-decided; DSCR gap "
          f"{'named' if names_gap else 'NOT NAMED'}")
    if hartline_actual != hartline_expected or not names_gap:
        failures.append("seed borrower policy outcomes diverge from hand-decided expectations")

    # A clean-pass file, built by hand: the coverage borrower
    # (DSCR 1.3000, FCCR 1.1333, leverage 0.8000) with a guarantor
    # whose combined view gives global DSCR 1.5135, a complete
    # checklist, FY2024 statements, a $2,000,000 term loan.
    def _policy_file(credit_file_id, *, amount="2000000", guarantors=(),
                     segment=BorrowerSegment.MANUFACTURING,
                     satisfied=True):
        facility = FacilityRequest(
            facility_type=FacilityType.TERM_LOAN,
            amount=Decimal(amount), purpose="eval fixture",
            term_months=60,
            guarantors=[
                Guarantor(name=g, ownership_pct=Decimal("100"))
                for g in guarantors
            ],
        )
        cf = CreditFile(
            credit_file_id=credit_file_id,
            borrower=Borrower(
                legal_name="Eval Fixture Borrower, LLC",
                ein_last4="9999", segment=segment,
                state_of_formation="OH", years_in_business=8,
                primary_contact_name="Eval Fixture",
                primary_contact_email="eval@fixture.example",
            ),
            facility=facility,
        )
        cf.checklist = generate_checklist(facility)
        if satisfied:
            for item in cf.checklist:
                item.satisfied_by_document_id = "doc-eval"
        return cf

    coverage_figs = {
        2023: {"current_portion_ltd": "200000"},
        2024: {
            "ebit": "270000", "depreciation_and_amortization": "120000",
            "interest_expense": "100000", "current_portion_ltd": "220000",
            "capital_expenditures": "50000",
            "total_liabilities": "800000", "total_equity": "1000000",
        },
    }
    cov_spread, cov_extractions = _fixture_spread(
        "cf-eval-clean", "covdoc", coverage_figs
    )
    cov_pack = compute_entity_pack(
        credit_file_id="cf-eval-clean", entity="Eval Fixture Borrower, LLC",
        spread=cov_spread, extractions=cov_extractions,
    )
    clean_file = _policy_file("cf-eval-clean", guarantors=("Eval Guarantor",))
    g_spread2, g_extractions2 = _fixture_spread(
        "cf-eval-clean", "guarstrong",
        {
            2023: {"current_portion_ltd": "40000"},
            2024: {
                "ebit": "150000", "depreciation_and_amortization": "20000",
                "interest_expense": "30000",
            },
        },
    )
    g_pack2 = compute_entity_pack(
        credit_file_id="cf-eval-clean", entity="Eval Guarantor",
        spread=g_spread2, extractions=g_extractions2,
    )
    clean_global = compute_global_pack(
        credit_file_id="cf-eval-clean", borrower_pack=cov_pack,
        entity_packs={"Eval Guarantor": g_pack2},
        expected_guarantors=["Eval Guarantor"],
    )
    clean_report = evaluate_policy(
        clean_file, cov_pack, policy_version="v1",
        global_pack=clean_global, as_of=as_of,
    )
    graded.append((clean_report, cov_pack, clean_global))
    clean_expected = {
        "max_leverage_by_segment": "pass",
        "min_dscr": "pass",
        "min_global_dscr": "pass",
        "min_fixed_charge_coverage": "pass",
        "max_single_borrower_concentration": "pass",
        "required_documents_complete": "pass",
        "financial_statement_age": "pass",
        "appraisal_age": "not_evaluable",
    }
    clean_actual = {o.test_id: o.outcome for o in clean_report.outcomes}
    print(f"policy_outcomes_vs_expectations (clean pass): "
          f"{sum(1 for k, v in clean_expected.items() if clean_actual.get(k) == v)}"
          f"/{len(clean_expected)} as hand-decided")
    if clean_actual != clean_expected:
        failures.append("clean-pass policy outcomes diverge from hand-decided expectations")

    # The leverage-fail file: liabilities 5,000,000 over tangible
    # net worth 1,000,000 = 5.0000 against the 2.00 ceiling.
    lev_spread, lev_extractions = _fixture_spread(
        "cf-eval-levfail", "levdoc",
        {2024: {"total_liabilities": "5000000", "total_equity": "1000000"}},
    )
    lev_pack = compute_entity_pack(
        credit_file_id="cf-eval-levfail", entity="Eval Fixture Borrower, LLC",
        spread=lev_spread, extractions=lev_extractions,
    )
    lev_report = evaluate_policy(
        _policy_file("cf-eval-levfail"), lev_pack,
        policy_version="v1", as_of=as_of,
    )
    graded.append((lev_report, lev_pack, None))
    lev_outcome = lev_report.outcome("max_leverage_by_segment")
    lev_ok = (
        lev_outcome.outcome == "fail"
        and lev_outcome.severity == "blocker"
        and lev_outcome.threshold == Decimal("2.00")
        and lev_report.summary["blocker_failures"] >= 1
    )
    print(f"policy_outcomes_vs_expectations (leverage fail): "
          f"{'blocker failure at 5.0000 vs 2.00' if lev_ok else 'WRONG'}")
    if not lev_ok:
        failures.append("leverage-fail fixture did not fail as a blocker")

    # Version discipline: the coverage borrower at DSCR 1.3000
    # passes v1 (floor 1.25) and fails v2 (floor 1.35); its v1
    # report, once stored, is byte-identical after the v2 run, and
    # the seed file's policy history records both pinned versions.
    disc_file = _policy_file("cf-eval-clean")
    v1_report = evaluate_policy(
        disc_file, cov_pack, policy_version="v1", as_of=as_of
    )
    graded.append((v1_report, cov_pack, None))
    r3.store.save_policy_report(v1_report)
    v2_report = evaluate_policy(
        disc_file, cov_pack, policy_version="v2", as_of=as_of
    )
    graded.append((v2_report, cov_pack, None))
    r3.evaluate_policy(r3_file.credit_file_id, pack_version="v2", as_of=as_of)
    kept = r3.store.get_policy_report(v1_report.report_id)
    history_versions = {
        h.policy_version
        for h in r3.store.get_credit_file(r3_file.credit_file_id).policy_history
    }
    discipline_ok = (
        v1_report.outcome("min_dscr").outcome == "pass"
        and v2_report.outcome("min_dscr").outcome == "fail"
        and v1_report.report_id != v2_report.report_id
        and kept is not None
        and kept.model_dump_json() == v1_report.model_dump_json()
        and {"v1", "v2"} <= history_versions
    )
    print(f"policy_version_discipline: "
          f"{'v1 report kept intact, history records v1 + v2' if discipline_ok else 'BROKEN'}")
    if not discipline_ok:
        failures.append("policy version discipline is broken")

    # The silent-pass counter: re-check every figure-test "pass"
    # against the pack it read. A pass over a missing or
    # not-computable figure — a not_evaluable wearing a pass's
    # clothes — is a violation.
    operators = {
        ">=": lambda v, t: v >= t, ">": lambda v, t: v > t,
        "<=": lambda v, t: v <= t, "<": lambda v, t: v < t,
    }
    violations = 0
    passes_checked = 0
    for report, b_pack, g_pack in graded:
        tests = {
            t.test_id: t
            for t in get_policy_pack(report.policy_version).tests
        }
        for outcome in report.outcomes:
            if outcome.outcome != "pass":
                continue
            test = tests[outcome.test_id]
            if test.kind != "figure":
                continue
            passes_checked += 1
            source = g_pack if test.scope == "global" else b_pack
            figure = next(
                (f for f in source.figures
                 if f.key == test.figure_key
                 and f.fiscal_year == outcome.fiscal_year),
                None,
            )
            if (
                figure is None
                or figure.status != "computed"
                or figure.value is None
                or outcome.threshold is None
                or not operators[test.operator](figure.value, outcome.threshold)
            ):
                violations += 1
    print(f"policy_silent_pass_counter: {violations} violations over "
          f"{passes_checked} figure-test passes (required 0)")
    if violations:
        failures.append("a policy pass rests on a figure that does not support it")

    # Determinism: the same file, version and inputs reproduce the
    # identical report artefact.
    repeated = r3.evaluate_policy(
        r3_file.credit_file_id, pack_version="v1", as_of=as_of
    )
    deterministic = (
        repeated.report_id == hartline_report.report_id
        and repeated.model_dump_json() == hartline_report.model_dump_json()
    )
    print(f"policy_determinism: "
          f"{'identical report on re-evaluation' if deterministic else 'MISMATCH'}")
    if not deterministic:
        failures.append("policy re-evaluation did not reproduce the identical report")

    # ---------------------------------------------------------------
    # Round 5: memo drafting + the figure verifier. Memos are
    # composed over the graded artefacts — the seed borrower (pinned
    # v1 report, via the pipeline), the clean-pass file with its
    # global pack, the coverage borrower under v1 and v2, and the
    # leverage-fail file — and graded on trace completeness,
    # tamper refusal, exceptions integrity, determinism, and the
    # offline composer's figure discipline.
    # ---------------------------------------------------------------
    hartline_inputs = MemoInputs(
        credit_file=r3.store.get_credit_file(r3_file.credit_file_id),
        spread=r3.get_spread(r3_file.credit_file_id),
        pack=hartline_pack,
        policy_report=hartline_report,
    )
    hartline_memo = r3.compose_memo(
        r3_file.credit_file_id, report_id=hartline_report.report_id
    )
    memo_cases: list[tuple[object, MemoInputs]] = [
        (hartline_memo, hartline_inputs),
        (None, MemoInputs(
            credit_file=clean_file, spread=cov_spread, pack=cov_pack,
            policy_report=clean_report, global_pack=clean_global,
        )),
        (None, MemoInputs(
            credit_file=disc_file, spread=cov_spread, pack=cov_pack,
            policy_report=v1_report,
        )),
        (None, MemoInputs(
            credit_file=disc_file, spread=cov_spread, pack=cov_pack,
            policy_report=v2_report,
        )),
        (None, MemoInputs(
            credit_file=_policy_file("cf-eval-levfail"),
            spread=lev_spread, pack=lev_pack, policy_report=lev_report,
        )),
    ]
    graded_memos = []
    for memo, inputs in memo_cases:
        if memo is None:
            memo = compose_memo(
                inputs.credit_file, inputs.spread, inputs.pack,
                inputs.policy_report, global_pack=inputs.global_pack,
            )
        graded_memos.append((memo, inputs))

    # Figure-trace completeness, re-extracted from the rendered
    # text here — the verifier's own count is not taken on trust.
    figures_total = 0
    figures_traced = 0
    for memo, _inputs in graded_memos:
        extracted = extract_figures(render_memo(memo))
        figures_total += len(extracted)
        if len(memo.verification.figures) == len(extracted):
            figures_traced += len(extracted)
    trace_completeness = (
        figures_traced / figures_total if figures_total else 0.0
    )
    print(f"memo_figure_trace_completeness: {figures_traced}/{figures_total} "
          f"= {trace_completeness:.3f} (required 1.000)")
    if trace_completeness != 1.0:
        failures.append("a composed memo carries an untraced figure")

    # Composer figure discipline: every number in the rendered text
    # exists in the inputs the memo was composed from.
    discipline_violations = 0
    for memo, inputs in graded_memos:
        universe = build_trace_universe(inputs)
        for figure in extract_figures(render_memo(memo)):
            if figure.value not in universe:
                discipline_violations += 1
    print(f"memo_composer_figure_discipline: {discipline_violations} "
          f"violations over {len(graded_memos)} memos (required 0)")
    if discipline_violations:
        failures.append("the offline composer emitted a figure outside its inputs")

    # Tamper cases: each altered memo must be REFUSED, the offender
    # named. Acceptance of even one fails the gate.
    def _tamper(memo, section_key, old, new):
        tampered = memo.model_copy(deep=True)
        section = tampered.section(section_key)
        for i, paragraph in enumerate(section.paragraphs):
            if old in paragraph:
                section.paragraphs[i] = paragraph.replace(old, new, 1)
                return tampered
        for table in section.tables:
            for r, row in enumerate(table.rows):
                for c, cell in enumerate(row):
                    if old in cell:
                        table.rows[r][c] = cell.replace(old, new, 1)
                        return tampered
        raise AssertionError(f"{old!r} not in section {section_key!r}")

    tamper_cases = [
        _tamper(hartline_memo, "financial_analysis",
                "$2,202,000.00", "$2,202,000.01"),  # one cent
        _tamper(hartline_memo, "financial_analysis",
                "2.2590×", "2.2950×"),  # transposed ratio
    ]
    invented = hartline_memo.model_copy(deep=True)
    invented.section("strengths").paragraphs.append(
        "The borrower also holds $7,777,777.77 in unencumbered reserves."
    )
    tamper_cases.append(invented)
    accepted = 0
    for tampered in tamper_cases:
        try:
            verify_memo(tampered, hartline_inputs)
            accepted += 1
        except MemoVerificationError:
            pass
    print(f"memo_untraceable_acceptance: {accepted} of "
          f"{len(tamper_cases)} tamper cases accepted (required 0)")
    if accepted:
        failures.append("the figure verifier accepted a tampered memo")

    # Exceptions integrity: listed == the report's non-pass outcomes
    # on every graded memo, and a memo tampered to drop one refused.
    omissions = 0
    for memo, inputs in graded_memos:
        expected = [
            o.test_id for o in inputs.policy_report.non_pass_outcomes()
        ]
        if memo.verification.exceptions_listed != expected:
            omissions += 1
    dropped = hartline_memo.model_copy(deep=True)
    dropped_section = dropped.section("policy_exceptions")
    dropped_section.exception_test_ids.remove("min_dscr")
    dropped_section.paragraphs = [
        p for p in dropped_section.paragraphs
        if not p.startswith("Minimum debt service coverage (borrower)")
    ]
    try:
        verify_memo(dropped, hartline_inputs)
        omissions += 1  # an omission the verifier accepted
    except MemoVerificationError:
        pass
    print(f"memo_exceptions_omission: {omissions} over "
          f"{len(graded_memos)} graded memos + 1 drop case (required 0)")
    if omissions:
        failures.append("a memo omitted a policy exception")

    # Determinism: identical inputs reproduce the identical memo id.
    recomposed = compose_memo(
        disc_file, cov_spread, cov_pack, v1_report
    )
    memo_deterministic = (
        recomposed.memo_id == graded_memos[2][0].memo_id
        and r3.compose_memo(
            r3_file.credit_file_id, report_id=hartline_report.report_id
        ).memo_id == hartline_memo.memo_id
    )
    print(f"memo_determinism: "
          f"{'identical memo id on recomposition' if memo_deterministic else 'MISMATCH'}")
    if not memo_deterministic:
        failures.append("memo recomposition did not reproduce the identical memo")

    # ---------------------------------------------------------------
    # Round 6: the credit officer gate, graded on its refusals and
    # its record. The Hartline draft (pipeline r3) takes the
    # edited-approval path; a second file opened cold takes the
    # return → recompose → approve path and the illegal
    # transitions. Zero tolerance throughout: one accepted
    # anonymous action, illegal transition or bad edit fails.
    # ---------------------------------------------------------------
    OFFICER = "officer-eval"
    OFFICER_ROLE = "credit_officer"
    RM = "rm-eval"
    RM_ROLE = "relationship_manager"
    gate_cf = hartline_memo.credit_file_id
    gate_mid = hartline_memo.memo_id

    # Anonymous actions: an empty id or role names nobody, and the
    # gate records nobody.
    anonymous_attempts = [
        lambda: r3.submit_memo_for_review(
            gate_cf, gate_mid, actor="", actor_role=RM_ROLE
        ),
        lambda: r3.approve_memo(
            gate_cf, gate_mid, officer_id="", officer_role=OFFICER_ROLE
        ),
        lambda: r3.return_memo(
            gate_cf, gate_mid, officer_id=OFFICER, officer_role="",
            reason_codes=["insufficient_analysis"],
        ),
    ]
    anonymous_accepted = 0
    for attempt in anonymous_attempts:
        try:
            attempt()
            anonymous_accepted += 1
        except ValidationError:
            pass
    print(f"officer_anonymous_action_accepted: {anonymous_accepted} of "
          f"{len(anonymous_attempts)} anonymous actions accepted "
          f"(required 0)")
    if anonymous_accepted:
        failures.append("the officer gate accepted an anonymous action")

    # The Hartline draft enters review.
    r3.submit_memo_for_review(gate_cf, gate_mid, actor=RM, actor_role=RM_ROLE)
    memos_before = len(r3.store.list_memos(gate_cf))

    # An edit introducing an untraceable figure stores nothing.
    untraceable_stored = 0
    try:
        r3.approve_memo(
            gate_cf, gate_mid, officer_id=OFFICER,
            officer_role=OFFICER_ROLE,
            edits={
                "recommendation": [
                    "Recommend approval; reserves of $8,888,888.88 "
                    "stand behind the facility."
                ]
            },
        )
        untraceable_stored = 1  # the approval itself landed
    except MemoVerificationError:
        if (
            len(r3.store.list_memos(gate_cf)) != memos_before
            or r3.store.list_memo_diffs(gate_cf)
        ):
            untraceable_stored = 1
    print(f"officer_untraceable_edit_stored: {untraceable_stored} "
          f"stored (required 0)")
    if untraceable_stored:
        failures.append("an approval with an untraceable edited figure was stored")

    # An edit aimed at the exceptions section stores nothing.
    exception_drop_stored = 0
    try:
        r3.approve_memo(
            gate_cf, gate_mid, officer_id=OFFICER,
            officer_role=OFFICER_ROLE,
            edits={"policy_exceptions": ["No exceptions."]},
        )
        exception_drop_stored = 1
    except ValidationError:
        if (
            len(r3.store.list_memos(gate_cf)) != memos_before
            or r3.store.list_memo_diffs(gate_cf)
        ):
            exception_drop_stored = 1
    print(f"officer_exception_drop_stored: {exception_drop_stored} "
          f"stored (required 0)")
    if exception_drop_stored:
        failures.append("an approval editing the exceptions section was stored")

    # The clean edited approval lands, with its diff recorded.
    diff_missing = 0
    edited_final = r3.approve_memo(
        gate_cf, gate_mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
        edits={
            "recommendation": [
                "Recommend approval of the $1,500,000.00 term "
                "facility, subject to the conditions recorded below."
            ]
        },
    )
    edited_diff = r3.store.get_memo_diff_by_final(edited_final.memo_id)
    if (
        edited_diff is None
        or edited_diff.action != "approved_with_edits"
        or not any(
            c.kind == "added" and c.new_text == "$1,500,000.00"
            for c in edited_diff.figure_changes
        )
    ):
        diff_missing += 1
    if r3.memo_status(gate_cf, gate_mid) != "officer_approved":
        failures.append("the edited approval did not close the review")

    # Illegal transitions + the return/recompose path, on a cold file.
    gate_pipeline = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    g_borrower, g_facility = ReferenceLosFeed().fetch_application(
        "LOS-2026-0117"
    )
    g_file = gate_pipeline.open_application(g_borrower, g_facility)
    g_cf = g_file.credit_file_id
    g_mid = gate_pipeline.store.list_memos(g_cf)[0].memo_id
    illegal_attempts = [
        lambda: gate_pipeline.approve_memo(
            g_cf, g_mid, officer_id=OFFICER, officer_role=OFFICER_ROLE
        ),  # still a draft
    ]
    gate_pipeline.submit_memo_for_review(
        g_cf, g_mid, actor=RM, actor_role=RM_ROLE
    )
    illegal_attempts.append(
        lambda: gate_pipeline.submit_memo_for_review(
            g_cf, g_mid, actor=RM, actor_role=RM_ROLE
        )  # already in review
    )
    gate_pipeline.return_memo(
        g_cf, g_mid, officer_id=OFFICER, officer_role=OFFICER_ROLE,
        reason_codes=["insufficient_analysis"],
    )
    illegal_attempts.append(
        lambda: gate_pipeline.approve_memo(
            g_cf, g_mid, officer_id=OFFICER, officer_role=OFFICER_ROLE
        )  # returned — must be recomposed first
    )
    illegal_accepted = 0
    for attempt in illegal_attempts:
        try:
            attempt()
            illegal_accepted += 1
        except ValidationError:
            pass
    print(f"officer_illegal_transition_accepted: {illegal_accepted} of "
          f"{len(illegal_attempts)} illegal transitions accepted "
          f"(required 0)")
    if illegal_accepted:
        failures.append("the officer gate accepted an illegal transition")

    g_draft2 = gate_pipeline.recompose_memo(
        g_cf, supersedes_memo_id=g_mid, actor=RM, actor_role=RM_ROLE
    )
    gate_pipeline.submit_memo_for_review(
        g_cf, g_draft2.memo_id, actor=RM, actor_role=RM_ROLE
    )
    g_final = gate_pipeline.approve_memo(
        g_cf, g_draft2.memo_id, officer_id=OFFICER,
        officer_role=OFFICER_ROLE,
    )
    asis_diff = gate_pipeline.store.get_memo_diff_by_final(g_final.memo_id)
    if (
        asis_diff is None
        or asis_diff.section_changes
        or asis_diff.figure_changes
    ):
        diff_missing += 1
    print(f"officer_diff_missing_for_edited_approval: {diff_missing} "
          f"missing over 2 approvals (required 0)")
    if diff_missing:
        failures.append("an approval landed without its diff record")
    gate_positive = (
        edited_final.status == "officer_approved"
        and g_final.status == "officer_approved"
        and gate_pipeline.memo_status(g_cf, g_mid) == "returned"
    )
    print(f"officer_gate_positive_paths: "
          f"{'edited approval + return/recompose/approve landed' if gate_positive else 'FAILED'}")
    if not gate_positive:
        failures.append("an officer gate positive path did not land")

    # ---------------------------------------------------------------
    # The committee-ready fixture (rounds 7 + 9). Since round 9
    # the committee can only be reached on a committee-ready file,
    # so the committee fixtures use a purpose-built borrower —
    # Calloway Precision Manufacturing — whose file is complete by
    # construction: three-section statements for FY2023–FY2025
    # (every figure computes), every checklist document on file.
    # Hand-set anchors under v1: FY2025 EBITDA $420,000 over debt
    # service $320,000 (DSCR 1.3125, a pass); FY2025 leverage
    # 5.0000 against the 2.00 ceiling (a fail — credit judgment,
    # carried as a memo exception, NOT a readiness blocker); no
    # guarantors (the global test does not apply).
    # ---------------------------------------------------------------
    _LABELS = {t.field: t.label for t in TEMPLATE_LINES}
    _BS = (
        "cash_and_equivalents", "accounts_receivable", "inventory",
        "total_current_assets", "net_fixed_assets", "total_assets",
        "accounts_payable", "current_portion_ltd",
        "total_current_liabilities", "long_term_debt",
        "total_liabilities", "total_equity",
    )
    _IS = (
        "revenue", "cost_of_goods_sold", "gross_profit",
        "operating_expenses", "ebit", "interest_expense", "net_income",
    )
    _CF = (
        "depreciation_and_amortization", "change_in_receivables",
        "change_in_inventory", "change_in_payables",
        "net_cash_from_operations", "capital_expenditures",
        "net_cash_from_investing", "debt_proceeds",
        "debt_repayments", "net_cash_from_financing",
        "net_change_in_cash",
    )
    _READY_FIGURES = {
        2023: dict(zip(_BS, (
            200000, 800000, 1000000, 2000000, 3000000, 5000000,
            650000, 200000, 1100000, 2900000, 4000000, 1000000,
        )), **dict(zip(_IS, (
            7500000, 5250000, 2250000, 1970000, 280000, 90000, 145000,
        ))), **dict(zip(_CF, (
            100000, -40000, -50000, 30000, 300000, 50000, -50000,
            50000, -180000, -130000, 120000,
        )))),
        2024: dict(zip(_BS, (
            250000, 840000, 1110000, 2200000, 3300000, 5500000,
            700000, 220000, 1200000, 3300000, 4500000, 1000000,
        )), **dict(zip(_IS, (
            8200000, 5740000, 2460000, 2140000, 320000, 95000, 170000,
        ))), **dict(zip(_CF, (
            110000, -40000, -110000, 50000, 350000, 55000, -55000,
            80000, -200000, -120000, 175000,
        )))),
        2025: dict(zip(_BS, (
            300000, 900000, 1200000, 2400000, 3600000, 6000000,
            800000, 240000, 1400000, 3600000, 5000000, 1000000,
        )), **dict(zip(_IS, (
            9000000, 6300000, 2700000, 2400000, 300000, 100000, 150000,
        ))), **dict(zip(_CF, (
            120000, -60000, -90000, 100000, 380000, 60000, -60000,
            100000, -220000, -120000, 200000,
        )))),
    }

    def _ready_statement_doc(year):
        figures = _READY_FIGURES[year]

        def page(number, title, fields):
            return {
                "page": number, "title": title,
                "lines": [
                    {"line": i, "label": _LABELS[f], "value": figures[f]}
                    for i, f in enumerate(fields, start=1)
                ],
            }

        return json.dumps({
            "document_type": "financial_statement",
            "borrower": "Calloway Precision Manufacturing, Inc.",
            "fiscal_year": year, "period_label": f"FY{year}",
            "basis": "audited",
            "pages": [
                page(1, "Balance Sheet", _BS),
                page(2, "Income Statement", _IS),
                page(3, "Cash Flow Statement", _CF),
            ],
        }).encode()

    def _ready_tax_doc():
        figures = _READY_FIGURES[2025]
        return json.dumps({
            "document_type": "tax_return",
            "borrower": "Calloway Precision Manufacturing, Inc.",
            "fiscal_year": 2025, "period_label": "FY2025",
            "pages": [{
                "page": 1, "title": "Income Statement (as filed)",
                "lines": [
                    {"line": i, "label": _LABELS[f], "value": figures[f]}
                    for i, f in enumerate(_IS, start=1)
                ],
            }],
        }).encode()

    def _ready_documents():
        docs = [
            {
                "kind": DocumentKind.FINANCIAL_STATEMENT.value,
                "filename": f"calloway-fy{year}-statements.json",
                "data_hex": _ready_statement_doc(year).hex(),
                "period_label": f"FY{year}",
            }
            for year in (2023, 2024, 2025)
        ]
        docs.append({
            "kind": DocumentKind.TAX_RETURN.value,
            "filename": "calloway-business-tax-fy2025.json",
            "data_hex": _ready_tax_doc().hex(),
            "period_label": "FY2025",
        })
        docs.append({
            "kind": DocumentKind.TAX_RETURN.value,
            "filename": "guarantor-personal-tax.json",
            "data_hex": json.dumps({
                "document_type": "tax_return",
                "pages": [{"page": 1, "lines": []}],
            }).encode().hex(),
            "period_label": "FY2025",
        })
        for kind, filename in (
            (DocumentKind.PERSONAL_FINANCIAL_STATEMENT, "guarantor-pfs.json"),
            (DocumentKind.ORG_DOCUMENT, "articles.json"),
            (DocumentKind.DEBT_SCHEDULE, "debt-schedule.json"),
            (DocumentKind.INSURANCE_CERTIFICATE, "insurance.json"),
            (DocumentKind.PROJECTIONS, "projections.json"),
        ):
            docs.append({
                "kind": kind.value, "filename": filename,
                "data_hex": json.dumps({}).encode().hex(),
            })
        return docs

    def _ready_borrower():
        return Borrower(
            legal_name="Calloway Precision Manufacturing, Inc.",
            ein_last4="4417", segment="manufacturing",
            state_of_formation="NC", years_in_business=14,
            primary_contact_name="Dana Whitfield",
            primary_contact_email="dana.whitfield@example.com",
        )

    def _ready_facility():
        return FacilityRequest(
            facility_type=FacilityType.TERM_LOAN,
            amount=Decimal("1500000"),
            purpose="CNC machining line expansion",
            term_months=60, guarantors=[],
        )

    def _officer_final_for(pipe, credit_file_id, *, as_of):
        report = pipe.evaluate_policy(
            credit_file_id, pack_version="v1", as_of=as_of
        )
        memo = pipe.compose_memo(
            credit_file_id, report_id=report.report_id
        )
        pipe.submit_memo_for_review(
            credit_file_id, memo.memo_id,
            actor="rm-eval", actor_role="relationship_manager",
        )
        final = pipe.approve_memo(
            credit_file_id, memo.memo_id,
            officer_id=C_OFFICER, officer_role=C_OFFICER_ROLE,
        )
        return memo, final

    # Round 7: the committee gate, graded on quorum discipline,
    # the decline path's coded reasons, and the positive paths.
    # Three fresh committee-ready files (pinned v1): one takes the
    # refusal probes and then a landed decline; one the
    # superseded-version probes and a conditional approval; one
    # the defer → re-present → approve path. Zero tolerance: one
    # accepted probe fails the gate.
    # ---------------------------------------------------------------
    C_OFFICER = "officer-eval"
    C_OFFICER_ROLE = "credit_officer"
    C_CHAIR = "chair-eval"
    C_MEMBER = "member-eval"
    C_MEMBERS = [
        CommitteeMember(member_id=C_CHAIR, member_role="committee_chair"),
        CommitteeMember(member_id=C_MEMBER, member_role="committee_member"),
    ]

    def _committee_file():
        pipe = CreditPipeline(
            store=InMemoryCreditStore(), documents=LocalDocumentStore()
        )
        c_file = pipe.open_application(
            _ready_borrower(), _ready_facility(),
            documents=_ready_documents(),
        )
        c_memo, c_final = _officer_final_for(
            pipe, c_file.credit_file_id, as_of=date(2026, 6, 30)
        )
        return pipe, c_file.credit_file_id, c_memo, c_final

    def _decide(pipe, cf, mid, **overrides):
        kwargs = dict(
            recorder_id=C_CHAIR, recorder_role="committee_chair",
            members=list(C_MEMBERS), outcome="approve",
        )
        kwargs.update(overrides)
        return pipe.decide_committee(cf, mid, **kwargs)

    def _refused(fn) -> int:
        try:
            fn()
        except (ValidationError, MemoVerificationError):
            return 0
        return 1

    # File 1: the refusal probes, then a properly tied decline.
    cp1, ccf1, cdraft1, cfinal1 = _committee_file()
    cp1.convene_committee(
        ccf1, cfinal1.memo_id, actor=C_OFFICER, actor_role=C_OFFICER_ROLE
    )
    single_member = 0
    single_member += _refused(lambda: _decide(
        cp1, ccf1, cfinal1.memo_id,
        members=C_MEMBERS[:1], quorum_required=1,
    ))
    single_member += _refused(lambda: _decide(
        cp1, ccf1, cfinal1.memo_id, members=C_MEMBERS[:1],
    ))
    single_member += _refused(lambda: _decide(
        cp1, ccf1, cfinal1.memo_id,
        recorder_id=C_OFFICER, recorder_role=C_OFFICER_ROLE,
        members=[CommitteeMember(
            member_id=C_OFFICER, member_role=C_OFFICER_ROLE
        )],
    ))
    print(f"committee_single_member_accepted: {single_member} of 3 "
          f"single-member decisions accepted (required 0)")
    if single_member:
        failures.append("the committee gate accepted a single-member decision")

    no_reasons = 0
    no_reasons += _refused(lambda: _decide(
        cp1, ccf1, cfinal1.memo_id, outcome="decline",
    ))
    no_reasons += _refused(lambda: _decide(
        cp1, ccf1, cfinal1.memo_id, outcome="decline",
        note="The committee was not comfortable.",
    ))
    print(f"committee_decline_without_reasons_accepted: {no_reasons} "
          f"of 2 reasonless declines accepted (required 0)")
    if no_reasons:
        failures.append("a decline without coded reasons was accepted")

    # DSCR PASSED under v1 (1.3125 vs the 1.25 floor).
    passed_test = _refused(lambda: _decide(
        cp1, ccf1, cfinal1.memo_id, outcome="decline",
        adverse_reasons=[AdverseActionReason(
            code="debt_service_coverage_below_policy",
            tie_kind="policy_test",
            test_id="min_dscr",
        )],
    ))
    print(f"committee_decline_passed_test_accepted: {passed_test} of 1 "
          f"passed-test declines accepted (required 0)")
    if passed_test:
        failures.append("a decline citing a passed policy test was accepted")

    untraceable = 0
    untraceable += _refused(lambda: _decide(
        cp1, ccf1, cfinal1.memo_id, outcome="decline",
        adverse_reasons=[AdverseActionReason(
            code="key_figure_shortfall", tie_kind="figure",
            figure_text="$9,999,999.99", figure_value="9999999.99",
            figure_source="computation pack, invented figure",
        )],
    ))
    untraceable += _refused(lambda: _decide(
        cp1, ccf1, cfinal1.memo_id, outcome="decline",
        adverse_reasons=[AdverseActionReason(
            code="required_documentation_missing", tie_kind="file_fact",
            checklist_item_id="chk-01",  # statements: on file
        )],
    ))
    untraceable += _refused(lambda: _decide(
        cp1, ccf1, cfinal1.memo_id,
        outcome="approve_with_conditions",
        conditions=[CommitteeCondition(
            condition_id="", type="financial_covenant",
            metric_key="dscr", operator=">=",
            threshold=Decimal("1.37"),  # traces to nothing pinned
            due_cadence="quarterly",
        )],
    ))
    print(f"committee_untraceable_accepted: {untraceable} of 3 "
          f"untraceable ties/thresholds accepted (required 0)")
    if untraceable:
        failures.append("an untraceable decline tie or condition was accepted")

    decline = _decide(
        cp1, ccf1, cfinal1.memo_id, outcome="decline",
        adverse_reasons=[
            AdverseActionReason(
                code="leverage_above_policy_limit",
                tie_kind="policy_test",
                test_id="max_leverage_by_segment",
            ),
            AdverseActionReason(
                code="financial_information_insufficient",
                tie_kind="policy_test", test_id="min_global_dscr",
            ),
        ],
    )
    notice = cp1.get_adverse_action_notice(ccf1, cfinal1.memo_id)
    decline_ok = (
        cp1.memo_status(ccf1, cfinal1.memo_id) == "declined"
        and decline.reason_vocabulary == "adverse-action-reasons-v1"
        and "NOTICE OF ACTION TAKEN" in notice
        and "Maximum leverage by borrower segment" in notice
        and "5.0000" in notice
        and "names no guarantors" in notice
    )
    print(f"committee_decline_positive_path: "
          f"{'tied decline landed, notice rendered' if decline_ok else 'FAILED'}")
    if not decline_ok:
        failures.append("the tied decline / notice path did not land")

    # File 2: superseded-version probes, then a conditional approval
    # whose covenant threshold (1.25) is the v1 policy floor itself.
    cp2, ccf2, cdraft2, cfinal2 = _committee_file()
    superseded = 0
    superseded += _refused(lambda: cp2.convene_committee(
        ccf2, cdraft2.memo_id, actor=C_OFFICER, actor_role=C_OFFICER_ROLE
    ))
    superseded += _refused(lambda: _decide(cp2, ccf2, cdraft2.memo_id))
    print(f"committee_decision_on_superseded_accepted: {superseded} "
          f"of 2 superseded-version attempts accepted (required 0)")
    if superseded:
        failures.append("the committee acted on a superseded memo version")

    cp2.convene_committee(
        ccf2, cfinal2.memo_id, actor=C_OFFICER, actor_role=C_OFFICER_ROLE
    )
    conditional = _decide(
        cp2, ccf2, cfinal2.memo_id,
        outcome="approve_with_conditions",
        conditions=[CommitteeCondition(
            condition_id="", type="financial_covenant",
            metric_key="dscr", operator=">=",
            threshold=Decimal("1.25"), due_cadence="quarterly",
        )],
    )
    conditional_ok = (
        cp2.memo_status(ccf2, cfinal2.memo_id)
        == "committee_approved_conditional"
        and conditional.conditions[0].threshold_trace is not None
        and "min_dscr" in conditional.conditions[0].threshold_trace
    )
    print(f"committee_conditional_positive_path: "
          f"{'traced conditions stored' if conditional_ok else 'FAILED'}")
    if not conditional_ok:
        failures.append("the conditional approval path did not land")

    # File 3: defer (coded, with named requirements), re-present,
    # approve — two decisions on the one artefact.
    cp3, ccf3, cdraft3, cfinal3 = _committee_file()
    cp3.convene_committee(
        ccf3, cfinal3.memo_id, actor=C_OFFICER, actor_role=C_OFFICER_ROLE
    )
    _decide(
        cp3, ccf3, cfinal3.memo_id, outcome="defer",
        deferral_reasons=["awaiting_required_documents"],
        representment_requirements=["chk-03 (Business tax returns) on file"],
    )
    deferred_ok = cp3.memo_status(ccf3, cfinal3.memo_id) == "deferred"
    cp3.convene_committee(
        ccf3, cfinal3.memo_id, actor=C_OFFICER, actor_role=C_OFFICER_ROLE
    )
    _decide(cp3, ccf3, cfinal3.memo_id, outcome="approve")
    represent_ok = (
        deferred_ok
        and cp3.memo_status(ccf3, cfinal3.memo_id) == "committee_approved"
        and [d.outcome for d in cp3.list_committee_decisions(
            ccf3, cfinal3.memo_id
        )] == ["defer", "approve"]
    )
    print(f"committee_defer_represent_positive_path: "
          f"{'defer -> re-present -> approve landed' if represent_ok else 'FAILED'}")
    if not represent_ok:
        failures.append("the defer / re-present path did not land")

    committee_positive = decline_ok and conditional_ok and represent_ok
    print(f"committee_positive_paths: "
          f"{'approve/conditional/decline/defer-represent landed' if committee_positive else 'FAILED'}")
    if not committee_positive:
        failures.append("a committee positive path did not land")

    # ---------------------------------------------------------------
    # Round 8: document disagreements. The seed borrower again, now
    # with its FY2024 tax return and management accounts registered
    # beside the audited statements — the witnesses disagree, and
    # the round is graded on the disagreement surviving every
    # layer: detection, the spread's working figures, the
    # computation flags, and the memo.
    # ---------------------------------------------------------------
    r8 = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    r8_file = r8.open_application(
        borrower, facility,
        documents=docs + hartline_disagreement_documents(),
    )
    r8_spread = r8.get_spread(r8_file.credit_file_id)
    r8_records = list(r8_spread.disagreements)
    r8_material = [d for d in r8_records if d.material]
    detection_exact = (
        len(r8_records) == 7
        and {d.line_key for d in r8_material}
        == {"revenue", "cost_of_goods_sold", "gross_profit", "ebit",
            "net_income"}
        and {d.line_key for d in r8_records if not d.material}
        == {"inventory", "total_current_assets"}
        and all(d.fiscal_year == 2024 for d in r8_records)
    )
    print(f"disagreement_detection_exactness: {len(r8_records)} records "
          f"({len(r8_material)} material, "
          f"{len(r8_records) - len(r8_material)} immaterial) on the "
          f"seed borrower FY2024 "
          f"{'— exact' if detection_exact else '— MISMATCH'}")
    if not detection_exact:
        failures.append("disagreement detection diverges from the fixture")

    # Averaged or dropped: the working figure is the audited figure
    # itself, and every source's figure rides in the record.
    averaged_or_dropped = 0
    revenue_line = next(l for l in r8_spread.lines if l.key == "revenue")
    if revenue_line.values.get(2024) != Decimal("12374000"):
        averaged_or_dropped += 1
    revenue_record = next(d for d in r8_records if d.line_key == "revenue")
    reported_values = {s.value for s in revenue_record.sources}
    if reported_values != {
        Decimal("12374000"), Decimal("11905000"), Decimal("12410000")
    }:
        averaged_or_dropped += 1
    if revenue_line.values.get(2024) in reported_values and len(
        reported_values
    ) > 1 and revenue_line.values[2024] == sum(reported_values) / 3:
        averaged_or_dropped += 1
    print(f"disagreement_averaged_or_dropped: {averaged_or_dropped} "
          f"(required 0)")
    if averaged_or_dropped:
        failures.append("a disagreement was averaged or a source dropped")

    # Cross-type restatement: the tax return was registered AFTER
    # the audited FY2024 statements; it superseded nothing.
    cross_type_restatement = 0
    fy24_sources = {
        ps.source_type: ps for ps in r8_spread.period_sources
        if ps.fiscal_year == 2024
    }
    tax_doc_id = next(
        d.document_id for d in r8_file.documents
        if d.kind == DocumentKind.TAX_RETURN
    )
    audited_source = fy24_sources.get("audited_statement")
    if audited_source is None or audited_source.document_id == tax_doc_id:
        cross_type_restatement += 1
    if "tax_return" not in fy24_sources:
        cross_type_restatement += 1
    if audited_source is not None and (
        tax_doc_id in audited_source.superseded_document_ids
    ):
        cross_type_restatement += 1
    print(f"disagreement_cross_type_restatement_accepted: "
          f"{cross_type_restatement} (required 0)")
    if cross_type_restatement:
        failures.append("a cross-type difference was treated as a restatement")

    # Flagged figures: exactly the figures over materially disagreed
    # lines carry flags, and their memo traces name the disagreement.
    r8_pack = r8.compute_metrics(r8_file.credit_file_id)
    flagged_keys = {
        f.key for f in r8_pack.figures
        if f.fiscal_year == 2024 and f.status == "computed"
        and f.disagreement_ids
    }
    flagged_clean = 0
    if flagged_keys != {
        "gross_margin", "operating_margin",
        "return_on_assets", "return_on_equity",
    }:
        flagged_clean += 1
    r8_report = r8.evaluate_policy(
        r8_file.credit_file_id, pack_version="v1", as_of=date(2025, 6, 30)
    )
    r8_memo = r8.compose_memo(
        r8_file.credit_file_id, report_id=r8_report.report_id
    )
    r8_inputs = MemoInputs(
        credit_file=r8.store.get_credit_file(r8_file.credit_file_id),
        spread=r8_spread,
        pack=r8_pack,
        policy_report=r8_report,
    )
    for f in r8_pack.figures:
        if not (f.disagreement_ids and f.status == "computed"
                and f.value is not None):
            continue
        traces = [
            t for t in r8_memo.verification.figures
            if t.value == format(f.value, "f")
        ]
        if traces and not any(
            "document disagreement" in t.source for t in traces
        ):
            flagged_clean += 1
    print(f"disagreement_flagged_figure_clean: {flagged_clean} "
          f"(required 0)")
    if flagged_clean:
        failures.append("a flagged figure presented as clean")

    # Material omission: three tampered memos, each refused.
    omitted_accepted = 0
    section_dropped = r8_memo.model_copy(deep=True)
    section_dropped.sections = [
        s for s in section_dropped.sections
        if s.key != "document_disagreements"
    ]
    one_removed = r8_memo.model_copy(deep=True)
    removed_section = one_removed.section("document_disagreements")
    ebit_record = next(
        d for d in r8_material if d.line_key == "ebit"
    )
    removed_section.paragraphs = [
        p for p in removed_section.paragraphs
        if p != render_disagreement_paragraph(ebit_record)
    ]
    removed_section.disagreement_ids = [
        i for i in removed_section.disagreement_ids
        if i != ebit_record.disagreement_id
    ]
    reworded = r8_memo.model_copy(deep=True)
    reworded_section = reworded.section("document_disagreements")
    revenue_paragraph = render_disagreement_paragraph(revenue_record)
    reworded_section.paragraphs = [
        (
            "Revenue (fiscal 2024): the audited statements report "
            "$12,374,000.00, which the spread carries; the tax "
            "return reports a broadly similar figure and the "
            "difference is not significant."
        ) if p == revenue_paragraph else p
        for p in reworded_section.paragraphs
    ]
    for tampered in (section_dropped, one_removed, reworded):
        try:
            verify_memo(tampered, r8_inputs)
            omitted_accepted += 1
        except MemoVerificationError:
            pass
    print(f"disagreement_material_omitted_accepted: {omitted_accepted} "
          f"of 3 tamper cases accepted (required 0)")
    if omitted_accepted:
        failures.append("the verifier accepted a memo missing a "
                        "material disagreement")

    # The positive path, on the record: the memo's verification
    # lists exactly the material ids the spread records.
    memo_disagreement_ok = (
        sorted(r8_memo.verification.disagreements_expected)
        == sorted(r8_memo.verification.disagreements_listed)
        == sorted(d.disagreement_id for d in r8_material)
    )
    print(f"disagreement_memo_positive_path: "
          f"{'memo carries all 5 material disagreements' if memo_disagreement_ok else 'FAILED'}")
    if not memo_disagreement_ok:
        failures.append("the memo did not carry the material disagreements")

    # ---------------------------------------------------------------
    # Round 9: committee readiness (law 7). The seed borrower is
    # the blocked file (7 documents missing, DSCR not computable);
    # the Calloway factory above is the ready file; a CRE variant
    # carries the stale-appraisal path. Zero tolerance on the four
    # counters; the positive paths must land.
    # ---------------------------------------------------------------
    rp = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    r_file = rp.open_application(borrower, facility, documents=docs)
    rcf = r_file.credit_file_id
    r_memo, r_final = _officer_final_for(rp, rcf, as_of=date(2025, 6, 30))

    def _convene(pipe, cf, mid, **kw):
        kw.setdefault("actor", C_OFFICER)
        kw.setdefault("actor_role", C_OFFICER_ROLE)
        return pipe.convene_committee(cf, mid, **kw)

    over_blocker = 0
    # (a) The seed borrower: missing documents + uncomputable DSCR.
    over_blocker += _refused(lambda: _convene(
        rp, rcf, r_final.memo_id, as_of=date(2025, 6, 30)
    ))
    # (b) The ready file minus its tax returns.
    tp = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    t_file = tp.open_application(
        _ready_borrower(), _ready_facility(),
        documents=[
            d for d in _ready_documents()
            if d["kind"] != DocumentKind.TAX_RETURN.value
        ],
    )
    _, t_final = _officer_final_for(
        tp, t_file.credit_file_id, as_of=date(2026, 6, 30)
    )
    over_blocker += _refused(lambda: _convene(
        tp, t_file.credit_file_id, t_final.memo_id, as_of=date(2026, 6, 30)
    ))
    tax_message = ""
    try:
        _convene(
            tp, t_file.credit_file_id, t_final.memo_id,
            as_of=date(2026, 6, 30),
        )
    except ValidationError as exc:
        tax_message = str(exc)
    # (A third blocked convene — the CRE stale appraisal — is
    # probed on the positive path below, where its refresh lands.)
    print(f"readiness_convene_over_blocker_accepted: {over_blocker} "
          f"of 2 blocked convenes accepted (required 0)")
    if over_blocker:
        failures.append("a memo was convened over a readiness blocker")

    # An old, fresh report must not bless a later, stale convene.
    fp = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    f_file = fp.open_application(
        _ready_borrower(), _ready_facility(), documents=_ready_documents()
    )
    fcf = f_file.credit_file_id
    _, f_final = _officer_final_for(fp, fcf, as_of=date(2026, 6, 30))
    fresh_report = fp.compute_readiness(
        fcf, memo_id=f_final.memo_id, as_of=date(2026, 6, 30)
    )
    stale_blessed = 0
    if not fresh_report.ready:
        stale_blessed += 1  # the fixture itself failed to be ready
    stale_blessed += _refused(lambda: _convene(
        fp, fcf, f_final.memo_id, as_of=date(2028, 1, 31)
    ))
    print(f"readiness_stale_blessed_by_old_report: {stale_blessed} "
          f"(required 0)")
    if stale_blessed:
        failures.append("an old readiness report blessed a stale file")

    # Blockers clear only on refresh: recomputation changes
    # nothing, and no waiver surface exists.
    cleared_without_refresh = 0
    first = rp.compute_readiness(
        rcf, memo_id=r_final.memo_id, as_of=date(2025, 6, 30)
    )
    second = rp.compute_readiness(
        rcf, memo_id=r_final.memo_id, as_of=date(2025, 6, 30)
    )
    if {e.input_key for e in second.blockers} != {
        e.input_key for e in first.blockers
    } or not second.blockers:
        cleared_without_refresh += 1
    # (Round 11 note: waive_breach is the one waiv-named method —
    # it resolves a covenant breach on a committee decision's
    # authority and cannot touch a readiness blocker. Round 12
    # added _overrides_for, a private lookup; round 13 adds
    # override_rating, which moves a risk rating's final grade
    # under its own discipline, and list_rating_overrides, a
    # read-only listing. None of them can touch a readiness
    # blocker, so the scan names exactly those four.)
    if [
        name for name in dir(CreditPipeline)
        if "waiv" in name or "override" in name
    ] != [
        "_overrides_for", "list_rating_overrides",
        "override_rating", "waive_breach",
    ]:
        cleared_without_refresh += 1
    try:
        rp.convene_committee(
            rcf, r_final.memo_id, actor=C_OFFICER,
            actor_role=C_OFFICER_ROLE, waive_blockers=True,
        )
        cleared_without_refresh += 1  # a waiver parameter was accepted
    except TypeError:
        pass
    except ValidationError:
        cleared_without_refresh += 1  # accepted as a kwarg, then refused
    print(f"readiness_blocker_cleared_without_refresh: "
          f"{cleared_without_refresh} (required 0)")
    if cleared_without_refresh:
        failures.append("a readiness blocker cleared without a refresh")

    # Every blocker named in the refusal.
    refusal_text = ""
    try:
        _convene(rp, rcf, r_final.memo_id, as_of=date(2025, 6, 30))
    except ValidationError as exc:
        refusal_text = str(exc)
    unnamed = sum(
        1 for e in first.blockers
        if e.input_key not in refusal_text or e.label not in refusal_text
    )
    print(f"readiness_blocker_unnamed_in_refusal: {unnamed} of "
          f"{len(first.blockers)} blockers unnamed (required 0)")
    if unnamed:
        failures.append("a readiness blocker was not named in the refusal")

    # Positive paths: fresh convene lands; the approval record
    # carries the stamp; stale appraisal blocks, refresh clears,
    # convene lands; missing tax returns block by name.
    convened = _convene(fp, fcf, f_final.memo_id, as_of=date(2026, 6, 30))
    fresh_convene_ok = fp.memo_status(fcf, f_final.memo_id) == "in_committee"
    approval_event = next(
        (e for e in rp.store.list_memo_review_events(rcf, r_memo.memo_id)
         if e.action.startswith("approved")),
        None,
    )
    stamp_report = (
        rp.store.get_readiness_report(approval_event.readiness_report_id)
        if approval_event is not None
        and approval_event.readiness_report_id is not None
        else None
    )
    stamp_ok = (
        approval_event is not None
        and approval_event.readiness_blocker_count is not None
        and approval_event.readiness_blocker_count > 0
        and stamp_report is not None
        and stamp_report.blocker_count
        == approval_event.readiness_blocker_count
    )
    tax_named_ok = (
        "[chk-03] Business tax returns" in tax_message
        and "[chk-04] Guarantor personal tax returns" in tax_message
    )

    cre = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    cre_docs = _ready_documents() + [
        {
            "kind": kind.value, "filename": filename,
            "data_hex": json.dumps({}).encode().hex(),
        }
        for kind, filename in (
            (DocumentKind.APPRAISAL, "appraisal-2025.json"),
            (DocumentKind.RENT_ROLL, "rent-roll.json"),
            (DocumentKind.LEASES, "leases.json"),
            (DocumentKind.ENVIRONMENTAL_REPORT, "environmental.json"),
        )
    ]
    cre_file = cre.open_application(
        Borrower(
            legal_name="Harborpoint Retail Center LLC",
            ein_last4="9021", segment="real_estate",
            state_of_formation="NC", years_in_business=9,
            primary_contact_name="Marcus Yuen",
            primary_contact_email="marcus.yuen@example.com",
        ),
        FacilityRequest(
            facility_type=FacilityType.CRE_LOAN,
            amount=Decimal("4000000"),
            purpose="Refinance of a stabilized retail center",
            term_months=84, guarantors=[],
        ),
        documents=cre_docs,
    )
    cre_cf = cre_file.credit_file_id
    cre_stored = cre.store.get_credit_file(cre_cf)
    cre_appraisal = next(
        d for d in cre_stored.documents if d.kind == DocumentKind.APPRAISAL
    )
    cre_appraisal.received_at = datetime(2025, 1, 15, tzinfo=timezone.utc)
    cre.store.save_credit_file(cre_stored)
    _, cre_final = _officer_final_for(cre, cre_cf, as_of=date(2026, 6, 30))
    cre_blocked = _refused(lambda: _convene(
        cre, cre_cf, cre_final.memo_id, as_of=date(2026, 6, 30)
    )) == 0
    cre_stored = cre.store.get_credit_file(cre_cf)
    new_appraisal = register_document(
        cre.store, cre.documents, cre_stored,
        kind=DocumentKind.APPRAISAL, filename="appraisal-2026.json",
        data=json.dumps({}).encode(),
    )
    cre_stored = cre.store.get_credit_file(cre_cf)
    cre_stored.document(new_appraisal.document_id).received_at = datetime(
        2026, 6, 1, tzinfo=timezone.utc
    )
    cre.store.save_credit_file(cre_stored)
    _convene(cre, cre_cf, cre_final.memo_id, as_of=date(2026, 6, 30))
    cre_path_ok = (
        cre_blocked
        and cre.memo_status(cre_cf, cre_final.memo_id) == "in_committee"
    )
    readiness_positive = (
        fresh_convene_ok and stamp_ok and tax_named_ok and cre_path_ok
    )
    print(f"readiness_positive_paths: "
          f"{'fresh convene, approval stamp, named tax block, stale-appraisal refresh all landed' if readiness_positive else 'FAILED'}")
    if not readiness_positive:
        failures.append("a readiness positive path did not land")

    # ---------------------------------------------------------------
    # Round 10: covenants (law 6). The graded date table runs the
    # pure schedule arithmetic; the end-to-end path takes the
    # committee-ready file through a conditional approval, a
    # recorded closing, the schedule, the queue, the tick and an
    # amendment. Zero tolerance on all four counters.
    # ---------------------------------------------------------------
    date_table = [
        (add_months(date(2026, 1, 31), 1), date(2026, 2, 28)),
        (add_months(date(2024, 1, 31), 1), date(2024, 2, 29)),
        (add_months(date(2024, 2, 29), 12), date(2025, 2, 28)),
        (fiscal_year_end(2024, month=2, day=29), date(2024, 2, 29)),
        (fiscal_year_end(2025, month=2, day=29), date(2025, 2, 28)),
        (
            covenant_test_dates(
                date(2026, 1, 15), frequency="annual",
                basis="fiscal_period_end", term_months=36,
            ),
            [(date(2026, 12, 31), 2026), (date(2027, 12, 31), 2027),
             (date(2028, 12, 31), 2028)],
        ),
        (
            covenant_test_dates(
                date(2026, 1, 15), frequency="quarterly",
                basis="fiscal_period_end", term_months=12,
            ),
            [(date(2026, 3, 31), 2026), (date(2026, 6, 30), 2026),
             (date(2026, 9, 30), 2026), (date(2026, 12, 31), 2026)],
        ),
        (
            covenant_test_dates(
                date(2026, 1, 15), frequency="annual",
                basis="fiscal_period_end", term_months=36,
                fiscal_month=6, fiscal_day=30,
            ),
            [(date(2026, 6, 30), 2026), (date(2027, 6, 30), 2027),
             (date(2028, 6, 30), 2028)],
        ),
        (
            covenant_test_dates(
                date(2024, 2, 29), frequency="annual",
                basis="point_in_time", term_months=12,
            ),
            [(date(2024, 12, 31), 2024), (date(2025, 2, 28), None)],
        ),
        (
            covenant_test_dates(
                date(2026, 7, 1), frequency="semiannual",
                basis="fiscal_period_end", term_months=24,
            ),
            [(date(2026, 12, 31), 2026), (date(2027, 6, 30), 2027),
             (date(2027, 12, 31), 2027), (date(2028, 6, 30), 2028)],
        ),
        (
            deliverable_due_dates(
                date(2026, 7, 1), frequency="annual", term_months=60,
            ),
            [(date(2027, 3, 31), 2026), (date(2028, 3, 30), 2027),
             (date(2029, 3, 31), 2028), (date(2030, 3, 31), 2029),
             (date(2031, 3, 31), 2030)],
        ),
    ]
    schedule_date_wrong = sum(
        1 for got, want in date_table if got != want
    )

    kp = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    k_file = kp.open_application(
        _ready_borrower(), _ready_facility(),
        documents=_ready_documents(),
    )
    kcf = k_file.credit_file_id
    _, k_final = _officer_final_for(kp, kcf, as_of=date(2026, 6, 30))
    kp.convene_committee(
        kcf, k_final.memo_id, actor=C_OFFICER,
        actor_role=C_OFFICER_ROLE, as_of=date(2026, 6, 30),
    )
    kp.record_closing(
        kcf, closing_date=date(2026, 7, 1), actor=C_OFFICER,
        actor_role=C_OFFICER_ROLE,
    )
    k_decision = kp.decide_committee(
        kcf, k_final.memo_id,
        recorder_id=C_CHAIR, recorder_role="committee_chair",
        members=list(C_MEMBERS), outcome="approve_with_conditions",
        conditions=[
            CommitteeCondition(
                condition_id="", type="financial_covenant",
                metric_key="dscr", operator=">=",
                threshold=Decimal("1.25"), due_cadence="annual",
            ),
            CommitteeCondition(
                condition_id="", type="reporting_requirement",
                report_what="Annual audited financial statements",
                due_cadence="annual",
            ),
        ],
    )
    k_sets = kp.list_covenant_sets(kcf)
    set_created_ok = (
        len(k_sets) == 1
        and k_sets[0].decision_id == k_decision.decision_id
        and k_sets[0].policy_version == "v1"
        and k_sets[0].state == "active"
    )
    k_set = k_sets[0]
    k_instances = kp.covenant_schedule(
        kcf, as_of=date(2026, 8, 1)
    )["sets"][0]["instances"]
    k_dscr_dates = [
        i.test_date for i in k_instances if i.metric_key == "dscr"
    ]
    if k_dscr_dates != [date(y, 12, 31) for y in range(2026, 2031)]:
        schedule_date_wrong += 1
    print(f"covenant_schedule_date_wrong: {schedule_date_wrong} of "
          f"{len(date_table) + 1} graded cases (required 0)")
    if schedule_date_wrong:
        failures.append("a covenant schedule date was wrong")

    # The queue: the FY2026 DSCR test stands awaiting its inputs
    # at 2026-12-15, and the queue must list it, inputs named.
    k_dec_instances = kp.covenant_schedule(
        kcf, as_of=date(2026, 12, 15)
    )["sets"][0]["instances"]
    k_dscr_2026 = next(
        i for i in k_dec_instances
        if i.metric_key == "dscr" and i.test_date == date(2026, 12, 31)
    )
    k_queue = kp.covenant_due_work(as_of=date(2026, 12, 15))
    k_queue_entry = next(
        (e for e in k_queue
         if e["instance"].instance_id == k_dscr_2026.instance_id),
        None,
    )
    due_missing = 0 if (
        k_dscr_2026.state == "awaiting_inputs"
        and k_queue_entry is not None
        and k_queue_entry["borrower"]
        == "Calloway Precision Manufacturing, Inc."
        and any(
            "FY2026" in name
            for name in k_queue_entry["instance"].required_inputs
        )
    ) else 1
    print(f"covenant_due_missing_from_queue: {due_missing} "
          f"(required 0)")
    if due_missing:
        failures.append("a due covenant test was missing from the queue")

    # The tick: crossings recorded once, however often it runs.
    first_tick = kp.tick_covenants(as_of=date(2027, 1, 15))
    second_tick = kp.tick_covenants(as_of=date(2027, 1, 15))
    tick_duplication = len(second_tick["crossings_recorded"])
    print(f"covenant_tick_duplication: {tick_duplication} "
          f"(required 0); first tick recorded "
          f"{len(first_tick['crossings_recorded'])} crossings")
    if tick_duplication:
        failures.append("the covenant tick duplicated a crossing")

    # The amendment: the stored set never moves; the derived
    # current covenant does — version 2 at the new threshold.
    k_original = next(
        c for c in k_set.covenants if c.metric_key == "dscr"
    )
    stored_before = kp.get_covenant_set(
        kcf, k_set.set_id
    ).model_dump_json()
    kp.amend_covenant(
        kcf, k_original.covenant_id,
        author_id=C_CHAIR, author_role="committee_chair",
        reason_code="committee_redecision",
        decision_id=k_decision.decision_id,
        threshold=Decimal("1.30"),
    )
    stored_after = kp.get_covenant_set(
        kcf, k_set.set_id
    ).model_dump_json()
    threshold_mutated = 0 if stored_before == stored_after else 1
    print(f"covenant_threshold_mutated_in_place: {threshold_mutated} "
          f"(required 0)")
    if threshold_mutated:
        failures.append("a covenant threshold was mutated in place")
    k_current = kp.covenant_schedule(
        kcf, as_of=date(2026, 8, 1)
    )["sets"][0]["current_covenants"]
    amendment_landed = any(
        c.metric_key == "dscr" and c.threshold == Decimal("1.30")
        and c.version == 2 for c in k_current
    )
    covenant_positive = (
        set_created_ok and due_missing == 0 and amendment_landed
        and len(first_tick["crossings_recorded"]) == 2
    )
    print(f"covenant_positive_paths: "
          f"{'set from conditional approval, schedule computed, tick marks due, queue lists with inputs named, amendment lands as v2' if covenant_positive else 'FAILED'}")
    if not covenant_positive:
        failures.append("a covenant positive path did not land")

    # ---------------------------------------------------------------
    # Round 11: covenant test execution + breach cases (law 6,
    # the math half). A fresh approved Calloway file — its DSCR
    # covenant stands at the un-amended 1.25 — receives hand-built
    # FY2026 statements that fail (DSCR 0.9143, leverage 4.4348,
    # current ratio 1.7310 passes), then FY2027 statements that
    # cure (DSCR 1.5000). A second file's period is restated. A
    # third file's statements arrive without the cash-flow page.
    # Zero tolerance on every counter.
    # ---------------------------------------------------------------
    _T_FY2026 = dict(zip(_BS, (
        260000, 950000, 1300000, 2510000, 3740000, 6250000,
        850000, 260000, 1450000, 3650000, 5100000, 1150000,
    )), **dict(zip(_IS, (
        8600000, 6020000, 2580000, 2380000, 200000, 110000, 60000,
    ))), **dict(zip(_CF, (
        120000, -50000, -100000, 50000, 80000, 260000, -260000,
        270000, -200000, 70000, -110000,
    ))))
    _T_FY2027 = dict(zip(_BS, (
        300000, 1000000, 1400000, 2700000, 3900000, 6600000,
        900000, 280000, 1500000, 3600000, 5100000, 1500000,
    )), **dict(zip(_IS, (
        9800000, 6860000, 2940000, 2540000, 400000, 100000, 210000,
    ))), **dict(zip(_CF, (
        140000, -50000, -100000, 50000, 450000, 300000, -300000,
        100000, -210000, -110000, 40000,
    ))))
    _T_FY2026_RESTATED = dict(
        _T_FY2026, ebit=320000, operating_expenses=2260000,
        net_income=180000,
    )

    def _t_stmt_doc(year, figures, with_cf=True):
        def page(number, title, fields):
            return {
                "page": number, "title": title,
                "lines": [
                    {"line": i, "label": _LABELS[f], "value": figures[f]}
                    for i, f in enumerate(fields, start=1)
                ],
            }

        pages = [
            page(1, "Balance Sheet", _BS),
            page(2, "Income Statement", _IS),
        ]
        if with_cf:
            pages.append(page(3, "Cash Flow Statement", _CF))
        return json.dumps({
            "document_type": "financial_statement",
            "borrower": "Calloway Precision Manufacturing, Inc.",
            "fiscal_year": year, "period_label": f"FY{year}",
            "basis": "audited", "pages": pages,
        }).encode()

    def _t_deliver(pipe, cf, year, figures, with_cf=True):
        stored = pipe.store.get_credit_file(cf)
        register_document(
            pipe.store, pipe.documents, stored,
            kind=DocumentKind.FINANCIAL_STATEMENT,
            filename=f"calloway-fy{year}-statements.json",
            data=_t_stmt_doc(year, figures, with_cf),
            period_label=f"FY{year}",
        )
        pipe.run_extraction(cf)

    def _t_approved(pipe):
        t_file = pipe.open_application(
            _ready_borrower(), _ready_facility(),
            documents=_ready_documents(),
        )
        tcf = t_file.credit_file_id
        _, t_final = _officer_final_for(pipe, tcf, as_of=date(2026, 6, 30))
        pipe.convene_committee(
            tcf, t_final.memo_id, actor=C_OFFICER,
            actor_role=C_OFFICER_ROLE, as_of=date(2026, 6, 30),
        )
        pipe.record_closing(
            tcf, closing_date=date(2026, 7, 1), actor=C_OFFICER,
            actor_role=C_OFFICER_ROLE,
        )
        t_decision = pipe.decide_committee(
            tcf, t_final.memo_id,
            recorder_id=C_CHAIR, recorder_role="committee_chair",
            members=list(C_MEMBERS), outcome="approve_with_conditions",
            conditions=[
                CommitteeCondition(
                    condition_id="", type="financial_covenant",
                    metric_key="dscr", operator=">=",
                    threshold=Decimal("1.25"), due_cadence="annual",
                ),
                CommitteeCondition(
                    condition_id="", type="reporting_requirement",
                    report_what="Annual audited financial statements",
                    due_cadence="annual",
                ),
            ],
        )
        return tcf, t_decision

    def _t_results(pipe, cf, metric_key):
        return [
            v for v in pipe.covenant_test_results(cf)
            if v["metric_key"] == metric_key
        ]

    # The graded figures: every recorded covenant figure on the
    # fail year must equal the hand computation exactly.
    vp = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    vcf, v_decision = _t_approved(vp)
    _t_deliver(vp, vcf, 2026, _T_FY2026)
    vp.run_due_covenant_tests(vcf, as_of=date(2027, 8, 1))
    _expected_figures = {
        ("dscr", 2026): Decimal("0.9143"),
        ("debt_to_tangible_net_worth", 2026): Decimal("4.4348"),
        ("current_ratio", 2026): Decimal("1.7310"),
        ("current_ratio", 2025): Decimal("1.7143"),
    }
    math_wrong = 0
    for (metric, year), want in _expected_figures.items():
        got = [
            v for v in _t_results(vp, vcf, metric)
            if v["fiscal_year"] == year and v["figure_value"] is not None
        ]
        if not got or any(
            Decimal(str(v["figure_value"])) != want for v in got
        ):
            math_wrong += 1
    print(f"covenant_math_wrong: {math_wrong} of "
          f"{len(_expected_figures)} graded figures (required 0)")
    if math_wrong:
        failures.append("a covenant test figure was wrong")

    # Every fail has its breach; every breach traces to a fail.
    v_results = vp.covenant_test_results(vcf)
    v_breaches = vp.list_breach_cases(
        as_of=date(2027, 8, 1), credit_file_id=vcf
    )
    fail_ids = {
        v["result_id"] for v in v_results if v["outcome"] == "fail"
    }
    breach_result_ids = {b["test_result_id"] for b in v_breaches}
    fail_without_breach = len(fail_ids - breach_result_ids)
    print(f"covenant_fail_without_breach: {fail_without_breach} "
          f"(required 0); {len(fail_ids)} fails, "
          f"{len(breach_result_ids)} breaches")
    if fail_without_breach:
        failures.append("a covenant failure opened no breach case")

    # A not_evaluable result never reads as a pass: the third
    # file's FY2026 statements arrive without the cash-flow page.
    np_ = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    ncf, _nd = _t_approved(np_)
    _t_deliver(np_, ncf, 2026, _T_FY2026, with_cf=False)
    np_.run_due_covenant_tests(ncf, as_of=date(2027, 8, 1))
    ne_results = [
        v for v in _t_results(np_, ncf, "dscr")
        if v["fiscal_year"] == 2026
    ]
    not_eval_as_pass = 0
    if (
        len(ne_results) != 1
        or ne_results[0]["outcome"] != "not_evaluable"
        or ne_results[0]["figure_value"] is not None
        or not any(
            "depreciation_and_amortization" in name
            for name in ne_results[0]["missing_inputs"]
        )
    ):
        not_eval_as_pass += 1
    if any(
        b["test_result_id"]
        in {v["result_id"] for v in ne_results}
        for b in np_.list_breach_cases(
            as_of=date(2027, 8, 1), credit_file_id=ncf
        )
    ):
        not_eval_as_pass += 1
    print(f"covenant_not_evaluable_read_as_pass: {not_eval_as_pass} "
          f"(required 0)")
    if not_eval_as_pass:
        failures.append("a not_evaluable test read as a pass")

    # Waivers without a committee reference are refused — an
    # invented id, a decline's id, and (via the state machine)
    # any act on a terminal breach.
    dscr_breach = next(
        b for b in v_breaches if b["metric_key"] == "dscr"
    )
    # (_refused returns 1 when a probe was ACCEPTED.)
    waiver_accepted = 0
    waiver_accepted += _refused(lambda: vp.waive_breach(
        vcf, dscr_breach["breach_id"], actor_id=C_OFFICER,
        actor_role=C_OFFICER_ROLE, decision_id="",
    ))
    waiver_accepted += _refused(lambda: vp.waive_breach(
        vcf, dscr_breach["breach_id"], actor_id=C_OFFICER,
        actor_role=C_OFFICER_ROLE,
        decision_id="decision-that-does-not-exist",
    ))
    dp = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    d_file = dp.open_application(
        _ready_borrower(), _ready_facility(),
        documents=_ready_documents(),
    )
    dcf2 = d_file.credit_file_id
    _, d_final = _officer_final_for(dp, dcf2, as_of=date(2026, 6, 30))
    dp.convene_committee(
        dcf2, d_final.memo_id, actor=C_OFFICER,
        actor_role=C_OFFICER_ROLE, as_of=date(2026, 6, 30),
    )
    d_decline = dp.decide_committee(
        dcf2, d_final.memo_id,
        recorder_id=C_CHAIR, recorder_role="committee_chair",
        members=list(C_MEMBERS), outcome="decline",
        adverse_reasons=[
            AdverseActionReason(
                code="leverage_above_policy_limit",
                tie_kind="policy_test",
                test_id="max_leverage_by_segment",
            ),
            AdverseActionReason(
                code="financial_information_insufficient",
                tie_kind="policy_test",
                test_id="min_global_dscr",
            ),
        ],
    )
    waiver_accepted += _refused(lambda: vp.waive_breach(
        vcf, dscr_breach["breach_id"], actor_id=C_OFFICER,
        actor_role=C_OFFICER_ROLE, decision_id=d_decline.decision_id,
    ))
    print(f"covenant_waiver_without_reference_accepted: "
          f"{waiver_accepted} of 3 probes (required 0)")
    if waiver_accepted:
        failures.append("a breach waiver without authority was accepted")

    # No silent close: an open breach refuses the default path
    # directly, refuses a cure by its own failing result, and a
    # terminal breach refuses every further act.
    silently_closed = 0
    silently_closed += _refused(lambda: vp.default_breach(
        vcf, dscr_breach["breach_id"], actor_id=C_OFFICER,
        actor_role=C_OFFICER_ROLE,
        reason_code="cure_period_expired_uncured",
    ))
    silently_closed += _refused(lambda: vp.cure_breach(
        vcf, dscr_breach["breach_id"], actor_id=C_OFFICER,
        actor_role=C_OFFICER_ROLE,
        cure_result_id=dscr_breach["test_result_id"],
    ))
    print(f"covenant_breach_silently_closed: "
          f"{silently_closed} of 2 probes accepted (required 0)")
    if silently_closed:
        failures.append("a breach was silently closed")

    # Positive paths: the current-ratio pass stands clean; the
    # DSCR breach is cured by the FY2027 pass; a leverage breach
    # is waived on the committee's decision; the restated file
    # flags its original result and the re-test passes.
    cr_pass = next(
        v for v in _t_results(vp, vcf, "current_ratio")
        if v["outcome"] == "pass"
    )
    _t_deliver(vp, vcf, 2027, _T_FY2027)
    vp.run_due_covenant_tests(vcf, as_of=date(2028, 8, 1))
    cure = next(
        v for v in _t_results(vp, vcf, "dscr")
        if v["fiscal_year"] == 2027 and v["outcome"] == "pass"
    )
    cured_ok = False
    try:
        cured = vp.cure_breach(
            vcf, dscr_breach["breach_id"], actor_id=C_OFFICER,
            actor_role=C_OFFICER_ROLE,
            cure_result_id=cure["result_id"],
        )
        cured_ok = cured.state == "cured"
    except ValidationError:
        cured_ok = False
    terminal_refused = 1 - _refused(lambda: vp.escalate_breach(
        vcf, dscr_breach["breach_id"], actor_id=C_OFFICER,
        actor_role=C_OFFICER_ROLE, reason_code="repeat_breach",
    ))
    lev_breach = next(
        b for b in vp.list_breach_cases(
            as_of=date(2027, 8, 1), credit_file_id=vcf
        )
        if b["metric_key"] == "debt_to_tangible_net_worth"
        and b["state"] == "open"
    )
    waived_ok = False
    try:
        waived = vp.waive_breach(
            vcf, lev_breach["breach_id"], actor_id=C_CHAIR,
            actor_role="committee_chair",
            decision_id=v_decision.decision_id,
        )
        waived_ok = waived.state == "waived"
    except ValidationError:
        waived_ok = False

    rp2 = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    rcf2, _rd2 = _t_approved(rp2)
    _t_deliver(rp2, rcf2, 2026, _T_FY2026)
    rp2.run_due_covenant_tests(rcf2, as_of=date(2027, 8, 1))
    original = next(
        v for v in _t_results(rp2, rcf2, "dscr")
        if v["outcome"] == "fail"
    )
    _t_deliver(rp2, rcf2, 2026, _T_FY2026_RESTATED)
    flagged = next(
        v for v in _t_results(rp2, rcf2, "dscr")
        if v["result_id"] == original["result_id"]
    )
    rp2.run_due_covenant_tests(rcf2, as_of=date(2027, 8, 3))
    retest = next(
        v for v in _t_results(rp2, rcf2, "dscr")
        if v["supersedes_result_id"] == original["result_id"]
    )
    restatement_ok = (
        flagged["standing"] == "superseded_by_restatement"
        and retest["outcome"] == "pass"
        and Decimal(str(retest["figure_value"])) == Decimal("1.2571")
    )
    testing_positive = (
        cr_pass is not None and cured_ok and terminal_refused == 1
        and waived_ok and restatement_ok
    )
    print(f"covenant_testing_positive_paths: "
          f"{'pass recorded clean, fail → breach → cured by FY2027 pass, breach waived on the committee decision, restatement flags + re-test passes' if testing_positive else 'FAILED'}")
    if not testing_positive:
        failures.append("a covenant testing positive path did not land")

    # ---------------------------------------------------------------
    # Round 12: annual review packs. The obligation derives from
    # the covenant set's anchor — the graded tables include a
    # leap-day closing (Feb 28 in common years, Feb 29 in leap
    # years). The pack assembles from stored artefacts under both
    # policy versions, differences named; its summary is verified
    # — an invented figure, a dropped locked fact, or a breach
    # denied in prose is refused; a stale pack is produced
    # incomplete, never blessed complete; the stance is a coded
    # act of a named officer, and the decided memo's approver may
    # not review alone. Zero tolerance on every counter.
    # ---------------------------------------------------------------
    from commercial_credit.covenants import (
        maturity_date as _maturity_date,
    )
    from commercial_credit.reviews import (
        review_due_dates as _review_due_dates,
    )

    date_wrong = 0
    _leap_anchor = date(2024, 2, 29)
    if _review_due_dates(
        _leap_anchor, maturity=_maturity_date(_leap_anchor, 60)
    ) != [
        date(2025, 2, 28), date(2026, 2, 28), date(2027, 2, 28),
        date(2028, 2, 29), date(2029, 2, 28),
    ]:
        date_wrong += 1
    rp = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    rcf, _r_decision = _t_approved(rp)
    r_instances = rp.annual_review_schedule(
        rcf, as_of=date(2027, 6, 15)
    )["sets"][0]["instances"]
    if [i.due_date for i in r_instances] != [
        date(2027, 7, 1), date(2028, 7, 1), date(2029, 7, 1),
        date(2030, 7, 1), date(2031, 7, 1),
    ] or r_instances[0].state != "due_soon":
        date_wrong += 1
    print(f"review_schedule_date_wrong: {date_wrong} of 2 graded "
          f"tables (required 0)")
    if date_wrong:
        failures.append("an annual review date was wrong")

    # The breach-year pack: FY2026 delivered, its tests run, the
    # pack assembled at 2027-03-01 — complete, carrying the
    # period's failures and breaches in its locked record.
    _t_deliver(rp, rcf, 2026, _T_FY2026)
    rp.run_due_covenant_tests(rcf, as_of=date(2027, 3, 1))
    r_pack = rp.assemble_review_pack(
        rcf, review_number=1, as_of=date(2027, 3, 1)
    )
    # The stale pack: a second approved file, no new statements —
    # produced at its due date incomplete, naming its blockers,
    # with the v1→v2 differences on the record.
    sp = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    scf, _s_decision = _t_approved(sp)
    s_pack = sp.assemble_review_pack(
        scf, review_number=1, as_of=date(2027, 7, 1)
    )
    stale_marked = 0
    if s_pack.complete or not s_pack.blockers:
        stale_marked += 1
    if not any(
        p.startswith("INCOMPLETE PACK")
        for p in s_pack.summary.locked_facts
    ):
        stale_marked += 1
    if r_pack.complete is not True or r_pack.blockers:
        stale_marked += 1
    print(f"review_stale_pack_marked_complete: {stale_marked} "
          f"(required 0)")
    if stale_marked:
        failures.append("a stale review pack was marked complete")

    # Tamper probes on the verified summary: an invented figure
    # and a dropped locked fact are both refused.
    untraceable_accepted = 0
    untraceable_accepted += _refused(lambda: rp.check_review_summary(
        rcf, r_pack.pack_id,
        paragraphs=[
            *r_pack.summary.paragraphs,
            "The exposure now totals $999,999.99.",
        ],
        locked_facts=r_pack.summary.locked_facts,
    ))
    _softened_locked = [
        p for p in r_pack.summary.locked_facts
        if not p.startswith("FAILED COVENANT TEST")
    ]
    untraceable_accepted += _refused(lambda: rp.check_review_summary(
        rcf, r_pack.pack_id,
        paragraphs=r_pack.summary.paragraphs,
        locked_facts=_softened_locked,
    ))
    print(f"review_pack_figure_untraceable: "
          f"{untraceable_accepted} of 2 probes accepted (required 0)")
    if untraceable_accepted:
        failures.append("a tampered review summary was accepted")

    # A breach denied in prose is a softened summary — refused;
    # and the locked record itself must state the failure.
    softened = _refused(lambda: rp.check_review_summary(
        rcf, r_pack.pack_id,
        paragraphs=[
            *r_pack.summary.paragraphs,
            "There were no breaches in the period.",
        ],
        locked_facts=r_pack.summary.locked_facts,
    ))
    if not any(
        p.startswith("FAILED COVENANT TEST")
        for p in r_pack.summary.locked_facts
    ):
        softened += 1
    print(f"review_summary_breach_softened: {softened} (required 0)")
    if softened:
        failures.append("a review summary softened a breach")

    # The stance's discipline: anonymous, unroled, uncoded, and
    # the memo's own approver acting alone are all refused.
    closed_unlawful = 0
    closed_unlawful += _refused(lambda: rp.record_review_outcome(
        rcf, review_number=1, stance="affirm",
        reason_codes=["performance_in_line"],
        reviewer_id="", reviewer_role=C_OFFICER_ROLE,
        pack_id=r_pack.pack_id,
    ))
    closed_unlawful += _refused(lambda: rp.record_review_outcome(
        rcf, review_number=1, stance="affirm",
        reason_codes=["performance_in_line"],
        reviewer_id="rm-eval", reviewer_role="relationship_manager",
        pack_id=r_pack.pack_id,
    ))
    closed_unlawful += _refused(lambda: rp.record_review_outcome(
        rcf, review_number=1, stance="affirm",
        reason_codes=[],
        reviewer_id=C_OFFICER, reviewer_role=C_OFFICER_ROLE,
        pack_id=r_pack.pack_id,
    ))
    closed_unlawful += _refused(lambda: rp.record_review_outcome(
        rcf, review_number=1, stance="affirm",
        reason_codes=["performance_in_line"],
        reviewer_id=C_OFFICER, reviewer_role=C_OFFICER_ROLE,
        pack_id=r_pack.pack_id,
    ))
    print(f"review_closed_without_officer_or_stance: "
          f"{closed_unlawful} of 4 probes accepted (required 0)")
    if closed_unlawful:
        failures.append("a review closed without a lawful stance")

    # Positive paths: the approver affirms with a countersigning
    # officer and the instance completes (the next derives,
    # unmoved); a watchlist stance lands on the stale file; an
    # escalation records its committee referral; the stale pack
    # names the v1 pass that v2 fails.
    affirm = rp.record_review_outcome(
        rcf, review_number=1, stance="affirm",
        reason_codes=["covenant_breach_in_period"],
        reviewer_id=C_OFFICER, reviewer_role=C_OFFICER_ROLE,
        countersigner_id="officer-eval-two",
        countersigner_role="senior_credit_officer",
        pack_id=r_pack.pack_id,
    )
    r_after = rp.annual_review_schedule(
        rcf, as_of=date(2027, 3, 2)
    )["sets"][0]["instances"]
    affirm_ok = (
        r_after[0].state == "completed"
        and r_after[0].outcome_id == affirm.outcome_id
        and r_after[1].period_start == date(2027, 7, 1)
        and r_after[1].due_date == date(2028, 7, 1)
    )
    watch = sp.record_review_outcome(
        scf, review_number=1, stance="place_on_watchlist",
        reason_codes=["incomplete_inputs"],
        reviewer_id="officer-eval-two",
        reviewer_role="senior_credit_officer",
        pack_id=s_pack.pack_id,
    )
    ep = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    ecf, _e_decision = _t_approved(ep)
    e_pack = ep.assemble_review_pack(
        ecf, review_number=1, as_of=date(2027, 7, 1)
    )
    esc = ep.record_review_outcome(
        ecf, review_number=1, stance="escalate_to_committee",
        reason_codes=["policy_exception_open"],
        reviewer_id="officer-eval-two",
        reviewer_role="senior_credit_officer",
        pack_id=e_pack.pack_id,
    )
    e_referrals = ep.list_committee_referrals(ecf)
    referral_ok = (
        esc.referral_id is not None
        and len(e_referrals) == 1
        and e_referrals[0].referral_id == esc.referral_id
        and e_referrals[0].pack_id == e_pack.pack_id
    )
    s_differences = {
        d.test_id: (d.pinned_outcome, d.current_outcome)
        for d in s_pack.policy_differences
    }
    review_positive = (
        affirm_ok
        and watch.stance == "place_on_watchlist"
        and referral_ok
        and s_differences.get("min_dscr") == ("pass", "fail")
        and r_pack.summary.verification.verified
    )
    print(f"review_positive_paths: "
          f"{'pack assembles complete over the breach year, affirm lands countersigned and the instance completes, watchlist lands, escalation records its referral, v1-to-v2 differences named' if review_positive else 'FAILED'}")
    if not review_positive:
        failures.append("an annual review positive path did not land")

    # ---------------------------------------------------------------
    # Round 13: the risk-rating scorecard. A coded stand-in for
    # the bank's own rating model — it grades, it never decides.
    # The graded fixture is hand-computed factor by factor; a
    # missing figure lands in its named band, never at zero; the
    # override is bounded, coded and countersigned; history is
    # append-only across scorecard versions. Zero tolerance.
    # ---------------------------------------------------------------
    from commercial_credit import committee as _committee_mod
    from commercial_credit import officer_gate as _officer_mod
    from commercial_credit.scorecard import (
        get_scorecard as _get_scorecard,
        register_scorecard as _register_scorecard,
    )

    kp = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    kcf, _k_decision = _t_approved(kp)
    k_assignment = kp.assign_rating(kcf, as_of=date(2026, 6, 30))
    k_factors = {
        f.factor_id: f for f in k_assignment.factors
    }
    _graded = {
        # factor: (input value, band, score, weighted contribution)
        "leverage": ("5.0000", "leverage_at_most_5_00", 8, "1.60"),
        "dscr": ("1.3125", "dscr_at_least_1_25", 5, "1.25"),
        "liquidity": (
            "1.7143", "current_ratio_at_least_1_50", 4, "0.60"
        ),
        "profitability_trend": (
            "-0.0040", "margin_trend_at_least_-0_0050", 5, "0.50"
        ),
    }
    factor_wrong = 0
    for _fid, (_val, _band, _score, _contrib) in _graded.items():
        _f = k_factors[_fid]
        if (
            str(_f.input_value) != _val or _f.band_id != _band
            or _f.score != _score
            or str(_f.weighted_contribution) != _contrib
        ):
            factor_wrong += 1
    for _fid, (_cat, _score) in {
        "covenant_compliance": ("no_test_history", 5),
        "breach_history": ("no_breach_history", 2),
        "statement_quality": ("audited_statements_clean", 2),
    }.items():
        _f = k_factors[_fid]
        if _f.input_category != _cat or _f.score != _score:
            factor_wrong += 1
    if (
        str(k_assignment.composite_score) != "4.85"
        or k_assignment.assigned_grade != 5
        or k_assignment.assigned_band != "watch"
    ):
        factor_wrong += 1
    # The breach year re-grades on the same file.
    _t_deliver(kp, kcf, 2026, _T_FY2026)
    kp.run_due_covenant_tests(kcf, as_of=date(2027, 3, 1))
    k_regrade = kp.assign_rating(kcf, as_of=date(2027, 3, 1))
    if (
        str(k_regrade.composite_score) != "7.05"
        or k_regrade.assigned_grade != 7
        or k_regrade.assigned_band != "special mention"
    ):
        factor_wrong += 1
    print(f"rating_factor_score_wrong: {factor_wrong} of 9 graded "
          f"checks (required 0)")
    if factor_wrong:
        failures.append("a scorecard factor scored wrong")

    # Missing figures: named bands, missing inputs named.
    hp13 = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    _hb, _hf = ReferenceLosFeed().fetch_application("LOS-2026-0117")
    hcf13 = hp13.open_application(
        _hb, _hf,
        documents=[
            {
                "kind": DocumentKind.FINANCIAL_STATEMENT.value,
                "filename": f"hartline-fy{year}-statements.json",
                "data_hex": statement_document(year).hex(),
                "period_label": f"FY{year}",
            }
            for year in (2022, 2023, 2024)
        ],
    ).credit_file_id
    h_assignment = hp13.assign_rating(hcf13, as_of=date(2026, 6, 30))
    h_factors = {f.factor_id: f for f in h_assignment.factors}
    zero_filled = 0
    for _fid in ("dscr", "profitability_trend"):
        _f = h_factors[_fid]
        if (
            _f.input_value is not None
            or not _f.band_id.endswith("_insufficient_data")
            or not _f.missing_inputs
        ):
            zero_filled += 1
    for _f in h_assignment.factors:
        if _f.kind == "figure" and _f.input_value is None and (
            not _f.band_id.endswith("_insufficient_data")
        ):
            zero_filled += 1
    print(f"rating_missing_input_zero_filled: {zero_filled} "
          f"(required 0)")
    if zero_filled:
        failures.append("a missing rating input was zero-filled "
                        "or skipped")

    # The override's discipline, probed on the first assignment:
    # beyond one grade, uncoded, uncountersigned,
    # self-countersigned, and a no-op move are all refused.
    unlawful = 0
    _k_aid = k_assignment.assignment_id
    unlawful += _refused(lambda: kp.override_rating(
        kcf, _k_aid, officer_id=C_OFFICER,
        officer_role=C_OFFICER_ROLE, new_grade=7,
        reason_code="collateral_strength",
        countersigner_id="officer-eval-two",
        countersigner_role="senior_credit_officer",
    ))
    unlawful += _refused(lambda: kp.override_rating(
        kcf, _k_aid, officer_id=C_OFFICER,
        officer_role=C_OFFICER_ROLE, new_grade=4,
        reason_code="gut_feel",
        countersigner_id="officer-eval-two",
        countersigner_role="senior_credit_officer",
    ))
    unlawful += _refused(lambda: kp.override_rating(
        kcf, _k_aid, officer_id=C_OFFICER,
        officer_role=C_OFFICER_ROLE, new_grade=4,
        reason_code="collateral_strength",
    ))
    unlawful += _refused(lambda: kp.override_rating(
        kcf, _k_aid, officer_id=C_OFFICER,
        officer_role=C_OFFICER_ROLE, new_grade=4,
        reason_code="collateral_strength",
        countersigner_id=C_OFFICER,
        countersigner_role=C_OFFICER_ROLE,
    ))
    unlawful += _refused(lambda: kp.override_rating(
        kcf, _k_aid, officer_id=C_OFFICER,
        officer_role=C_OFFICER_ROLE, new_grade=5,
        reason_code="collateral_strength",
        countersigner_id="officer-eval-two",
        countersigner_role="senior_credit_officer",
    ))
    print(f"rating_override_unlawful_accepted: {unlawful} of 5 "
          f"probes accepted (required 0)")
    if unlawful:
        failures.append("an unlawful rating override was accepted")

    # The rating is an input, never a decision: the gates do not
    # import or consult the scorecard, no gate signature takes a
    # rating, and a rated file with no gate action stands
    # undecided with its memo a draft.
    used_as_approval = 0
    for _mod in (_committee_mod, _officer_mod):
        _src = inspect.getsource(_mod).lower()
        if "scorecard" in _src or "ratingassignment" in _src:
            used_as_approval += 1
    for _fn in (
        CreditPipeline.decide_committee,
        CreditPipeline.convene_committee,
        CreditPipeline.approve_memo,
    ):
        if any(
            "rating" in _name
            for _name in inspect.signature(_fn).parameters
        ):
            used_as_approval += 1
    _h_memo = hp13.compose_memo(hcf13)
    if hp13.memo_status(hcf13, _h_memo.memo_id) != "draft":
        used_as_approval += 1
    if hp13.store.list_committee_decisions(hcf13):
        used_as_approval += 1
    print(f"rating_used_as_approval: {used_as_approval} "
          f"(required 0)")
    if used_as_approval:
        failures.append("a rating was used as an approval")

    # History: a second registered version appends; the v1
    # artefact stands byte-identical; the comparison is labelled.
    _v1_json = kp.get_rating_assignment(
        kcf, k_regrade.assignment_id
    ).model_dump_json()
    _register_scorecard(_get_scorecard("1").model_copy(update={
        "version": "eval-2",
        "note": "Eval-registered second version.",
    }))
    _k_v2 = kp.assign_rating(
        kcf, as_of=date(2027, 3, 1), scorecard_version="eval-2"
    )
    rewritten = 0
    if kp.get_rating_assignment(
        kcf, k_regrade.assignment_id
    ).model_dump_json() != _v1_json:
        rewritten += 1
    _k_history = kp.rating_history(kcf)
    if (
        len(_k_history) != 3
        or _k_history[-1]["assignment"].assignment_id
        != _k_v2.assignment_id
        or _k_history[-1]["cross_version"] is not True
    ):
        rewritten += 1
    print(f"rating_history_rewritten: {rewritten} (required 0)")
    if rewritten:
        failures.append("a scorecard version change rewrote "
                        "rating history")

    # Positive paths: both assignments landed; the one-grade
    # override lands with both grades visible; the memo states
    # the rating; the review pack carries it with its breakdown.
    _k_override = kp.override_rating(
        kcf, _k_aid, officer_id=C_OFFICER,
        officer_role=C_OFFICER_ROLE, new_grade=4,
        reason_code="collateral_strength",
        countersigner_id="officer-eval-two",
        countersigner_role="senior_credit_officer",
    )
    _k_view = kp.rating_view(
        kp.get_rating_assignment(kcf, _k_aid)
    )
    _k_memo = kp.compose_memo(kcf)
    _k_pack = kp.assemble_review_pack(
        kcf, review_number=1, as_of=date(2027, 3, 1)
    )
    rating_positive = (
        h_assignment.assigned_grade == 5
        and _k_view["assigned_grade"] == 5
        and _k_view["final_grade"] == 4
        and _k_memo.rating_assignment_id == _k_v2.assignment_id
        and _k_memo.rating_grade == 7
        and _k_pack.rating_assignment_id == _k_v2.assignment_id
        and len(_k_pack.rating_factors) == 7
        and _k_pack.summary.verification.verified
    )
    print(f"rating_positive_paths: "
          f"{'assignments land, the one-grade override lands with both grades visible, memo and review pack carry the rating' if rating_positive else 'FAILED'}")
    if not rating_positive:
        failures.append("a rating positive path did not land")

    if failures:
        print("GATE: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
