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

Later rounds add counters (stale-input committee-ready, decline
without coded reasons) as their machinery lands. Exit code
0 = GATE: PASS.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commercial_credit.checklist import generate_checklist  # noqa: E402
from commercial_credit.computations import (  # noqa: E402
    compute_entity_pack,
    compute_global_pack,
    verify_trace,
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
)
from commercial_credit.memo import (  # noqa: E402
    MemoInputs,
    build_trace_universe,
    compose_memo,
    extract_figures,
    render_memo,
    verify_memo,
)
from commercial_credit.extraction import receive_extraction  # noqa: E402
from commercial_credit.models import (  # noqa: E402
    AdjustmentReasonCode,
    Borrower,
    BorrowerSegment,
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

    if failures:
        print("GATE: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
