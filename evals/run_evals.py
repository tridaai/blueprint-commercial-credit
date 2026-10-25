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

Later rounds add counters (untraceable memo figure, stale-input
committee-ready, decline without coded reasons) as their machinery
lands. Exit code 0 = GATE: PASS.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commercial_credit.checklist import generate_checklist  # noqa: E402
from commercial_credit.documents import (  # noqa: E402
    LocalDocumentStore,
    read_document_verified,
    register_document,
)
from commercial_credit.errors import CreditError, ProvenanceError  # noqa: E402
from commercial_credit.extraction import receive_extraction  # noqa: E402
from commercial_credit.models import (  # noqa: E402
    AdjustmentReasonCode,
    DocumentKind,
    ExtractedValue,
    FacilityRequest,
    FacilityType,
    Provenance,
    SpreadAdjustment,
)
from commercial_credit.pipeline import CreditPipeline  # noqa: E402
from commercial_credit.seed import (  # noqa: E402
    ReferenceLosFeed,
    ground_truth,
    statement_document,
)
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

    if failures:
        print("GATE: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
