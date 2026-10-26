"""The intake pipeline, as a LangGraph graph.

Topology (each round extends the right-hand side by one node):

    intake ──► checklist ──► document_intake ──► extraction ──► spreading
        ──► computation ──► policy ──► memo ──► END

Each node does one thing and records an audit event; the graph is
the auditable spine of the deployment, so the same compiled graph
serves the API, the demo and the evals. The memo node drafts the
credit memo and runs the figure verifier over it — a memo that
cannot be fully traced is refused, and the refusal fails the run
rather than storing a bad draft. The human gates act on the
stored memo rather than inside the intake graph: the credit
officer gate (round 6) is the workflow in the middle of this
module — named-officer review over the draft, edits re-verified,
returns coded, diffs recorded — and the committee gate (round 7)
below it: a quorum of named members deciding the officer-final
memo, conditions as structured records, declines only with coded
adverse-action reasons tied to the evidence, deferrals that name
what must change before the memo comes back.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .checklist import generate_checklist
from .committee import (

    ADVERSE_REASONS_VERSION,
    DEFERRAL_REASONS_VERSION,
    OUTCOMES,
    STATUS_IN_COMMITTEE,
    derive_full_status,
    render_adverse_action_notice,
    validate_adverse_reasons,
    validate_conditions,
    validate_deferral,
    validate_quorum,
)
from .computations import (
    ENGINE_VERSION,
    compute_entity_pack,
    compute_global_pack,
)
from .covenants import (
    amendment_record,
    build_covenant_set,
    current_covenants,
    derive_schedule,
    due_work,
    frequency_from_cadence,
    get_covenant_or_raise,
    validate_amendment,
)
from .covenant_testing import (
    BREACH_DEFAULT_REASON_CODES,
    BREACH_ESCALATION_REASON_CODES,
    breach_id_for,
    compare,
    lineage_root,
    period_sources_for,
    require_breach_actor,
    result_standing,
    test_result_id,
    validate_breach_transition,
    validate_cure,
    validate_waiver_decision,
)
from .documents import read_document_verified, register_document
from .errors import NotFoundError, ValidationError
from .extraction import OfflineReferenceExtractor, receive_extraction
from .models import (
    BREACH_CURED,
    BREACH_DEFAULTED,
    BREACH_ESCALATED,
    BREACH_TERMINAL_STATES,
    BREACH_UNDER_REVIEW,
    BREACH_WAIVED,
    RESULT_SUPERSEDED_BY_RESTATEMENT,
    TEST_AWAITING_INPUTS,
    TEST_FAIL,
    TEST_NOT_EVALUABLE,
    TEST_PASS,
    AdverseActionReason,
    AnnualReviewOutcome,
    AnnualReviewPack,
    AuditEvent,
    Borrower,
    BreachCase,
    BreachEvent,
    CommitteeCondition,
    CommitteeDecision,
    CommitteeMember,
    CommitteeReferral,
    ComputationPack,
    CovenantAmendment,
    CovenantDefinition,
    CovenantSet,
    CovenantTestResult,
    CreditFile,
    CreditFileStatus,
    CreditMemo,
    Disagreement,
    DocumentKind,
    Elimination,
    ExtractedValue,
    FacilityRequest,
    MemoCommitteeFlag,
    MemoDiff,
    MemoReviewEvent,
    MemoVerification,
    PolicyHistoryEntry,
    PolicyOutcomeDifference,
    PolicyReport,
    RatingAssignment,
    RatingOverride,
    ReadinessReport,
    ReviewBlocker,
    ReviewSummary,
    Spread,
    SpreadAdjustment,
    new_id,
    utcnow,
)
from .gateway import ModelAssistedExtractor, ModelGateway
from .memo import MemoInputs, RatingFacts, _canonical_hash, verify_memo
from .memo import compose_memo as draft_memo
from .memo import get_composer
from .officer_gate import (
    FLAG_KINDS,
    RETURN_REASONS_VERSION,
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_IN_REVIEW,
    STATUS_RETURNED,
    apply_edits,
    compute_memo_diff,
    require_identity,
    require_officer,
    validate_return_reasons,
)
from .policy import CURRENT_POLICY_VERSION
from .policy import evaluate_policy as run_policy_evaluation
from .ports import CreditFileStore, DocumentStore, Extractor
from .readiness import build_readiness_report
from .readiness import readiness_refusal_message
from .readiness import render_outstanding_items
from .reviews import (
    REVIEW_VOCABULARY_VERSION,
    REVIEW_WORK_STATES,
    ReviewInputs,
    ReviewInstance,
    derive_review_instances,
    get_review_composer,
    locked_fact_paragraphs,
    validate_review_outcome,
    verify_review_summary,
)
from .scorecard import (
    build_rating_assignment,
    final_grade_of,
    get_scorecard,
    grade_band_label,
    validate_override,
)
from .spreading import build_spread as assemble_spread
from .spreading import validate_adjustment

log = logging.getLogger("commercial_credit.pipeline")


class PipelineState(TypedDict, total=False):
    borrower: Borrower
    facility: FacilityRequest
    credit_file_id: str
    pending_documents: list[dict]
    extracted_document_ids: list[str]
    extraction_count: int
    spread_id: str
    computation_pack_id: str
    policy_report_id: str
    memo_id: str


class CreditPipeline:
    """The compiled intake graph plus the stores it writes through."""

    def __init__(
        self,
        store: CreditFileStore,
        documents: DocumentStore,
        extractor: Extractor | None = None,
        actor: str = "intake-pipeline",
        gateway: ModelGateway | None = None,
    ) -> None:
        self.store = store
        self.documents = documents
        self.extractor = extractor or OfflineReferenceExtractor()
        self.actor = actor
        # Round 15: when a gateway is wired, every drafting call
        # resolves through it (profiles as data, every call
        # priced into the ledger, budget enforced). A bare stack
        # keeps the round-5/12 composer registries — the offline
        # composer, and the 'live' stub that refuses loudly —
        # exactly as before.
        self.gateway = gateway
        self._graph = self._build()

    # -- model gateway (round 15) ------------------------------------

    def _memo_composer(self, profile: str, credit_file_id: str):  # noqa: ANN202
        """The memo composer for a profile: the gateway's bound
        composer when wired (the call ledgers against the file),
        the round-5 registry otherwise."""
        if self.gateway is not None:
            return self.gateway.memo_composer(
                profile, credit_file_id=credit_file_id
            )
        return get_composer(profile)

    def _review_composer(self, profile: str, credit_file_id: str):  # noqa: ANN202
        """The review-summary composer for a profile — the
        round-12 parallel of `_memo_composer`."""
        if self.gateway is not None:
            return self.gateway.review_composer(
                profile, credit_file_id=credit_file_id
            )
        return get_review_composer(profile)

    def _require_gateway(self) -> ModelGateway:
        if self.gateway is None:
            raise ValidationError(
                "no model gateway is wired in this stack; the "
                "cost ledger exists only where the gateway does "
                "(wiring.build_pipeline wires one)"
            )
        return self.gateway

    def model_profiles(self) -> list[dict]:
        """Every gateway profile: model, price, configured flag.
        Key values never appear — variable names only."""
        return self._require_gateway().profile_summaries()

    def file_model_ledger(self, credit_file_id: str) -> list[dict]:
        """The file's priced model calls, in ledger order."""
        self._load(credit_file_id)
        gateway = self._require_gateway()
        return [
            entry.model_dump(mode="json")
            for entry in gateway.list_call_entries(credit_file_id)
        ]

    def file_model_costs(self, credit_file_id: str) -> dict:
        """The file's model cost rollup: totals by purpose and
        profile, with its budget position."""
        self._load(credit_file_id)
        return self._require_gateway().file_rollup(credit_file_id)

    def portfolio_model_costs(self) -> list[dict]:
        """Model spend across the portfolio, by day and profile."""
        return self._require_gateway().portfolio_rollup()

    # -- graph nodes -------------------------------------------------
    def _node_intake(self, state: PipelineState) -> PipelineState:
        credit_file = CreditFile(
            credit_file_id=new_id("cf"),
            borrower=state["borrower"],
            facility=state["facility"],
            status=CreditFileStatus.INTAKE,
        )
        self.store.save_credit_file(credit_file)
        self._audit(credit_file.credit_file_id, "application_intake",
                    f"Application opened for {credit_file.borrower.legal_name}")
        log.info("credit file %s opened", credit_file.credit_file_id)
        return {"credit_file_id": credit_file.credit_file_id}

    def _node_checklist(self, state: PipelineState) -> PipelineState:
        credit_file = self._load(state["credit_file_id"])
        credit_file.checklist = generate_checklist(credit_file.facility)
        credit_file.status = CreditFileStatus.COLLECTING_DOCUMENTS
        self.store.save_credit_file(credit_file)
        self._audit(credit_file.credit_file_id, "checklist_generated",
                    f"{len(credit_file.checklist)} required items for "
                    f"{credit_file.facility.facility_type.value}")
        return {}

    def _node_document_intake(self, state: PipelineState) -> PipelineState:
        credit_file = self._load(state["credit_file_id"])
        for pending in state.get("pending_documents", []):
            doc = register_document(
                self.store,
                self.documents,
                credit_file,
                kind=DocumentKind(pending["kind"]),
                filename=pending["filename"],
                data=bytes.fromhex(pending["data_hex"]),
                period_label=pending.get("period_label"),
                content_type=pending.get("content_type", "application/json"),
            )
            self._audit(credit_file.credit_file_id, "document_registered",
                        f"{doc.kind.value} {doc.filename} sha256={doc.sha256[:12]}…")
        return {}

    def _node_extraction(self, state: PipelineState) -> PipelineState:
        credit_file = self._load(state["credit_file_id"])
        total = 0
        done: list[str] = []
        for doc in credit_file.documents:
            # Round 8: tax returns are extracted too — they are a
            # source the spread reads in their own right, and the
            # disagreements between them and the statements are
            # round 8's subject. Other kinds stay unextracted:
            # the extractor reads statements and returns, and
            # returns [] for anything else.
            if doc.kind not in (
                DocumentKind.FINANCIAL_STATEMENT,
                DocumentKind.TAX_RETURN,
            ):
                continue
            content = read_document_verified(self.documents, doc)
            # Round 15: a model-assisted extractor is bound per
            # file, so the extraction calls it makes ledger against
            # this file and the per-file budget applies. Any other
            # extractor (the offline reference extractor) is used
            # exactly as constructed.
            extractor = self.extractor
            if isinstance(extractor, ModelAssistedExtractor):
                extractor = extractor.for_file(credit_file.credit_file_id)
            values = receive_extraction(extractor.extract(doc, content))
            if values:
                self.store.save_extractions(credit_file.credit_file_id, values)
                total += len(values)
                done.append(doc.document_id)
                self._audit(credit_file.credit_file_id, "extraction_completed",
                            f"{len(values)} values from {doc.document_id} "
                            "with complete provenance")
        return {"extracted_document_ids": done, "extraction_count": total}

    def _node_spreading(self, state: PipelineState) -> PipelineState:
        spread = self.build_spread(state["credit_file_id"])
        return {"spread_id": spread.spread_id}

    def _node_computation(self, state: PipelineState) -> PipelineState:
        pack = self.compute_metrics(state["credit_file_id"])
        return {"computation_pack_id": pack.pack_id}

    def _node_policy(self, state: PipelineState) -> PipelineState:
        report = self.evaluate_policy(state["credit_file_id"])
        return {"policy_report_id": report.report_id}

    def _node_memo(self, state: PipelineState) -> PipelineState:
        memo = self.compose_memo(
            state["credit_file_id"], report_id=state["policy_report_id"]
        )
        return {"memo_id": memo.memo_id}

    def _build(self):  # noqa: ANN202 - compiled LangGraph app
        graph = StateGraph(PipelineState)
        graph.add_node("intake", self._node_intake)
        graph.add_node("checklist", self._node_checklist)
        graph.add_node("document_intake", self._node_document_intake)
        graph.add_node("extraction", self._node_extraction)
        graph.add_node("spreading", self._node_spreading)
        graph.add_node("computation", self._node_computation)
        graph.add_node("policy", self._node_policy)
        graph.add_node("memo", self._node_memo)
        graph.add_edge(START, "intake")
        graph.add_edge("intake", "checklist")
        graph.add_edge("checklist", "document_intake")
        graph.add_edge("document_intake", "extraction")
        graph.add_edge("extraction", "spreading")
        graph.add_edge("spreading", "computation")
        graph.add_edge("computation", "policy")
        graph.add_edge("policy", "memo")
        graph.add_edge("memo", END)
        return graph.compile()

    # -- public runs ---------------------------------------------------
    def open_application(
        self,
        borrower: Borrower,
        facility: FacilityRequest,
        documents: list[dict] | None = None,
    ) -> CreditFile:
        """Run intake → checklist → documents → extraction → spreading
        → computation → policy."""
        result = self._graph.invoke(
            {
                "borrower": borrower,
                "facility": facility,
                "pending_documents": documents or [],
            }
        )
        return self._load(result["credit_file_id"])

    def run_extraction(self, credit_file_id: str) -> CreditFile:
        """Extraction pass over a file's already-registered documents."""
        credit_file = self._load(credit_file_id)
        self._node_extraction({"credit_file_id": credit_file.credit_file_id})
        return self._load(credit_file_id)

    def build_spread(self, credit_file_id: str) -> Spread:
        """(Re)build the spread from the file's current extractions
        and adjustments, and store it. Deterministic: rebuilding over
        unchanged inputs reproduces the stored spread id, in which
        case nothing is re-audited — the audit trail records change,
        not reads."""
        credit_file = self._load(credit_file_id)
        spread = assemble_spread(
            credit_file,
            self.store.get_extractions(credit_file_id),
            self.store.list_adjustments(credit_file_id),
        )
        latest = self.store.get_latest_spread(credit_file_id)
        self.store.save_spread(spread)
        if latest is None or latest.spread_id != spread.spread_id:
            detail = (
                f"{len(spread.periods)} periods "
                f"({', '.join(str(y) for y in spread.periods)}), "
                f"{len(spread.lines)} template lines, "
                f"{len(spread.unmapped)} unmapped lines, "
                f"template {spread.template_version}"
            )
            if spread.disagreements:
                material = sum(1 for d in spread.disagreements if d.material)
                detail += (
                    f"; {len(spread.disagreements)} document "
                    f"disagreement(s) recorded under policy "
                    f"{spread.disagreement_policy_version} "
                    f"({material} material)"
                )
            self._audit(credit_file_id, "spread_built", detail)
        return spread

    def get_spread(self, credit_file_id: str) -> Spread:
        """The file's current spread, rebuilt so it can never lag a
        restated document or a new extraction behind the store."""
        return self.build_spread(credit_file_id)

    def list_disagreements(
        self, credit_file_id: str, *, material: bool | None = None
    ) -> list[Disagreement]:
        """The disagreements the file's current spread records
        (round 8) — every one, material and immaterial alike, in
        the spread's own order. The as-reported view: each record
        carries every source's figure with its provenance."""
        spread = self.build_spread(credit_file_id)
        records = list(spread.disagreements)
        if material is not None:
            records = [d for d in records if d.material == material]
        return records

    def get_disagreement(
        self, credit_file_id: str, disagreement_id: str
    ) -> Disagreement:
        """One disagreement record from the file's current spread."""
        for record in self.list_disagreements(credit_file_id):
            if record.disagreement_id == disagreement_id:
                return record
        raise NotFoundError(
            f"no disagreement {disagreement_id!r} on credit file "
            f"{credit_file_id}"
        )

    def compute_metrics(
        self,
        credit_file_id: str,
        *,
        view: str = "adjusted",
        supplemental: list[ExtractedValue] | None = None,
        covenant_definitions: list[CovenantDefinition] | None = None,
    ) -> ComputationPack:
        """Run the computation engine over the file's current spread
        and store the resulting pack.

        The pack is content-addressed: recomputing the same inputs
        returns the stored artefact, and the audit trail records the
        run only the first time those inputs are seen — change is
        audited, reads are not.
        """
        credit_file = self._load(credit_file_id)
        spread = self.build_spread(credit_file_id)
        pack = compute_entity_pack(
            credit_file_id=credit_file_id,
            entity=credit_file.borrower.legal_name,
            spread=spread,
            extractions=self.store.get_extractions(credit_file_id),
            supplemental=supplemental or [],
            covenant_definitions=covenant_definitions or [],
            view=view,
        )
        self._store_pack(pack)
        return pack

    def compute_global(
        self,
        credit_file_id: str,
        *,
        guarantor_spreads: dict[str, tuple[Spread, list[ExtractedValue]]] | None = None,
        eliminations: list[Elimination] | None = None,
        expected_guarantors: list[str] | None = None,
        exclude_missing_guarantors: bool = False,
        view: str = "adjusted",
    ) -> ComputationPack:
        """The global cash-flow view: the borrower's pack combined
        with a pack per guarantor spread handed in, named
        eliminations applied. Guarantor entity packs are stored as
        artefacts of this credit file alongside the borrower's."""
        credit_file = self._load(credit_file_id)
        borrower_pack = self.compute_metrics(credit_file_id, view=view)
        entity_packs: dict[str, ComputationPack] = {}
        for name, (g_spread, g_extractions) in (guarantor_spreads or {}).items():
            pack = compute_entity_pack(
                credit_file_id=credit_file_id,
                entity=name,
                spread=g_spread,
                extractions=g_extractions,
                view=view,
            )
            self._store_pack(pack)
            entity_packs[name] = pack
        pack = compute_global_pack(
            credit_file_id=credit_file_id,
            borrower_pack=borrower_pack,
            entity_packs=entity_packs,
            eliminations=eliminations or [],
            expected_guarantors=expected_guarantors
            or [g.name for g in credit_file.facility.guarantors],
            exclude_missing_guarantors=exclude_missing_guarantors,
        )
        self._store_pack(pack)
        return pack

    def _store_pack(self, pack: ComputationPack) -> None:
        if self.store.get_computation_pack(pack.pack_id) is not None:
            return
        self.store.save_computation_pack(pack)
        computed = sum(1 for f in pack.figures if f.status == "computed")
        self._audit(
            pack.credit_file_id,
            "computation_pack_stored",
            f"{pack.scope} pack {pack.pack_id} for {pack.entity}: "
            f"{computed}/{len(pack.figures)} figures computed, "
            f"{len(pack.covenant_results)} covenant results, "
            f"engine {ENGINE_VERSION}, {pack.view} view",
        )

    def evaluate_policy(
        self,
        credit_file_id: str,
        *,
        pack_version: str | None = None,
        as_of: date | None = None,
        global_pack: ComputationPack | None = None,
    ) -> PolicyReport:
        """Evaluate the file against ONE pinned policy pack version
        and store the report.

        The version is pinned by the caller (the pipeline's own runs
        use the current version); nothing here reaches for a newer
        pack on its own. The report is content-addressed: evaluating
        unchanged inputs under the same version returns the stored
        artefact, and the audit trail and the file's policy history
        record the evaluation only the first time those inputs are
        seen — a later version's evaluation appends its own report
        and its own history entry, and never rewrites this one.
        """
        credit_file = self._load(credit_file_id)
        pack = self.compute_metrics(credit_file_id)
        report = run_policy_evaluation(
            credit_file,
            pack,
            policy_version=pack_version or CURRENT_POLICY_VERSION,
            global_pack=global_pack,
            as_of=as_of or date.today(),
        )
        if self.store.get_policy_report(report.report_id) is not None:
            return report
        self.store.save_policy_report(report)
        self._audit(
            credit_file_id,
            "policy_evaluated",
            f"policy pack {report.policy_pack_id} {report.policy_version} "
            f"report {report.report_id} as of {report.as_of}: "
            f"{report.summary.get('pass', 0)} pass, "
            f"{report.summary.get('fail', 0)} fail, "
            f"{report.summary.get('not_evaluable', 0)} not evaluable "
            f"({report.summary.get('blocker_failures', 0)} blocker failures)",
        )
        credit_file = self._load(credit_file_id)
        if not any(
            h.report_id == report.report_id for h in credit_file.policy_history
        ):
            credit_file.policy_history.append(
                PolicyHistoryEntry(
                    policy_pack_id=report.policy_pack_id,
                    policy_version=report.policy_version,
                    report_id=report.report_id,
                    as_of=report.as_of,
                )
            )
            self.store.save_credit_file(credit_file)
        return report

    def compose_memo(
        self,
        credit_file_id: str,
        *,
        report_id: str | None = None,
        pack_version: str | None = None,
        profile: str = "offline",
        global_pack: ComputationPack | None = None,
    ) -> CreditMemo:
        """Compose the draft credit memo for a file and store it.

        The memo pins the artefacts it was composed from: the
        policy report named by `report_id`, or evaluated under
        `pack_version`, or — the default — the file's most recent
        report (evaluating under the current version when none
        exists yet); the computation pack that report read; the
        spread that pack ran over. Composition runs the composer
        profile's draft through the figure verifier: a draft with
        an untraceable figure or a dropped policy exception raises
        MemoVerificationError and is never stored. The stored memo
        is content-addressed — composing over unchanged inputs
        returns the stored artefact and audits only the first time.
        """
        credit_file = self._load(credit_file_id)
        if report_id is not None:
            report = self.store.get_policy_report(report_id)
            if report is None or report.credit_file_id != credit_file_id:
                raise NotFoundError(
                    f"policy report {report_id} not found on credit "
                    f"file {credit_file_id}"
                )
        elif pack_version is not None:
            report = self.evaluate_policy(
                credit_file_id,
                pack_version=pack_version,
                global_pack=global_pack,
            )
        else:
            reports = self.store.list_policy_reports(credit_file_id)
            report = (
                reports[-1]
                if reports
                else self.evaluate_policy(
                    credit_file_id, global_pack=global_pack
                )
            )
        pack = self.store.get_computation_pack(report.computation_pack_id)
        if pack is None:
            raise NotFoundError(
                f"computation pack {report.computation_pack_id} not found"
            )
        spread = next(
            (
                s
                for s in self.store.list_spreads(credit_file_id)
                if s.spread_id == pack.spread_id
            ),
            None,
        ) or self.store.get_latest_spread(credit_file_id)
        if spread is None:
            raise NotFoundError(f"no spread on credit file {credit_file_id}")
        global_artefact = global_pack
        if global_artefact is None and report.global_pack_id:
            global_artefact = self.store.get_computation_pack(
                report.global_pack_id
            )
        memo = draft_memo(
            credit_file,
            spread,
            pack,
            report,
            global_pack=global_artefact,
            composer=self._memo_composer(profile, credit_file_id),
            rating=self._current_rating_facts(credit_file_id),
        )
        if self.store.get_memo(memo.memo_id) is not None:
            return memo
        self.store.save_memo(memo)
        self._audit(
            credit_file_id,
            "memo_composed",
            f"draft memo {memo.memo_id}: composed from spread "
            f"{memo.spread_id}, computation pack {memo.computation_pack_id}, "
            f"policy report {memo.policy_report_id} (pack "
            f"{memo.policy_version}) by profile {memo.composer_profile}; "
            f"{memo.verification.figure_count} figures traced, "
            f"{len(memo.verification.exceptions_listed)} policy "
            f"exceptions carried; status draft",
        )
        return memo

    def _get_memo_raw(self, credit_file_id: str, memo_id: str) -> CreditMemo:
        """The stored artefact, exactly as stored — no status
        overlay. The gate works on artefacts; readers get the
        derived review status via get_memo / list_memos."""
        self._load(credit_file_id)
        memo = self.store.get_memo(memo_id)
        if memo is None or memo.credit_file_id != credit_file_id:
            raise NotFoundError(
                f"memo {memo_id} not found on credit file {credit_file_id}"
            )
        return memo

    def _status_of(self, memo: CreditMemo) -> str:
        events = self.store.list_memo_review_events(
            memo.credit_file_id, memo.memo_id
        )
        return derive_full_status(memo, events)

    def _with_status(self, memo: CreditMemo) -> CreditMemo:
        return memo.model_copy(update={"status": self._status_of(memo)})

    def get_memo(self, credit_file_id: str, memo_id: str) -> CreditMemo:
        return self._with_status(self._get_memo_raw(credit_file_id, memo_id))

    def list_memos(self, credit_file_id: str) -> list[CreditMemo]:
        self._load(credit_file_id)
        return [
            self._with_status(memo)
            for memo in self.store.list_memos(credit_file_id)
        ]

    def memo_status(self, credit_file_id: str, memo_id: str) -> str:
        """A memo's derived review status (officer gate, round 6)."""
        return self._status_of(self._get_memo_raw(credit_file_id, memo_id))

    # -- the credit officer gate (round 6) ---------------------------
    def _memo_inputs(self, memo: CreditMemo) -> MemoInputs:
        """The artefacts a memo pins, loaded back from the store —
        the inputs its edits are re-verified against."""
        credit_file = self._load(memo.credit_file_id)
        report = self.store.get_policy_report(memo.policy_report_id)
        if report is None:
            raise NotFoundError(
                f"policy report {memo.policy_report_id} not found"
            )
        pack = self.store.get_computation_pack(memo.computation_pack_id)
        if pack is None:
            raise NotFoundError(
                f"computation pack {memo.computation_pack_id} not found"
            )
        spread = next(
            (
                s
                for s in self.store.list_spreads(memo.credit_file_id)
                if s.spread_id == memo.spread_id
            ),
            None,
        ) or self.store.get_latest_spread(memo.credit_file_id)
        if spread is None:
            raise NotFoundError(
                f"no spread on credit file {memo.credit_file_id}"
            )
        global_pack = (
            self.store.get_computation_pack(memo.global_pack_id)
            if memo.global_pack_id
            else None
        )
        return MemoInputs(
            credit_file=credit_file,
            spread=spread,
            pack=pack,
            policy_report=report,
            global_pack=global_pack,
        )

    def _record_review_event(self, event: MemoReviewEvent) -> MemoReviewEvent:
        self.store.save_memo_review_event(event)
        return event

    # -------------------------------------------------------------
    # Round 9: committee readiness (design law 7). The report is
    # computed fresh at the moment it is used — at officer approval,
    # so the block is stamped on the approval record, and again at
    # convene, where any blocker refuses the transition and the
    # refusal names every blocker. A report minted earlier is a
    # record, never a blessing: staleness is judged at the as_of of
    # the action itself.
    # -------------------------------------------------------------

    def _store_readiness(self, report: ReadinessReport) -> ReadinessReport:
        """Store a readiness report, auditing only its first minting
        (the artefact is content-addressed; recomputation of the
        same state records nothing new)."""
        is_new = (
            self.store.get_readiness_report(report.report_id) is None
        )
        self.store.save_readiness_report(report)
        if is_new:
            self._audit(
                report.credit_file_id,
                "readiness_computed",
                f"readiness report {report.report_id} computed at "
                f"{report.as_of} under readiness policy "
                f"{report.readiness_policy_id} "
                f"{report.readiness_policy_version} and policy pack "
                f"{report.policy_version}: "
                f"{report.blocker_count} blocker(s), "
                f"{report.summary}",
            )
        return report

    def _readiness_for_inputs(
        self, inputs: MemoInputs, *, as_of: date
    ) -> ReadinessReport:
        return build_readiness_report(
            inputs.credit_file,
            as_of=as_of,
            policy_version=inputs.policy_report.policy_version,
            pack=inputs.pack,
            global_pack=inputs.global_pack,
            policy_report=inputs.policy_report,
        )

    def compute_readiness(
        self,
        credit_file_id: str,
        *,
        as_of: date | None = None,
        memo_id: str | None = None,
    ) -> ReadinessReport:
        """Compute (and store) the file's current readiness report.

        With a memo, the report reads the artefacts that memo pins —
        the view the committee gate itself uses. Without one, it
        reads the file's latest computation packs and policy
        report. `as_of` is the evaluation date; freshness is judged
        at that date, never at the wall clock.
        """
        as_of = as_of or date.today()
        if memo_id is not None:
            memo = self._get_memo_raw(credit_file_id, memo_id)
            report = self._readiness_for_inputs(
                self._memo_inputs(memo), as_of=as_of
            )
            return self._store_readiness(report)
        credit_file = self._load(credit_file_id)
        packs = self.store.list_computation_packs(credit_file_id)
        pack = next(
            (p for p in reversed(packs) if p.scope == "entity"), None
        )
        global_pack = next(
            (p for p in reversed(packs) if p.scope == "global"), None
        )
        reports = self.store.list_policy_reports(credit_file_id)
        policy_report = reports[-1] if reports else None
        report = build_readiness_report(
            credit_file,
            as_of=as_of,
            policy_version=(
                policy_report.policy_version
                if policy_report is not None
                else CURRENT_POLICY_VERSION
            ),
            pack=pack,
            global_pack=global_pack,
            policy_report=policy_report,
        )
        return self._store_readiness(report)

    def get_readiness_report(
        self, credit_file_id: str, report_id: str
    ) -> ReadinessReport:
        self._load(credit_file_id)
        report = self.store.get_readiness_report(report_id)
        if report is None or report.credit_file_id != credit_file_id:
            raise NotFoundError(
                f"readiness report {report_id} not found on credit "
                f"file {credit_file_id}"
            )
        return report

    def list_readiness_reports(
        self, credit_file_id: str
    ) -> list[ReadinessReport]:
        self._load(credit_file_id)
        return self.store.list_readiness_reports(credit_file_id)

    def outstanding_items(
        self,
        credit_file_id: str,
        *,
        as_of: date | None = None,
        memo_id: str | None = None,
    ) -> str:
        """The borrower-facing 'what we still need' list, rendered
        from the current readiness report — generated, never
        hand-typed."""
        report = self.compute_readiness(
            credit_file_id, as_of=as_of, memo_id=memo_id
        )
        credit_file = self._load(credit_file_id)
        return render_outstanding_items(report, credit_file)

    def submit_memo_for_review(
        self,
        credit_file_id: str,
        memo_id: str,
        *,
        actor: str,
        actor_role: str,
    ) -> CreditMemo:
        """Open the officer review of a draft memo.

        Any named actor may submit (the relationship manager who
        prepared the file, say); deciding is reserved to officers.
        A memo that is not a draft — already in review, returned,
        approved — cannot be submitted again.
        """
        require_identity(actor, actor_role, what="submission for review")
        memo = self._get_memo_raw(credit_file_id, memo_id)
        status = self._status_of(memo)
        if status != STATUS_DRAFT:
            raise ValidationError(
                f"memo {memo_id} is {status}; only a draft can be "
                "submitted for officer review"
            )
        self._record_review_event(MemoReviewEvent(
            credit_file_id=credit_file_id,
            memo_id=memo_id,
            action="submitted_for_review",
            actor_id=actor,
            actor_role=actor_role,
        ))
        self._audit(
            credit_file_id,
            "memo_submitted_for_review",
            f"draft memo {memo_id} submitted for credit officer "
            f"review by {actor} ({actor_role})",
            actor=actor,
        )
        return self.get_memo(credit_file_id, memo_id)

    def approve_memo(
        self,
        credit_file_id: str,
        memo_id: str,
        *,
        officer_id: str,
        officer_role: str,
        edits: dict[str, list[str]] | None = None,
        note: str = "",
        as_of: date | None = None,
    ) -> CreditMemo:
        """The officer's approval: as-is, or with paragraph edits.

        Approval mints the officer-final memo — a new artefact
        that supersedes the draft, names the approving officer and
        carries the memo's committee flags — plus a MemoDiff
        recording exactly what changed. Edited text faces the
        figure verifier over the full memo before anything is
        stored: one untraceable figure, or an exceptions section
        that no longer accounts for the pinned report, refuses
        the approval and stores nothing. An officer who declared
        a conflict of interest on this memo cannot approve it.

        Approval is NOT the committee gate: it lands even when the
        file carries readiness blockers (round 9) — but the
        readiness report computed over the memo's pinned artefacts
        is stamped onto the approval event and the diff, so the
        block is on the record from this moment, never a surprise
        at convene.
        """
        require_officer(officer_id, officer_role, what="an approval")
        memo = self._get_memo_raw(credit_file_id, memo_id)
        status = self._status_of(memo)
        if status != STATUS_IN_REVIEW:
            raise ValidationError(
                f"memo {memo_id} is {status}; only a memo in "
                "officer review can be approved"
            )
        events = self.store.list_memo_review_events(credit_file_id, memo_id)
        own_conflict = next(
            (
                e.flag
                for e in events
                if e.action == "flagged"
                and e.flag is not None
                and e.flag.kind == "conflict_of_interest"
                and e.flag.officer_id == officer_id
            ),
            None,
        )
        if own_conflict is not None:
            raise ValidationError(
                f"officer {officer_id} recorded a conflict of "
                f"interest on memo {memo_id} (flag "
                f"{own_conflict.flag_id}); the memo must be "
                "approved by a different officer"
            )
        edits = dict(edits or {})
        sections = (
            apply_edits(memo, edits)
            if edits
            else [s.model_copy(deep=True) for s in memo.sections]
        )
        flags = [
            e.flag for e in events if e.action == "flagged" and e.flag
        ]
        candidate = memo.model_copy(update={
            "memo_id": "memo-pending",
            "status": STATUS_APPROVED,
            "sections": sections,
            "supersedes_memo_id": memo.memo_id,
            "reviewed_by": officer_id,
            "committee_flags": flags,
            "verification": MemoVerification(
                composer_profile=memo.composer_profile, figure_count=0
            ),
            "created_at": utcnow(),
        })
        inputs = self._memo_inputs(memo)
        # The verifier decides — over the final rendered text,
        # exactly as it decided for the draft. A refusal raises
        # MemoVerificationError and nothing below runs.
        verification = verify_memo(candidate, inputs)
        readiness = self._readiness_for_inputs(
            inputs, as_of=as_of or date.today()
        )
        descriptor = {
            "supersedes_memo_id": memo.memo_id,
            "reviewed_by": officer_id,
            "credit_file_id": candidate.credit_file_id,
            "spread_id": candidate.spread_id,
            "computation_pack_id": candidate.computation_pack_id,
            "global_pack_id": candidate.global_pack_id,
            "policy_report_id": candidate.policy_report_id,
            "composer_profile": candidate.composer_profile,
            "sections": [s.model_dump(mode="json") for s in sections],
            "verification": verification.model_dump(mode="json"),
        }
        final = candidate.model_copy(update={
            "memo_id": f"memo-{_canonical_hash(descriptor)[:16]}",
            "verification": verification,
        })
        action = "approved_with_edits" if edits else "approved_as_is"
        diff = compute_memo_diff(
            memo,
            final,
            officer_id=officer_id,
            officer_role=officer_role,
            action=action,
            note=note,
        )
        diff.readiness_report_id = readiness.report_id
        diff.readiness_blocker_count = readiness.blocker_count
        self.store.save_memo(final)
        self.store.save_memo_diff(diff)
        self._store_readiness(readiness)
        self._record_review_event(MemoReviewEvent(
            credit_file_id=credit_file_id,
            memo_id=memo_id,
            action=action,
            actor_id=officer_id,
            actor_role=officer_role,
            resulting_memo_id=final.memo_id,
            readiness_report_id=readiness.report_id,
            readiness_blocker_count=readiness.blocker_count,
            note=note,
        ))
        self._audit(
            credit_file_id,
            "memo_approved",
            f"memo {memo_id} {action.replace('_', ' ')} by "
            f"{officer_id} ({officer_role}); officer-final memo "
            f"{final.memo_id}, diff {diff.diff_id}: "
            f"{len(diff.section_changes)} section(s) and "
            f"{len(diff.figure_changes)} figure(s) changed; "
            f"readiness report {readiness.report_id} carries "
            f"{readiness.blocker_count} blocker(s)",
            actor=officer_id,
        )
        return final

    def return_memo(
        self,
        credit_file_id: str,
        memo_id: str,
        *,
        officer_id: str,
        officer_role: str,
        reason_codes: list[str],
        note: str = "",
    ) -> CreditMemo:
        """Return a memo in review for rework, with coded reasons.

        The codes come from the versioned return vocabulary; the
        version is recorded on the event, so a later vocabulary
        change never reinterprets this return. A returned memo
        cannot be approved — it must be recomposed into a new
        draft (see recompose_memo).
        """
        require_officer(officer_id, officer_role, what="a return for rework")
        memo = self._get_memo_raw(credit_file_id, memo_id)
        status = self._status_of(memo)
        if status != STATUS_IN_REVIEW:
            raise ValidationError(
                f"memo {memo_id} is {status}; only a memo in "
                "officer review can be returned"
            )
        validate_return_reasons(reason_codes, note)
        self._record_review_event(MemoReviewEvent(
            credit_file_id=credit_file_id,
            memo_id=memo_id,
            action="returned_for_rework",
            actor_id=officer_id,
            actor_role=officer_role,
            reason_codes=list(reason_codes),
            reason_vocabulary=RETURN_REASONS_VERSION,
            note=note,
        ))
        self._audit(
            credit_file_id,
            "memo_returned_for_rework",
            f"memo {memo_id} returned for rework by {officer_id} "
            f"({officer_role}); reasons ({RETURN_REASONS_VERSION}): "
            f"{', '.join(reason_codes)}",
            actor=officer_id,
        )
        return self.get_memo(credit_file_id, memo_id)

    def flag_memo(
        self,
        credit_file_id: str,
        memo_id: str,
        *,
        officer_id: str,
        officer_role: str,
        kind: str,
        section_key: str | None = None,
        note: str = "",
    ) -> MemoReviewEvent:
        """Flag an item on a memo in review for the committee, or
        declare a conflict of interest on it.

        Flags do not move the memo's status. Committee-attention
        flags are carried onto the officer-final memo at approval;
        a conflict-of-interest flag bars the flagging officer from
        approving the memo (approve_memo enforces it).
        """
        require_officer(officer_id, officer_role, what="a flag")
        if kind not in FLAG_KINDS:
            raise ValidationError(
                f"unknown flag kind {kind!r}; kinds are "
                f"{sorted(FLAG_KINDS)}"
            )
        memo = self._get_memo_raw(credit_file_id, memo_id)
        status = self._status_of(memo)
        if status != STATUS_IN_REVIEW:
            raise ValidationError(
                f"memo {memo_id} is {status}; only a memo in "
                "officer review can be flagged"
            )
        if section_key is not None and memo.section(section_key) is None:
            raise ValidationError(
                f"flag names section {section_key!r}, which memo "
                f"{memo_id} does not have"
            )
        flag = MemoCommitteeFlag(
            flag_id=new_id("flag"),
            kind=kind,
            section_key=section_key,
            note=note,
            officer_id=officer_id,
            officer_role=officer_role,
        )
        event = self._record_review_event(MemoReviewEvent(
            credit_file_id=credit_file_id,
            memo_id=memo_id,
            action="flagged",
            actor_id=officer_id,
            actor_role=officer_role,
            flag=flag,
            note=note,
        ))
        self._audit(
            credit_file_id,
            "memo_flagged",
            f"memo {memo_id} flagged {kind} by {officer_id} "
            f"({officer_role})"
            + (f" on section {section_key}" if section_key else "")
            + (f": {note}" if note else ""),
            actor=officer_id,
        )
        return event

    def recompose_memo(
        self,
        credit_file_id: str,
        *,
        supersedes_memo_id: str,
        actor: str,
        actor_role: str,
    ) -> CreditMemo:
        """Recompose a returned memo into a NEW draft version.

        The new draft is composed from the file's current pinned
        artefacts (latest policy report and the pack and spread it
        read), names the returned memo it supersedes, and starts
        its own review life as a draft. The returned memo itself
        is never reopened or edited.
        """
        require_identity(actor, actor_role, what="recomposition")
        old = self._get_memo_raw(credit_file_id, supersedes_memo_id)
        status = self._status_of(old)
        if status != STATUS_RETURNED:
            raise ValidationError(
                f"memo {supersedes_memo_id} is {status}; only a "
                "returned memo is recomposed"
            )
        credit_file = self._load(credit_file_id)
        reports = self.store.list_policy_reports(credit_file_id)
        report = (
            reports[-1]
            if reports
            else self.evaluate_policy(credit_file_id)
        )
        pack = self.store.get_computation_pack(report.computation_pack_id)
        if pack is None:
            raise NotFoundError(
                f"computation pack {report.computation_pack_id} not found"
            )
        spread = next(
            (
                s
                for s in self.store.list_spreads(credit_file_id)
                if s.spread_id == pack.spread_id
            ),
            None,
        ) or self.store.get_latest_spread(credit_file_id)
        if spread is None:
            raise NotFoundError(f"no spread on credit file {credit_file_id}")
        global_pack = (
            self.store.get_computation_pack(report.global_pack_id)
            if report.global_pack_id
            else None
        )
        composed = draft_memo(
            credit_file,
            spread,
            pack,
            report,
            global_pack=global_pack,
            composer=self._memo_composer(
                old.composer_profile, credit_file_id
            ),
            rating=self._current_rating_facts(credit_file_id),
        )
        sequence = 1 + sum(
            1
            for m in self.store.list_memos(credit_file_id)
            if m.supersedes_memo_id == old.memo_id and m.status == STATUS_DRAFT
        )
        descriptor = {
            "recomposes_memo_id": old.memo_id,
            "sequence": sequence,
            "credit_file_id": composed.credit_file_id,
            "spread_id": composed.spread_id,
            "computation_pack_id": composed.computation_pack_id,
            "global_pack_id": composed.global_pack_id,
            "policy_report_id": composed.policy_report_id,
            "composer_profile": composed.composer_profile,
            "sections": [s.model_dump(mode="json") for s in composed.sections],
            "verification": composed.verification.model_dump(mode="json"),
        }
        draft = composed.model_copy(update={
            "memo_id": f"memo-{_canonical_hash(descriptor)[:16]}",
            "supersedes_memo_id": old.memo_id,
            "created_at": utcnow(),
        })
        self.store.save_memo(draft)
        self._record_review_event(MemoReviewEvent(
            credit_file_id=credit_file_id,
            memo_id=draft.memo_id,
            action="recomposed",
            actor_id=actor,
            actor_role=actor_role,
            note=f"recomposition of returned memo {old.memo_id}",
        ))
        self._audit(
            credit_file_id,
            "memo_recomposed",
            f"returned memo {old.memo_id} recomposed into draft "
            f"{draft.memo_id} by {actor} ({actor_role})",
            actor=actor,
        )
        return draft

    def get_memo_diff(self, credit_file_id: str, memo_id: str) -> MemoDiff:
        """The diff recorded when `memo_id` was approved — the
        draft-vs-final record of its officer gate."""
        self._get_memo_raw(credit_file_id, memo_id)
        diff = self.store.get_memo_diff_by_final(memo_id)
        if diff is None or diff.credit_file_id != credit_file_id:
            raise NotFoundError(
                f"no officer diff recorded for memo {memo_id}"
            )
        return diff

    def memo_history(self, credit_file_id: str, memo_id: str) -> dict:
        """A memo's version lineage and its review events.

        Versions: the chain of memo artefacts containing this one
        (the memo it supersedes, up to the root draft, and every
        artefact that supersedes it in turn), each with its
        derived review status. Events: the gate actions recorded
        on this memo, in order. Diff: the approval diff, when this
        memo is an officer-final artefact.
        """
        memo = self._get_memo_raw(credit_file_id, memo_id)
        all_memos = self.store.list_memos(credit_file_id)
        by_id = {m.memo_id: m for m in all_memos}
        root = memo
        seen: set[str] = set()
        while (
            root.supersedes_memo_id
            and root.supersedes_memo_id in by_id
            and root.supersedes_memo_id not in seen
        ):
            seen.add(root.memo_id)
            root = by_id[root.supersedes_memo_id]
        chain = {root.memo_id}
        frontier = [root.memo_id]
        while frontier:
            nxt = [
                m.memo_id
                for m in all_memos
                if m.supersedes_memo_id in frontier
                and m.memo_id not in chain
            ]
            chain.update(nxt)
            frontier = nxt
        versions = [
            {
                "memo_id": m.memo_id,
                "status": self._status_of(m),
                "supersedes_memo_id": m.supersedes_memo_id,
                "reviewed_by": m.reviewed_by,
                "composer_profile": m.composer_profile,
                "created_at": m.created_at,
            }
            for m in all_memos
            if m.memo_id in chain
        ]
        # Events recorded on this memo, plus the approval event
        # that created it if it is an officer-final artefact (that
        # event is recorded on the draft, naming this memo as its
        # result).
        events = [
            e
            for e in self.store.list_memo_review_events(credit_file_id)
            if e.memo_id == memo_id or e.resulting_memo_id == memo_id
        ]
        diff = self.store.get_memo_diff_by_final(memo_id)
        return {
            "memo_id": memo_id,
            "status": self._status_of(memo),
            "versions": versions,
            "events": events,
            "diff": (
                diff
                if diff is not None and diff.credit_file_id == credit_file_id
                else None
            ),
            "committee_decisions": self.store.list_committee_decisions(
                credit_file_id, memo_id
            ),
        }

    # -- the committee gate (round 7) --------------------------------
    def _require_decidable_memo(
        self, credit_file_id: str, memo_id: str
    ) -> CreditMemo:
        """The artefact the committee may act on: the officer-final
        memo itself — not its draft (the draft was superseded by
        it), not a returned or still-draft memo."""
        memo = self._get_memo_raw(credit_file_id, memo_id)
        superseded_by = next(
            (
                m.memo_id
                for m in self.store.list_memos(credit_file_id)
                if m.supersedes_memo_id == memo_id
            ),
            None,
        )
        if superseded_by is not None:
            raise ValidationError(
                f"memo {memo_id} has been superseded by "
                f"{superseded_by}: the committee decides the "
                "current officer-final memo, not a superseded "
                "version"
            )
        if memo.status != STATUS_APPROVED:
            raise ValidationError(
                f"memo {memo_id} is a {memo.status} artefact; the "
                "committee acts only on the officer-final memo "
                "(officer_approved) — the officer gate decides first"
            )
        return memo

    def convene_committee(
        self,
        credit_file_id: str,
        memo_id: str,
        *,
        actor: str,
        actor_role: str,
        as_of: date | None = None,
    ) -> CreditMemo:
        """Bring an officer-final memo before the committee.

        Lawful from officer_approved (first presentment) and from
        deferred (re-presentment after the officer has addressed
        the deferral's requirements). Convening is an event on the
        memo's stream — the derived status becomes in_committee —
        and the decision is a separate act (decide_committee).

        Readiness gate (round 9, design law 7): readiness is
        recomputed HERE, at convene time, over the memo's pinned
        artefacts and the file as it stands today — a report minted
        at approval (or any earlier moment) cannot bless this
        transition. Any blocker — a missing required item, a stale
        one, a figure or test that cannot be evaluated for want of
        an input — refuses the convene, and the refusal names every
        blocker. There is no waiver: the block clears only when the
        input itself is received or refreshed.
        """
        require_identity(actor, actor_role, what="convening the committee")
        memo = self._require_decidable_memo(credit_file_id, memo_id)
        status = self._status_of(memo)
        if status not in (STATUS_APPROVED, "deferred"):
            raise ValidationError(
                f"memo {memo_id} is {status}; only an "
                "officer-approved memo awaiting the committee, or "
                "a deferred memo being re-presented, can be convened"
            )
        readiness = self._store_readiness(self._readiness_for_inputs(
            self._memo_inputs(memo), as_of=as_of or date.today()
        ))
        if readiness.blockers:
            raise ValidationError(
                readiness_refusal_message(readiness, memo_id=memo_id)
            )
        self._record_review_event(MemoReviewEvent(
            credit_file_id=credit_file_id,
            memo_id=memo_id,
            action="committee_convened",
            actor_id=actor,
            actor_role=actor_role,
        ))
        self._audit(
            credit_file_id,
            "committee_convened",
            f"officer-final memo {memo_id} brought before the "
            f"credit committee by {actor} ({actor_role})"
            + (" (re-presentment)" if status == "deferred" else ""),
            actor=actor,
        )
        return self.get_memo(credit_file_id, memo_id)

    def decide_committee(
        self,
        credit_file_id: str,
        memo_id: str,
        *,
        recorder_id: str,
        recorder_role: str,
        members: list[CommitteeMember],
        outcome: str,
        quorum_required: int = 2,
        conditions: list[CommitteeCondition] | None = None,
        adverse_reasons: list[AdverseActionReason] | None = None,
        deferral_reasons: list[str] | None = None,
        representment_requirements: list[str] | None = None,
        note: str = "",
    ) -> CommitteeDecision:
        """Record the committee's decision on a convened memo.

        The decision is one stored record: the outcome, the named
        members and the quorum they met, the recorder (one of the
        members), and the outcome's own payload — structured
        conditions for a conditional approval, coded tied reasons
        for a decline, coded reasons + named re-presentment
        requirements for a deferral. Every discipline in
        committee.py runs before anything is stored: a refused
        decision records nothing, and the memo stays in committee.
        """
        require_identity(recorder_id, recorder_role,
                         what="a committee decision")
        memo = self._require_decidable_memo(credit_file_id, memo_id)
        status = self._status_of(memo)
        if status != STATUS_IN_COMMITTEE:
            raise ValidationError(
                f"memo {memo_id} is {status}; the committee "
                "decides only a memo that stands convened before "
                "it (in_committee) — a decided memo cannot be "
                "decided again"
            )
        if outcome not in OUTCOMES:
            raise ValidationError(
                f"unknown committee outcome {outcome!r}; outcomes "
                f"are {sorted(OUTCOMES)}"
            )
        validate_quorum(
            members,
            quorum_required,
            recorder_id=recorder_id,
            officer_id=memo.reviewed_by,
        )
        conditions = list(conditions or [])
        adverse_reasons = list(adverse_reasons or [])
        deferral_reasons = list(deferral_reasons or [])
        representment_requirements = list(representment_requirements or [])
        inputs = self._memo_inputs(memo)
        stored_conditions: list[CommitteeCondition] = []
        if outcome == "approve_with_conditions":
            if not conditions:
                raise ValidationError(
                    "an approve_with_conditions decision requires "
                    "at least one structured condition: conditions "
                    "are records, not prose in a note"
                )
            numbered = [
                c.model_copy(update={
                    "condition_id": c.condition_id or f"cond-{i + 1}",
                })
                for i, c in enumerate(conditions)
            ]
            stored_conditions = validate_conditions(numbered, inputs)
            # Round 10: an approval's conditions become covenants.
            # The scheduler must be able to read every condition's
            # cadence as a frequency BEFORE the decision is stored —
            # a condition that cannot be scheduled refuses the
            # decision here, while nothing has been recorded, not
            # the covenant set after it.
            for condition in stored_conditions:
                if condition.type in (
                    "financial_covenant", "reporting_requirement"
                ):
                    frequency_from_cadence(
                        condition.due_cadence,
                        what=(
                            f"committee condition "
                            f"{condition.condition_id!r}"
                        ),
                    )
        elif conditions:
            raise ValidationError(
                f"a {outcome} decision carries no conditions; "
                "conditions belong to approve_with_conditions"
            )
        if outcome == "decline":
            validate_adverse_reasons(
                adverse_reasons,
                inputs.policy_report,
                memo,
                inputs.credit_file,
            )
        elif adverse_reasons:
            raise ValidationError(
                f"a {outcome} decision carries no adverse-action "
                "reasons; coded reasons belong to a decline "
                "(design law 3)"
            )
        if outcome == "defer":
            validate_deferral(deferral_reasons, representment_requirements, note)
        elif deferral_reasons or representment_requirements:
            raise ValidationError(
                f"a {outcome} decision carries no deferral "
                "reasons or re-presentment requirements; those "
                "belong to a deferral"
            )
        prior = self.store.list_committee_decisions(credit_file_id, memo_id)
        descriptor = {
            "memo_id": memo_id,
            "sequence": len(prior) + 1,
            "outcome": outcome,
            "quorum_required": quorum_required,
            "members": [m.model_dump(mode="json") for m in members],
            "recorded_by": recorder_id,
            "conditions": [
                c.model_dump(mode="json") for c in stored_conditions
            ],
            "adverse_reasons": [
                r.model_dump(mode="json") for r in adverse_reasons
            ],
            "deferral_reasons": deferral_reasons,
            "representment_requirements": representment_requirements,
            "note": note,
        }
        decision = CommitteeDecision(
            decision_id=f"decision-{_canonical_hash(descriptor)[:16]}",
            credit_file_id=credit_file_id,
            memo_id=memo_id,
            outcome=outcome,
            quorum_required=quorum_required,
            members=list(members),
            recorded_by=recorder_id,
            recorded_by_role=recorder_role,
            conditions=stored_conditions,
            adverse_reasons=adverse_reasons if outcome == "decline" else [],
            reason_vocabulary=(
                ADVERSE_REASONS_VERSION if outcome == "decline" else None
            ),
            deferral_reasons=deferral_reasons if outcome == "defer" else [],
            deferral_vocabulary=(
                DEFERRAL_REASONS_VERSION if outcome == "defer" else None
            ),
            representment_requirements=(
                representment_requirements if outcome == "defer" else []
            ),
            note=note,
        )
        self.store.save_committee_decision(decision)
        event_codes = (
            [r.code for r in adverse_reasons]
            if outcome == "decline"
            else deferral_reasons if outcome == "defer" else []
        )
        self._record_review_event(MemoReviewEvent(
            credit_file_id=credit_file_id,
            memo_id=memo_id,
            action="committee_decided",
            actor_id=recorder_id,
            actor_role=recorder_role,
            committee_outcome=outcome,
            reason_codes=event_codes,
            reason_vocabulary=decision.reason_vocabulary
            or decision.deferral_vocabulary,
            note=note,
        ))
        detail = (
            f"committee decision {decision.decision_id} on memo "
            f"{memo_id}: {outcome}, recorded by {recorder_id} "
            f"({recorder_role}) for {len(members)} members "
            f"(quorum {quorum_required})"
        )
        if stored_conditions:
            detail += f"; {len(stored_conditions)} condition(s)"
        if outcome == "decline":
            detail += (
                f"; adverse-action reasons "
                f"({ADVERSE_REASONS_VERSION}): "
                + ", ".join(r.code for r in adverse_reasons)
            )
        if outcome == "defer":
            detail += (
                f"; deferral reasons ({DEFERRAL_REASONS_VERSION}): "
                + ", ".join(deferral_reasons)
                + "; re-presentment requires: "
                + "; ".join(representment_requirements)
            )
        self._audit(credit_file_id, "committee_decision_recorded", detail,
                    actor=recorder_id)
        if outcome in ("approve", "approve_with_conditions"):
            # Round 10 (law 6): an approval starts the facility's
            # monitoring life. The covenant set is created from
            # this decision — its conditions plus the policy
            # template — in the same act, so an approved credit
            # can never exist without its covenant set.
            self._create_covenant_set_for_decision(
                inputs.credit_file, decision, memo, actor=recorder_id
            )
        return decision

    def list_committee_decisions(
        self, credit_file_id: str, memo_id: str
    ) -> list[CommitteeDecision]:
        """Every decision recorded on a memo, in order — across a
        deferral, the superseded deferral first, then the decision
        taken at re-presentment."""
        self._get_memo_raw(credit_file_id, memo_id)
        return self.store.list_committee_decisions(credit_file_id, memo_id)

    def get_committee_decision(
        self, credit_file_id: str, decision_id: str
    ) -> CommitteeDecision:
        self._load(credit_file_id)
        decision = self.store.get_committee_decision(decision_id)
        if decision is None or decision.credit_file_id != credit_file_id:
            raise NotFoundError(
                f"committee decision {decision_id} not found on "
                f"credit file {credit_file_id}"
            )
        return decision

    def committee_state(self, credit_file_id: str, memo_id: str) -> dict:
        """A memo's committee position: its derived status, every
        decision recorded on it, and the latest one."""
        memo = self._get_memo_raw(credit_file_id, memo_id)
        decisions = self.store.list_committee_decisions(
            credit_file_id, memo_id
        )
        return {
            "memo_id": memo_id,
            "status": self._status_of(memo),
            "decisions": decisions,
            "latest_decision": decisions[-1] if decisions else None,
        }

    def get_adverse_action_notice(
        self,
        credit_file_id: str,
        memo_id: str,
        *,
        decision_id: str | None = None,
    ) -> str:
        """The adverse-action notice for a decline on this memo,
        generated from the decision's coded reasons and their ties
        (committee.render_adverse_action_notice). Defaults to the
        memo's latest decline."""
        memo = self._get_memo_raw(credit_file_id, memo_id)
        decisions = self.store.list_committee_decisions(
            credit_file_id, memo_id
        )
        if decision_id is not None:
            decision = next(
                (d for d in decisions if d.decision_id == decision_id),
                None,
            )
            if decision is None:
                raise NotFoundError(
                    f"committee decision {decision_id} not found on "
                    f"memo {memo_id}"
                )
        else:
            declines = [d for d in decisions if d.outcome == "decline"]
            decision = declines[-1] if declines else None
            if decision is None:
                raise NotFoundError(
                    f"no decline recorded on memo {memo_id}: there "
                    "is no adverse-action notice"
                )
        report = self.store.get_policy_report(memo.policy_report_id)
        if report is None:
            raise NotFoundError(
                f"policy report {memo.policy_report_id} not found"
            )
        credit_file = self._load(credit_file_id)
        return render_adverse_action_notice(decision, memo, report, credit_file)

    # -- Covenants (round 10) -----------------------------------------
    #
    # Law 6: covenants are clocks + math. An approval creates the
    # set; the schedule is derived from the set in code, never
    # stored; the tick records first-seen due/overdue crossings on
    # the audit trail; amendments supersede, never edit.

    def record_closing(
        self,
        credit_file_id: str,
        *,
        closing_date: date,
        actor: str,
        actor_role: str,
    ) -> CreditFile:
        """Record the facility's closing date — the anchor fact
        every covenant test date derives from.

        Recording is an audited act by a named actor. Re-recording
        the same date is a no-op. Recording a DIFFERENT date is a
        correction: the new date governs, and the audit names both
        dates — the closing date is never silently edited, the
        same discipline the covenants themselves live under."""
        require_identity(actor, actor_role, what="a closing record")
        credit_file = self._load(credit_file_id)
        if credit_file.closing_date == closing_date:
            return credit_file
        previous = credit_file.closing_date
        updated = credit_file.model_copy(update={
            "closing_date": closing_date,
            "updated_at": utcnow(),
        })
        self.store.save_credit_file(updated)
        if previous is None:
            self._audit(
                credit_file_id,
                "facility_closed",
                f"facility closing recorded: {closing_date.isoformat()} "
                f"by {actor} ({actor_role}); covenant schedules "
                "anchor on this date",
                actor=actor,
            )
        else:
            self._audit(
                credit_file_id,
                "closing_date_corrected",
                f"closing date corrected from {previous.isoformat()} "
                f"to {closing_date.isoformat()} by {actor} "
                f"({actor_role}); schedules re-derive from the "
                "corrected date",
                actor=actor,
            )
        return updated

    def _create_covenant_set_for_decision(
        self,
        credit_file: CreditFile,
        decision: CommitteeDecision,
        memo: CreditMemo,
        *,
        actor: str,
    ) -> CovenantSet:
        """Create (once) the covenant set an approval brings into
        being. Insert-once end to end: a second call for the same
        decision returns the stored set unchanged."""
        existing = [
            s for s in self.store.list_covenant_sets(credit_file.credit_file_id)
            if s.decision_id == decision.decision_id
        ]
        if existing:
            return existing[0]
        covenant_set = build_covenant_set(
            credit_file,
            decision,
            policy_version=memo.policy_version,
            policy_pack_id=memo.policy_pack_id,
            created_by=actor,
        )
        self.store.save_covenant_set(covenant_set)
        detail = (
            f"covenant set {covenant_set.set_id} created from "
            f"decision {decision.decision_id} (memo {decision.memo_id}) "
            f"under policy pack {covenant_set.policy_pack_id} "
            f"{covenant_set.policy_version}: "
            f"{len(covenant_set.covenants)} covenant(s), "
            f"{len(covenant_set.deliverables)} reporting "
            f"deliverable(s); state {covenant_set.state}"
        )
        if covenant_set.state == "awaiting_closing_date":
            detail += (
                "; no closing date on file — the set exists, its "
                "schedule waits for the closing record"
            )
        self._audit(
            credit_file.credit_file_id,
            "covenant_set_created",
            detail,
            actor=actor,
        )
        return covenant_set

    def create_covenant_set(
        self,
        credit_file_id: str,
        *,
        decision_id: str,
        actor: str,
    ) -> CovenantSet:
        """Create the covenant set for a recorded approval —
        the explicit form of the act decide_committee performs
        automatically. Refuses, by name, a decision that is not
        an approval of a memo on this file."""
        credit_file = self._load(credit_file_id)
        decision = self.store.get_committee_decision(decision_id)
        if decision is None or decision.credit_file_id != credit_file_id:
            raise NotFoundError(
                f"committee decision {decision_id} not found on "
                f"credit file {credit_file_id}"
            )
        memo = self._get_memo_raw(credit_file_id, decision.memo_id)
        return self._create_covenant_set_for_decision(
            credit_file, decision, memo, actor=actor
        )

    def list_covenant_sets(
        self, credit_file_id: str
    ) -> list[CovenantSet]:
        self._load(credit_file_id)
        return self.store.list_covenant_sets(credit_file_id)

    def get_covenant_set(
        self, credit_file_id: str, set_id: str
    ) -> CovenantSet:
        self._load(credit_file_id)
        covenant_set = self.store.get_covenant_set(set_id)
        if covenant_set is None or covenant_set.credit_file_id != credit_file_id:
            raise NotFoundError(
                f"covenant set {set_id} not found on credit file "
                f"{credit_file_id}"
            )
        return covenant_set

    def list_covenant_amendments(
        self, credit_file_id: str
    ) -> list[CovenantAmendment]:
        self._load(credit_file_id)
        return self.store.list_covenant_amendments(credit_file_id)

    def _effective_set(
        self, credit_file: CreditFile, covenant_set: CovenantSet
    ) -> CovenantSet:
        """The set as it stands for scheduling: its stored record,
        with the file's closing date applied when the set was
        created before one existed. The stored artefact is never
        rewritten — the derivation reads the current fact, and the
        set's own pin (the decision, the pack) is untouched."""
        if covenant_set.closing_date is None and credit_file.closing_date:
            return covenant_set.model_copy(update={
                "state": "active",
                "closing_date": credit_file.closing_date,
            })
        return covenant_set

    def _spread_years(self, credit_file_id: str) -> set[int]:
        """The fiscal years the file's current spread covers —
        the availability fact the schedule derives states from."""
        try:
            spread = self.get_spread(credit_file_id)
        except NotFoundError:
            return set()
        return set(spread.periods)

    def covenant_schedule(
        self,
        credit_file_id: str,
        *,
        as_of: date,
        set_id: str | None = None,
    ) -> dict:
        """One file's covenant schedule at `as_of`: the set (with
        its current, amendment-aware covenants), and every derived
        instance — covenant tests and reporting deliverables —
        with its state and the inputs it needs named."""
        credit_file = self._load(credit_file_id)
        sets = self.store.list_covenant_sets(credit_file_id)
        if set_id is not None:
            sets = [s for s in sets if s.set_id == set_id]
            if not sets:
                raise NotFoundError(
                    f"covenant set {set_id} not found on credit "
                    f"file {credit_file_id}"
                )
        amendments = self.store.list_covenant_amendments(credit_file_id)
        spread_years = self._spread_years(credit_file_id)
        views = []
        for covenant_set in sets:
            effective = self._effective_set(credit_file, covenant_set)
            current = current_covenants(effective, amendments)
            derivable = effective.model_copy(update={
                "covenants": current,
            })
            instances = derive_schedule(
                derivable, as_of=as_of, spread_years=spread_years
            )
            views.append({
                "covenant_set": effective,
                "current_covenants": current,
                "instances": instances,
            })
        return {
            "credit_file_id": credit_file_id,
            "borrower": credit_file.borrower.legal_name,
            "as_of": as_of,
            "sets": views,
        }

    def covenant_due_work(self, *, as_of: date) -> list[dict]:
        """The portfolio due-work queue at `as_of`: every instance
        standing at overdue / due / awaiting_inputs / due_soon
        across every credit file, in the queue's order — what is
        due, for which borrower, needing which inputs."""
        queue: list[dict] = []
        for credit_file in self.store.list_credit_files():
            schedule = self.covenant_schedule(
                credit_file.credit_file_id, as_of=as_of
            )
            instances = [
                instance
                for view in schedule["sets"]
                for instance in view["instances"]
            ]
            for instance in due_work(instances, as_of=as_of):
                queue.append({
                    "borrower": credit_file.borrower.legal_name,
                    "credit_file_id": credit_file.credit_file_id,
                    "kind": "covenant_schedule",
                    "instance": instance,
                })
        order = {"overdue": 0, "due": 1, "awaiting_inputs": 2, "due_soon": 3}
        queue.sort(key=lambda entry: (
            order[entry["instance"].state],
            entry["instance"].test_date,
            entry["instance"].name,
        ))
        # Round 12: annual review instances standing at work join
        # the same queue surface — the monitoring desk reads one
        # list of what is owed. They ride after the covenant
        # entries, worst review first, each naming its review.
        queue.extend(
            {
                "borrower": entry["borrower"],
                "credit_file_id": entry["credit_file_id"],
                "kind": "annual_review",
                "review_instance": entry["instance"],
            }
            for entry in self.review_due_work(as_of=as_of)
        )
        return queue

    def tick_covenants(
        self, *, as_of: date, credit_file_id: str | None = None
    ) -> dict:
        """The scheduler tick at `as_of`: walk every set's derived
        schedule and record, on the credit file's audit trail, the
        first sighting of each instance standing at due or
        overdue. Idempotent by construction: a crossing already on
        the audit trail is never recorded twice, so the tick can
        run as often as the bank likes."""
        files = (
            [self._load(credit_file_id)]
            if credit_file_id is not None
            else self.store.list_credit_files()
        )
        recorded: list[dict] = []
        checked = 0
        for credit_file in files:
            schedule = self.covenant_schedule(
                credit_file.credit_file_id, as_of=as_of
            )
            seen = {
                (event.action, event.detail)
                for event in self.store.list_audit(credit_file.credit_file_id)
            }
            for view in schedule["sets"]:
                checked += 1
                for instance in view["instances"]:
                    if instance.state not in ("due", "overdue"):
                        continue
                    kind_word = (
                        "covenant test"
                        if instance.kind == "covenant_test"
                        else "reporting deliverable"
                    )
                    action = (
                        f"{'covenant_test' if instance.kind == 'covenant_test' else 'covenant_deliverable'}"
                        f"_{instance.state}"
                    )
                    detail = (
                        f"{kind_word} {instance.instance_id} "
                        f"({instance.name}, {instance.period_label}, "
                        f"test date {instance.test_date.isoformat()}) "
                        f"first seen {instance.state} at as_of "
                        f"{as_of.isoformat()}"
                    )
                    if (action, detail) in seen:
                        continue
                    self._audit(
                        credit_file.credit_file_id, action, detail
                    )
                    seen.add((action, detail))
                    recorded.append({
                        "credit_file_id": credit_file.credit_file_id,
                        "borrower": credit_file.borrower.legal_name,
                        "instance_id": instance.instance_id,
                        "kind": instance.kind,
                        "name": instance.name,
                        "state": instance.state,
                        "test_date": instance.test_date,
                        "action": action,
                    })
        # Round 11: the tick also executes the tests that stand
        # due — re-spreading and recomputing through the existing
        # engines, recording results, opening breach cases on
        # failures. Idempotent the same way: a test already
        # recorded over the same inputs records nothing twice.
        tests_recorded: list[dict] = []
        breaches_opened: list[dict] = []
        for credit_file in files:
            ran = self._run_due_tests(credit_file, as_of=as_of)
            tests_recorded.extend(ran["results"])
            breaches_opened.extend(ran["breaches"])
        # Round 12: the tick also opens the annual reviews that
        # stand due — assembling each one's pack from the stored
        # artefacts, so the officer's work is waiting, evidenced.
        # Idempotent the same way: an instance with a pack already
        # assembled is not opened twice.
        reviews_opened: list[dict] = []
        for credit_file in files:
            reviews_opened.extend(
                self._open_due_reviews_for(credit_file, as_of=as_of)
            )
        return {
            "as_of": as_of,
            "sets_checked": checked,
            "crossings_recorded": recorded,
            "tests_recorded": tests_recorded,
            "breaches_opened": breaches_opened,
            "reviews_opened": reviews_opened,
        }

    # ------------------------------------------------------------------
    # Round 11: covenant test execution and breach cases. Round
    # 10 derived the schedule; this section runs the tests on it.
    # A test re-spreads the period through the existing spreading
    # engine and recomputes the covenant's metric through the
    # existing computation engine — no new math lives here; the
    # figure is read out of the stored pack's own covenant result
    # and compared against the covenant's threshold. A failure
    # opens a breach case automatically; resolutions are coded
    # acts of named officers, recorded on the case and the audit
    # trail. Nothing here deletes or silently closes anything.
    # ------------------------------------------------------------------

    def _current_spread(self, credit_file_id: str):
        """The file's current spread — rebuilt, per the pipeline's
        own convention, so standing and readiness can never lag a
        restated document or a fresh extraction behind the store.
        Falls back to the latest stored spread when a rebuild is
        impossible (a file with nothing extracted yet)."""
        try:
            return self.get_spread(credit_file_id)
        except (ValidationError, NotFoundError):
            return self.store.get_latest_spread(credit_file_id)

    def _head_result(
        self, results: list[CovenantTestResult], instance_id: str
    ) -> CovenantTestResult | None:
        """The current result for one instance: the latest
        recorded result no other recorded result supersedes."""
        mine = [r for r in results if r.instance_id == instance_id]
        if not mine:
            return None
        superseded = {
            r.supersedes_result_id for r in mine if r.supersedes_result_id
        }
        heads = [r for r in mine if r.result_id not in superseded]
        return heads[-1] if heads else mine[-1]

    def _execute_covenant_test(
        self,
        credit_file: CreditFile,
        view: dict,
        instance: CovenantTestInstance,
        *,
        as_of: date,
        strict: bool,
    ) -> tuple[CovenantTestResult | None, bool]:
        """Run (or stand) one covenant test instance. Returns the
        instance's head result and whether this call recorded it.
        `strict` is the single-instance API path: an instance that
        is not yet due is refused by name. The scheduler path is
        lenient: not-due instances are simply skipped."""
        credit_file_id = credit_file.credit_file_id
        covenant = next(
            c for c in view["current_covenants"]
            if c.covenant_id == instance.covenant_id
        )
        results = self.store.list_covenant_test_results(credit_file_id)
        head = self._head_result(results, instance.instance_id)
        spread = self._current_spread(credit_file_id)
        inputs_ready = (
            instance.fiscal_year is not None
            and spread is not None
            and instance.fiscal_year in spread.periods
        )
        supersedes: str | None = None
        if head is not None and head.fiscal_year != instance.fiscal_year:
            # A point-in-time instance whose current period moved
            # when newer statements arrived: the recorded result
            # tested the period that was current on its day, and
            # the instance now owes a test over the new period.
            supersedes = head.result_id
        elif head is not None:
            if head.outcome == TEST_AWAITING_INPUTS:
                if not inputs_ready:
                    return head, False
                supersedes = head.result_id
            elif result_standing(head, spread) == (
                RESULT_SUPERSEDED_BY_RESTATEMENT
            ):
                self._audit(
                    credit_file_id, "covenant_retest_scheduled",
                    f"test result {head.result_id} for "
                    f"{instance.name} ({instance.period_label}) is "
                    f"superseded by a restatement of its period: a "
                    f"re-test is scheduled",
                )
                supersedes = head.result_id
            else:
                return head, False
        elif instance.state not in ("due", "overdue", "awaiting_inputs"):
            if strict:
                raise ValidationError(
                    f"covenant test instance {instance.instance_id} "
                    f"({instance.name}, {instance.period_label}) "
                    f"stands at {instance.state!r} at "
                    f"{as_of.isoformat()}: a test runs when it is "
                    "due, not before"
                )
            return None, False

        if not inputs_ready:
            # The statements have not arrived: the test stands at
            # awaiting_inputs, blocked on the borrower — recorded,
            # so the wait is on the file, naming what is missing.
            result = CovenantTestResult(
                result_id=test_result_id(
                    instance_id=instance.instance_id,
                    covenant_id=covenant.covenant_id,
                    spread_id=None,
                    computation_pack_id=None,
                    outcome=TEST_AWAITING_INPUTS,
                    supersedes_result_id=supersedes,
                ),
                credit_file_id=credit_file_id,
                set_id=view["covenant_set"].set_id,
                instance_id=instance.instance_id,
                covenant_id=covenant.covenant_id,
                covenant_version=covenant.version,
                covenant_name=covenant.name,
                metric_key=covenant.metric_key,
                operator=covenant.operator,
                threshold=covenant.threshold,
                test_date=instance.test_date,
                period_label=instance.period_label,
                fiscal_year=instance.fiscal_year,
                outcome=TEST_AWAITING_INPUTS,
                missing_inputs=list(instance.required_inputs),
                supersedes_result_id=supersedes,
                is_retest=supersedes is not None,
                as_of=as_of,
            )
            created = (
                self.store.get_covenant_test_result(result.result_id)
                is None
            )
            if created:
                self.store.save_covenant_test_result(result)
                self._audit(
                    credit_file_id, "covenant_test_awaiting_inputs",
                    f"covenant test {instance.instance_id} "
                    f"({instance.name}, {instance.period_label}) "
                    f"cannot run: awaiting inputs — "
                    f"{'; '.join(result.missing_inputs)}",
                )
            return result, created

        # The inputs exist: re-spread and recompute through the
        # existing engines, with this covenant as the definition,
        # tested over the instance's fiscal year.
        definition = CovenantDefinition(
            name=covenant.name,
            metric_key=covenant.metric_key,
            operator=covenant.operator,
            threshold=covenant.threshold,
            fiscal_year=instance.fiscal_year,
        )
        pack = self.compute_metrics(
            credit_file_id, covenant_definitions=[definition]
        )
        spread = next(
            s for s in self.store.list_spreads(credit_file_id)
            if s.spread_id == pack.spread_id
        )
        engine_result = next(
            (
                cr for cr in pack.covenant_results
                if cr.name == covenant.name
                and cr.metric_key == covenant.metric_key
                and cr.fiscal_year == instance.fiscal_year
            ),
            None,
        )
        if engine_result is None:  # pragma: no cover - engine contract
            raise ValidationError(
                f"the computation pack {pack.pack_id} carries no "
                f"result for covenant {covenant.name!r}: the test "
                "cannot be recorded without the engine's figure"
            )
        if engine_result.status == "not_computable":
            outcome = TEST_NOT_EVALUABLE
            figure = None
            headroom = None
            missing = list(engine_result.missing_inputs)
        else:
            figure = engine_result.value
            headroom = engine_result.headroom
            missing = []
            outcome = (
                TEST_PASS
                if compare(figure, covenant.operator, covenant.threshold)
                else TEST_FAIL
            )
        result = CovenantTestResult(
            result_id=test_result_id(
                instance_id=instance.instance_id,
                covenant_id=covenant.covenant_id,
                spread_id=pack.spread_id,
                computation_pack_id=pack.pack_id,
                outcome=outcome,
                supersedes_result_id=supersedes,
            ),
            credit_file_id=credit_file_id,
            set_id=view["covenant_set"].set_id,
            instance_id=instance.instance_id,
            covenant_id=covenant.covenant_id,
            covenant_version=covenant.version,
            covenant_name=covenant.name,
            metric_key=covenant.metric_key,
            operator=covenant.operator,
            threshold=covenant.threshold,
            test_date=instance.test_date,
            period_label=instance.period_label,
            fiscal_year=instance.fiscal_year,
            spread_id=pack.spread_id,
            computation_pack_id=pack.pack_id,
            figure_value=figure,
            headroom=headroom,
            outcome=outcome,
            missing_inputs=missing,
            period_sources=period_sources_for(spread, instance.fiscal_year),
            supersedes_result_id=supersedes,
            is_retest=supersedes is not None,
            as_of=as_of,
        )
        created = (
            self.store.get_covenant_test_result(result.result_id) is None
        )
        if not created:
            return result, False
        self.store.save_covenant_test_result(result)
        figure_word = (
            f"figure {figure}" if figure is not None else "no figure"
        )
        self._audit(
            credit_file_id, "covenant_test_recorded",
            f"covenant test {instance.instance_id} "
            f"({instance.name}, {instance.period_label}) recorded "
            f"{outcome}: {figure_word} against "
            f"{covenant.operator} {covenant.threshold} "
            f"(pack {pack.pack_id}, spread {pack.spread_id})",
        )
        if outcome == TEST_FAIL:
            self._open_breach(credit_file, view, covenant, result)
        return result, True

    def _open_breach(
        self,
        credit_file: CreditFile,
        view: dict,
        covenant: Covenant,
        result: CovenantTestResult,
    ) -> BreachCase:
        """A failing test opens its breach case — automatically,
        in the same act, so a failure without a case cannot
        exist. One breach per failing result, derived from it."""
        breach = BreachCase(
            breach_id=breach_id_for(result.result_id),
            credit_file_id=credit_file.credit_file_id,
            borrower_name=credit_file.borrower.legal_name,
            set_id=view["covenant_set"].set_id,
            covenant_id=covenant.covenant_id,
            covenant_version=covenant.version,
            covenant_name=covenant.name,
            metric_key=covenant.metric_key,
            operator=covenant.operator,
            threshold=covenant.threshold,
            figure_value=result.figure_value,
            test_result_id=result.result_id,
            instance_id=result.instance_id,
            test_date=result.test_date,
            period_label=result.period_label,
            cure_deadline=(
                result.test_date + timedelta(days=covenant.grace_days)
            ),
            events=[BreachEvent(
                action="opened",
                actor_id="covenant-scheduler",
                actor_role="system",
                note=(
                    f"opened by failing covenant test result "
                    f"{result.result_id}: figure "
                    f"{result.figure_value} against "
                    f"{covenant.operator} {covenant.threshold}; "
                    f"cure period {covenant.grace_days} days"
                ),
            )],
        )
        self.store.save_breach_case(breach)
        self._audit(
            credit_file.credit_file_id, "breach_opened",
            f"breach {breach.breach_id} opened on "
            f"{credit_file.borrower.legal_name}: {covenant.name} "
            f"({result.period_label}) figure {result.figure_value} "
            f"against {covenant.operator} {covenant.threshold}; "
            f"cure deadline {breach.cure_deadline.isoformat()}",
        )
        return breach

    def _run_due_tests(
        self, credit_file: CreditFile, *, as_of: date
    ) -> dict:
        """Every due covenant test on one file, executed: results
        recorded, breaches opened. Deliverable instances are not
        tests and are never executed here."""
        schedule = self.covenant_schedule(
            credit_file.credit_file_id, as_of=as_of
        )
        recorded: list[dict] = []
        breaches: list[dict] = []
        for view in schedule["sets"]:
            for instance in view["instances"]:
                if instance.kind != "covenant_test":
                    continue
                result, created = self._execute_covenant_test(
                    credit_file, view, instance,
                    as_of=as_of, strict=False,
                )
                if result is None or not created:
                    continue
                recorded.append({
                    "credit_file_id": credit_file.credit_file_id,
                    "borrower": credit_file.borrower.legal_name,
                    "result": result,
                })
                if result.outcome == TEST_FAIL:
                    breach = self.store.get_breach_case(
                        breach_id_for(result.result_id)
                    )
                    if breach is not None:
                        breaches.append({
                            "credit_file_id": credit_file.credit_file_id,
                            "borrower": credit_file.borrower.legal_name,
                            "breach": breach,
                        })
        return {"results": recorded, "breaches": breaches}

    def run_due_covenant_tests(
        self, credit_file_id: str, *, as_of: date
    ) -> dict:
        """Run every covenant test standing due on one file at
        `as_of` (the API's run-tests action; the scheduler tick
        runs the same machinery across the portfolio)."""
        credit_file = self._load(credit_file_id)
        return self._run_due_tests(credit_file, as_of=as_of)

    def run_covenant_test(
        self,
        credit_file_id: str,
        instance_id: str,
        *,
        as_of: date,
    ) -> CovenantTestResult:
        """Run one covenant test instance now. An instance that is
        not yet due is refused by name — tests are not run early
        to make a quarter look clean."""
        credit_file = self._load(credit_file_id)
        schedule = self.covenant_schedule(credit_file_id, as_of=as_of)
        for view in schedule["sets"]:
            for instance in view["instances"]:
                if instance.instance_id != instance_id:
                    continue
                if instance.kind != "covenant_test":
                    raise ValidationError(
                        f"instance {instance_id} is a reporting "
                        "deliverable, not a covenant test: "
                        "deliverables are received, not run"
                    )
                result, _created = self._execute_covenant_test(
                    credit_file, view, instance,
                    as_of=as_of, strict=True,
                )
                return result
        raise NotFoundError(
            f"covenant test instance {instance_id} not found on "
            f"credit file {credit_file_id}"
        )

    # -- Results and standing -------------------------------------

    def covenant_test_results(
        self, credit_file_id: str
    ) -> list[dict]:
        """Every recorded test result on a file, each carrying
        its derived standing against the file's current spread:
        a result whose period has been restated reads
        superseded_by_restatement — flagged, never rewritten."""
        self._load(credit_file_id)
        spread = self._current_spread(credit_file_id)
        views = []
        for result in self.store.list_covenant_test_results(credit_file_id):
            view = result.model_dump(mode="json")
            view["standing"] = result_standing(result, spread)
            views.append(view)
        return views

    def covenant_history(
        self, credit_file_id: str, covenant_id: str, *, as_of: date
    ) -> dict:
        """One covenant's full history: every recorded result
        across its versions (amendments included), the breaches
        they opened, and each result's restatement standing."""
        credit_file = self._load(credit_file_id)
        sets = self.store.list_covenant_sets(credit_file_id)
        amendments = self.store.list_covenant_amendments(credit_file_id)
        root = lineage_root(covenant_id, sets, amendments)
        if root is None:
            raise NotFoundError(
                f"covenant {covenant_id} not found on credit file "
                f"{credit_file_id}"
            )
        lineage_ids = {
            result.covenant_id
            for result in self.store.list_covenant_test_results(
                credit_file_id
            )
            if lineage_root(result.covenant_id, sets, amendments) == root
        } | {covenant_id}
        spread = self._current_spread(credit_file_id)
        result_views = []
        for result in self.store.list_covenant_test_results(credit_file_id):
            if result.covenant_id not in lineage_ids:
                continue
            view = result.model_dump(mode="json")
            view["standing"] = result_standing(result, spread)
            result_views.append(view)
        result_views.sort(key=lambda v: (v["test_date"], v["result_id"]))
        breach_views = [
            self._breach_view(breach, as_of=as_of)
            for breach in self.store.list_breach_cases(credit_file_id)
            if breach.covenant_id in lineage_ids
        ]
        return {
            "credit_file_id": credit_file_id,
            "borrower": credit_file.borrower.legal_name,
            "covenant_id": covenant_id,
            "lineage_root": root,
            "results": result_views,
            "breaches": breach_views,
        }

    # -- Breach cases ----------------------------------------------

    def _breach_view(self, breach: BreachCase, *, as_of: date) -> dict:
        view = breach.model_dump(mode="json")
        view["days_in_breach"] = (as_of - breach.test_date).days
        view["days_to_cure_deadline"] = (
            breach.cure_deadline - as_of
        ).days
        view["cure_overdue"] = (
            breach.state
            not in (BREACH_CURED, BREACH_WAIVED, BREACH_DEFAULTED)
            and as_of > breach.cure_deadline
        )
        return view

    def list_breach_cases(
        self, *, as_of: date, credit_file_id: str | None = None
    ) -> list[dict]:
        """The breach list — one file's, or the portfolio's —
        with ageing against each cure deadline. Open breaches
        sort first, oldest failure first: the list is a work
        queue, not an archive."""
        if credit_file_id is not None:
            self._load(credit_file_id)
        breaches = self.store.list_breach_cases(credit_file_id)
        views = [self._breach_view(b, as_of=as_of) for b in breaches]
        views.sort(key=lambda v: (
            v["state"] in (BREACH_CURED, BREACH_WAIVED, BREACH_DEFAULTED),
            v["test_date"],
            v["breach_id"],
        ))
        return views

    def get_breach_case(
        self, credit_file_id: str, breach_id: str, *, as_of: date
    ) -> dict:
        return self._breach_view(
            self._load_breach(credit_file_id, breach_id), as_of=as_of
        )

    def _load_breach(
        self, credit_file_id: str, breach_id: str
    ) -> BreachCase:
        self._load(credit_file_id)
        breach = self.store.get_breach_case(breach_id)
        if breach is None or breach.credit_file_id != credit_file_id:
            raise NotFoundError(
                f"breach case {breach_id} not found on credit "
                f"file {credit_file_id}"
            )
        return breach

    def _record_breach_act(
        self,
        breach: BreachCase,
        *,
        action: str,
        target_state: str,
        actor_id: str,
        actor_role: str,
        reason_code: str | None = None,
        note: str = "",
        test_result_id: str | None = None,
        decision_id: str | None = None,
    ) -> BreachCase:
        validate_breach_transition(
            state=breach.state, target=target_state, what=action
        )
        breach.events.append(BreachEvent(
            action=action,
            actor_id=actor_id,
            actor_role=actor_role,
            reason_code=reason_code,
            note=note,
            test_result_id=test_result_id,
            decision_id=decision_id,
        ))
        breach.state = target_state
        self.store.save_breach_case(breach)
        self._audit(
            breach.credit_file_id, f"breach_{action}",
            f"breach {breach.breach_id} ({breach.covenant_name}, "
            f"{breach.period_label}) moved to {target_state} by "
            f"{actor_id} ({actor_role})"
            + (f" under reason {reason_code}" if reason_code else "")
            + (f": {note}" if note else ""),
        )
        return breach

    def review_breach(
        self,
        credit_file_id: str,
        breach_id: str,
        *,
        actor_id: str,
        actor_role: str,
        note: str = "",
    ) -> BreachCase:
        """A named officer takes the breach under review."""
        require_breach_actor(actor_id, actor_role, what="breach review")
        breach = self._load_breach(credit_file_id, breach_id)
        return self._record_breach_act(
            breach, action="under_review",
            target_state=BREACH_UNDER_REVIEW,
            actor_id=actor_id, actor_role=actor_role, note=note,
        )

    def escalate_breach(
        self,
        credit_file_id: str,
        breach_id: str,
        *,
        actor_id: str,
        actor_role: str,
        reason_code: str,
        note: str = "",
    ) -> BreachCase:
        """Escalate the breach to the watchlist queue — a coded
        act by a named officer, never a quiet flag."""
        require_breach_actor(
            actor_id, actor_role, what="breach escalation"
        )
        if reason_code not in BREACH_ESCALATION_REASON_CODES:
            raise ValidationError(
                f"unknown breach escalation reason code "
                f"{reason_code!r}: codes come from the versioned "
                f"vocabulary (breach-resolution-reasons-v1): "
                f"{sorted(BREACH_ESCALATION_REASON_CODES)}"
            )
        breach = self._load_breach(credit_file_id, breach_id)
        return self._record_breach_act(
            breach, action="escalated",
            target_state=BREACH_ESCALATED,
            actor_id=actor_id, actor_role=actor_role,
            reason_code=reason_code, note=note,
        )

    def cure_breach(
        self,
        credit_file_id: str,
        breach_id: str,
        *,
        actor_id: str,
        actor_role: str,
        cure_result_id: str,
        note: str = "",
    ) -> BreachCase:
        """Cure the breach — on the evidence of a later passing
        re-test of the same covenant, linked by result id. The
        cure is validated mechanically: no passing re-test, no
        cure."""
        require_breach_actor(actor_id, actor_role, what="breach cure")
        breach = self._load_breach(credit_file_id, breach_id)
        cure_result = self.store.get_covenant_test_result(cure_result_id)
        sets = self.store.list_covenant_sets(credit_file_id)
        amendments = self.store.list_covenant_amendments(credit_file_id)
        validate_cure(
            cure_result=cure_result,
            breach_covenant_id=breach.covenant_id,
            breach_result_id=breach.test_result_id,
            breach_test_date=breach.test_date,
            sets=sets,
            amendments=amendments,
            credit_file_id=credit_file_id,
        )
        return self._record_breach_act(
            breach, action="cured", target_state=BREACH_CURED,
            actor_id=actor_id, actor_role=actor_role, note=note,
            test_result_id=cure_result_id,
        )

    def waive_breach(
        self,
        credit_file_id: str,
        breach_id: str,
        *,
        actor_id: str,
        actor_role: str,
        decision_id: str,
        note: str = "",
    ) -> BreachCase:
        """Waive the breach — only on a committee decision's
        authority. The decision must be a stored artefact of
        this credit file with an approval outcome; a waiver that
        names no decision is a free-text waiver and is refused."""
        require_breach_actor(actor_id, actor_role, what="breach waiver")
        breach = self._load_breach(credit_file_id, breach_id)
        decision = (
            self.store.get_committee_decision(decision_id)
            if decision_id
            else None
        )
        validate_waiver_decision(
            decision=decision, credit_file_id=credit_file_id
        )
        return self._record_breach_act(
            breach, action="waived", target_state=BREACH_WAIVED,
            actor_id=actor_id, actor_role=actor_role, note=note,
            decision_id=decision_id,
        )

    def default_breach(
        self,
        credit_file_id: str,
        breach_id: str,
        *,
        actor_id: str,
        actor_role: str,
        reason_code: str,
        note: str = "",
    ) -> BreachCase:
        """Move the breach onto the default path — the
        enforcement outcome, reached from review or escalation
        under a coded reason. The breach record stays; the
        facility's handling changes, not its history."""
        require_breach_actor(
            actor_id, actor_role, what="breach default path"
        )
        if reason_code not in BREACH_DEFAULT_REASON_CODES:
            raise ValidationError(
                f"unknown breach default reason code "
                f"{reason_code!r}: codes come from the versioned "
                f"vocabulary (breach-resolution-reasons-v1): "
                f"{sorted(BREACH_DEFAULT_REASON_CODES)}"
            )
        breach = self._load_breach(credit_file_id, breach_id)
        return self._record_breach_act(
            breach, action="defaulted",
            target_state=BREACH_DEFAULTED,
            actor_id=actor_id, actor_role=actor_role,
            reason_code=reason_code, note=note,
        )

    # ------------------------------------------------------------------
    # Round 12: annual review packs. Round 10 set the covenant
    # clocks running and round 11 executes their tests; this
    # section is the periodic judgement over the whole credit.
    # The obligation derives from the covenant set's anchor (its
    # closing date, or the approval date when no closing is
    # recorded); the pack is assembled in code from stored
    # artefacts only — fresh spread and computation pack through
    # the existing engines, the period's covenant history and
    # breaches, the policy report under BOTH the pinned and the
    # current pack versions, readiness recomputed at the date —
    # and the officer's stance is a coded act that closes the
    # instance. Nothing here approves credit either: a stance
    # affirms a review, attaches conditions, watches, or refers
    # the credit back to the committee machinery.
    # ------------------------------------------------------------------

    def _review_anchor(
        self, credit_file: CreditFile, covenant_set: CovenantSet
    ) -> tuple[date, str]:
        """The date annual reviews count from, and its basis —
        named, never guessed silently: the closing date when one
        is recorded (the set's own, or the file's applied at read
        time, exactly as the covenant schedule reads it), else
        the date of the committee decision that created the set."""
        effective = self._effective_set(credit_file, covenant_set)
        if effective.closing_date is not None:
            return effective.closing_date, "closing_date"
        decision = self.store.get_committee_decision(
            covenant_set.decision_id
        )
        if decision is None:  # pragma: no cover - set provenance
            raise NotFoundError(
                f"committee decision {covenant_set.decision_id} "
                f"not found: the covenant set {covenant_set.set_id} "
                "has no anchor to derive its reviews from"
            )
        return decision.decided_at.date(), "approval_date"

    def _review_set_for(
        self, credit_file_id: str, set_id: str | None
    ) -> CovenantSet:
        """The covenant set a review act names — or the file's
        only set. A file with several sets must name its set: a
        review attached to the wrong set would judge the wrong
        facility's year."""
        if set_id is not None:
            return self.get_covenant_set(credit_file_id, set_id)
        sets = self.store.list_covenant_sets(credit_file_id)
        if not sets:
            raise NotFoundError(
                f"credit file {credit_file_id} has no covenant "
                "set: annual reviews attach to an approved "
                "facility's covenant set"
            )
        if len(sets) > 1:
            raise ValidationError(
                f"credit file {credit_file_id} carries "
                f"{len(sets)} covenant sets; name the set the "
                "review belongs to (set_id)"
            )
        return sets[0]

    def _review_instances(
        self,
        credit_file: CreditFile,
        covenant_set: CovenantSet,
        *,
        as_of: date,
    ) -> list[ReviewInstance]:
        """The set's derived review instances at `as_of`, each
        carrying the latest pack assembled for it, if any."""
        anchor, _basis = self._review_anchor(credit_file, covenant_set)
        outcomes = [
            o for o in self.store.list_review_outcomes(
                credit_file.credit_file_id
            )
            if o.set_id == covenant_set.set_id
        ]
        instances = derive_review_instances(
            credit_file.credit_file_id,
            covenant_set,
            anchor=anchor,
            as_of=as_of,
            outcomes=outcomes,
        )
        latest_pack: dict[int, str] = {}
        for pack in self.store.list_review_packs(credit_file.credit_file_id):
            if pack.set_id == covenant_set.set_id:
                latest_pack[pack.review_number] = pack.pack_id
        return [
            ReviewInstance(
                credit_file_id=instance.credit_file_id,
                set_id=instance.set_id,
                review_number=instance.review_number,
                anchor_date=instance.anchor_date,
                period_start=instance.period_start,
                due_date=instance.due_date,
                state=instance.state,
                days_until_due=instance.days_until_due,
                outcome_id=instance.outcome_id,
                pack_id=latest_pack.get(instance.review_number),
            )
            for instance in instances
        ]

    def annual_review_schedule(
        self,
        credit_file_id: str,
        *,
        as_of: date,
        set_id: str | None = None,
    ) -> dict:
        """One file's annual review obligations at `as_of`: per
        covenant set, the anchor (and which basis it stands on)
        and every derived instance — its window, its due date,
        its state, and the pack and outcome it carries, if any."""
        credit_file = self._load(credit_file_id)
        sets = self.store.list_covenant_sets(credit_file_id)
        if set_id is not None:
            sets = [s for s in sets if s.set_id == set_id]
            if not sets:
                raise NotFoundError(
                    f"covenant set {set_id} not found on credit "
                    f"file {credit_file_id}"
                )
        views = []
        for covenant_set in sets:
            anchor, basis = self._review_anchor(credit_file, covenant_set)
            views.append({
                "covenant_set": self._effective_set(
                    credit_file, covenant_set
                ),
                "anchor_date": anchor,
                "anchor_basis": basis,
                "instances": self._review_instances(
                    credit_file, covenant_set, as_of=as_of
                ),
            })
        return {
            "credit_file_id": credit_file_id,
            "borrower": credit_file.borrower.legal_name,
            "as_of": as_of,
            "sets": views,
        }

    def _review_instance_for(
        self,
        credit_file: CreditFile,
        covenant_set: CovenantSet,
        review_number: int,
        *,
        as_of: date,
    ) -> ReviewInstance:
        instances = self._review_instances(
            credit_file, covenant_set, as_of=as_of
        )
        instance = next(
            (i for i in instances if i.review_number == review_number),
            None,
        )
        if instance is None:
            raise NotFoundError(
                f"annual review {review_number} does not exist "
                f"on covenant set {covenant_set.set_id}: the "
                f"facility's term carries {len(instances)} annual "
                "review(s)"
            )
        return instance

    def _build_review_inputs(
        self,
        credit_file: CreditFile,
        covenant_set: CovenantSet,
        instance: ReviewInstance,
        anchor: date,
        basis: str,
        *,
        as_of: date,
    ) -> ReviewInputs:
        """Assemble everything a review pack pins, through the
        existing engines only: the spread rebuilt, the
        computation pack recomputed with the set's current
        covenants as definitions over the latest period, the
        policy report under the pinned version and under the
        current one, readiness recomputed at the review date,
        and the period's recorded covenant history and breaches
        read with their restatement standing."""
        credit_file_id = credit_file.credit_file_id
        spread = self._current_spread(credit_file_id)
        if spread is None:
            raise ValidationError(
                f"credit file {credit_file_id} has no spread: an "
                "annual review pack is assembled from spread "
                "figures, and there are none on file"
            )
        effective = self._effective_set(credit_file, covenant_set)
        current = current_covenants(
            effective,
            self.store.list_covenant_amendments(credit_file_id),
        )
        definitions: list[CovenantDefinition] = []
        if spread.periods:
            latest = max(spread.periods)
            definitions = [
                CovenantDefinition(
                    name=covenant.name,
                    metric_key=covenant.metric_key,
                    operator=covenant.operator,
                    threshold=covenant.threshold,
                    fiscal_year=latest,
                )
                for covenant in current
            ]
        pack = self.compute_metrics(
            credit_file_id, covenant_definitions=definitions
        )
        spread = next(
            s for s in self.store.list_spreads(credit_file_id)
            if s.spread_id == pack.spread_id
        )
        pinned_report = self.evaluate_policy(
            credit_file_id,
            pack_version=covenant_set.policy_version,
            as_of=as_of,
        )
        current_report = self.evaluate_policy(
            credit_file_id,
            pack_version=CURRENT_POLICY_VERSION,
            as_of=as_of,
        )
        readiness = self.compute_readiness(credit_file_id, as_of=as_of)
        in_window = (
            lambda day: instance.period_start < day <= instance.due_date
        )
        results = sorted(
            (
                r for r in self.store.list_covenant_test_results(
                    credit_file_id
                )
                if in_window(r.test_date)
            ),
            key=lambda r: (r.test_date, r.result_id),
        )
        standings = {
            r.result_id: result_standing(r, spread) for r in results
        }
        all_breaches = self.store.list_breach_cases(credit_file_id)
        breaches = sorted(
            (b for b in all_breaches if in_window(b.test_date)),
            key=lambda b: (b.test_date, b.breach_id),
        )
        open_breaches = sorted(
            (
                b for b in all_breaches
                if b.state not in BREACH_TERMINAL_STATES
            ),
            key=lambda b: (b.test_date, b.breach_id),
        )
        rating_pair = self._current_rating_pair(credit_file_id)
        return ReviewInputs(
            credit_file=credit_file,
            covenant_set=covenant_set,
            review_number=instance.review_number,
            anchor_date=anchor,
            anchor_basis=basis,
            period_start=instance.period_start,
            due_date=instance.due_date,
            as_of=as_of,
            spread=spread,
            pack=pack,
            pinned_report=pinned_report,
            current_report=current_report,
            readiness=readiness,
            results=results,
            standings=standings,
            breaches=breaches,
            open_breaches=open_breaches,
            blockers=readiness.blockers,
            rating=rating_pair[0] if rating_pair else None,
            rating_override=rating_pair[1] if rating_pair else None,
        )

    def assemble_review_pack(
        self,
        credit_file_id: str,
        *,
        as_of: date,
        review_number: int | None = None,
        set_id: str | None = None,
        profile: str = "offline",
    ) -> AnnualReviewPack:
        """Assemble the pack for one annual review instance.

        The instance defaults to the earliest review without a
        recorded outcome. The summary is composed through the
        model port and verified before the pack exists — a
        summary that cannot be fully traced, drops a locked
        fact, or softens the record refuses the assembly, and
        nothing is stored. The pack is content-addressed:
        assembling the same review over the same inputs at the
        same date reproduces it, and the audit records only the
        first assembly of each distinct pack."""
        credit_file = self._load(credit_file_id)
        covenant_set = self._review_set_for(credit_file_id, set_id)
        anchor, basis = self._review_anchor(credit_file, covenant_set)
        if review_number is None:
            instances = self._review_instances(
                credit_file, covenant_set, as_of=as_of
            )
            if not instances:
                raise ValidationError(
                    f"covenant set {covenant_set.set_id} carries "
                    "no annual review instances: the facility's "
                    "term ends before its first anniversary"
                )
            open_instances = [
                i for i in instances if i.outcome_id is None
            ]
            if not open_instances:
                raise ValidationError(
                    f"every annual review on covenant set "
                    f"{covenant_set.set_id} is completed: there "
                    "is no open instance to assemble a pack for"
                )
            instance = open_instances[0]
        else:
            instance = self._review_instance_for(
                credit_file, covenant_set, review_number, as_of=as_of
            )
        inputs = self._build_review_inputs(
            credit_file, covenant_set, instance, anchor, basis,
            as_of=as_of,
        )
        composer = self._review_composer(profile, credit_file_id)
        paragraphs = composer.compose(inputs)
        locked = locked_fact_paragraphs(inputs)
        verification = verify_review_summary(
            paragraphs=paragraphs,
            locked_facts=locked,
            inputs=inputs,
            composer_profile=composer.name,
        )
        summary = ReviewSummary(
            composer_profile=composer.name,
            paragraphs=paragraphs,
            locked_facts=locked,
            verification=verification,
        )
        descriptor = {
            "credit_file_id": credit_file_id,
            "set_id": covenant_set.set_id,
            "review_number": instance.review_number,
            "as_of": as_of.isoformat(),
            "spread_id": inputs.spread.spread_id,
            "computation_pack_id": inputs.pack.pack_id,
            "pinned_policy_report_id": inputs.pinned_report.report_id,
            "current_policy_report_id": inputs.current_report.report_id,
            "readiness_report_id": inputs.readiness.report_id,
            "rating_assignment_id": (
                inputs.rating.assignment_id if inputs.rating else None
            ),
            "rating_override_id": (
                inputs.rating_override.override_id
                if inputs.rating_override else None
            ),
            "summary": summary.model_dump(mode="json"),
        }
        rating_final = (
            inputs.rating_override.new_grade
            if inputs.rating_override is not None
            else inputs.rating.assigned_grade
            if inputs.rating is not None
            else None
        )
        pack = AnnualReviewPack(
            pack_id=f"reviewpack-{_canonical_hash(descriptor)[:16]}",
            credit_file_id=credit_file_id,
            set_id=covenant_set.set_id,
            review_number=instance.review_number,
            anchor_date=anchor,
            period_start=instance.period_start,
            due_date=instance.due_date,
            as_of=as_of,
            borrower_name=credit_file.borrower.legal_name,
            memo_id=covenant_set.memo_id,
            spread_id=inputs.spread.spread_id,
            computation_pack_id=inputs.pack.pack_id,
            latest_period=(
                max(inputs.spread.periods)
                if inputs.spread.periods else None
            ),
            periods_covered=list(inputs.spread.periods),
            pinned_policy_pack_id=inputs.pinned_report.policy_pack_id,
            pinned_policy_version=inputs.pinned_report.policy_version,
            pinned_policy_report_id=inputs.pinned_report.report_id,
            current_policy_version=inputs.current_report.policy_version,
            current_policy_report_id=inputs.current_report.report_id,
            policy_differences=[
                PolicyOutcomeDifference(
                    test_id=test_id,
                    pinned_outcome=pinned_outcome,
                    current_outcome=current_outcome,
                )
                for test_id, pinned_outcome, current_outcome
                in inputs.differences
            ],
            readiness_report_id=inputs.readiness.report_id,
            complete=inputs.complete,
            blockers=[
                ReviewBlocker(
                    input_key=entry.input_key,
                    label=entry.label,
                    state=entry.state,
                    detail=entry.detail,
                )
                for entry in inputs.blockers
            ],
            test_result_ids=[r.result_id for r in inputs.results],
            breach_ids=[b.breach_id for b in inputs.breaches],
            open_breach_ids=[
                b.breach_id for b in inputs.open_breaches
            ],
            material_disagreement_ids=[
                d.disagreement_id
                for d in inputs.spread.disagreements if d.material
            ],
            rating_assignment_id=(
                inputs.rating.assignment_id if inputs.rating else None
            ),
            rating_scorecard_id=(
                inputs.rating.scorecard_id if inputs.rating else None
            ),
            rating_scorecard_version=(
                inputs.rating.scorecard_version if inputs.rating else None
            ),
            rating_assigned_grade=(
                inputs.rating.assigned_grade if inputs.rating else None
            ),
            rating_assigned_band=(
                inputs.rating.assigned_band if inputs.rating else None
            ),
            rating_grade=rating_final,
            rating_band=(
                self._band_label_for(inputs.rating, rating_final)
                if inputs.rating is not None and rating_final is not None
                else None
            ),
            rating_override_id=(
                inputs.rating_override.override_id
                if inputs.rating_override else None
            ),
            rating_factors=(
                list(inputs.rating.factors) if inputs.rating else []
            ),
            summary=summary,
        )
        is_new = self.store.get_review_pack(pack.pack_id) is None
        self.store.save_review_pack(pack)
        if is_new:
            self._audit(
                credit_file_id,
                "annual_review_pack_assembled",
                f"annual review {pack.review_number} pack "
                f"{pack.pack_id} assembled at {as_of.isoformat()} "
                f"for the period {pack.period_start.isoformat()} "
                f"to {pack.due_date.isoformat()}: spread "
                f"{pack.spread_id}, computation pack "
                f"{pack.computation_pack_id}, policy "
                f"{pack.pinned_policy_version} (pinned) and "
                f"{pack.current_policy_version} (current), "
                + (
                    "complete"
                    if pack.complete
                    else f"INCOMPLETE — {len(pack.blockers)} "
                    "blocker(s): "
                    + ", ".join(b.input_key for b in pack.blockers)
                ),
            )
        return pack

    def get_review_pack(
        self, credit_file_id: str, pack_id: str
    ) -> AnnualReviewPack:
        self._load(credit_file_id)
        pack = self.store.get_review_pack(pack_id)
        if pack is None or pack.credit_file_id != credit_file_id:
            raise NotFoundError(
                f"annual review pack {pack_id} not found on "
                f"credit file {credit_file_id}"
            )
        return pack

    def list_review_packs(
        self, credit_file_id: str
    ) -> list[AnnualReviewPack]:
        self._load(credit_file_id)
        return self.store.list_review_packs(credit_file_id)

    def check_review_summary(
        self,
        credit_file_id: str,
        pack_id: str,
        *,
        paragraphs: list[str],
        locked_facts: list[str],
    ) -> object:
        """Re-verify a candidate summary against a stored pack's
        pinned inputs — the examiner's surface. The pack's own
        summary passes (it was verified at assembly); a tampered
        one — a figure swapped, a locked paragraph dropped, a
        breach denied in prose — is refused by name. Nothing is
        stored either way: this is a check, not an edit path."""
        pack = self.get_review_pack(credit_file_id, pack_id)
        inputs = self._review_inputs_for_pack(pack)
        return verify_review_summary(
            paragraphs=list(paragraphs),
            locked_facts=list(locked_facts),
            inputs=inputs,
            composer_profile=pack.summary.composer_profile,
        )

    def _review_inputs_for_pack(
        self, pack: AnnualReviewPack
    ) -> ReviewInputs:
        """Rebuild the assembly inputs a stored pack pins, from
        the store — every artefact by the id the pack recorded."""
        credit_file = self._load(pack.credit_file_id)
        covenant_set = self.get_covenant_set(
            pack.credit_file_id, pack.set_id
        )
        anchor, basis = self._review_anchor(credit_file, covenant_set)
        spread = next(
            (
                s for s in self.store.list_spreads(pack.credit_file_id)
                if s.spread_id == pack.spread_id
            ),
            None,
        )
        if spread is None:
            raise NotFoundError(
                f"spread {pack.spread_id} pinned by review pack "
                f"{pack.pack_id} not found"
            )
        computation_pack = self.store.get_computation_pack(
            pack.computation_pack_id
        )
        if computation_pack is None:
            raise NotFoundError(
                f"computation pack {pack.computation_pack_id} "
                f"pinned by review pack {pack.pack_id} not found"
            )
        pinned_report = self.store.get_policy_report(
            pack.pinned_policy_report_id
        )
        current_report = self.store.get_policy_report(
            pack.current_policy_report_id
        )
        readiness = self.store.get_readiness_report(
            pack.readiness_report_id
        )
        if pinned_report is None or current_report is None \
                or readiness is None:
            raise NotFoundError(
                f"a report pinned by review pack {pack.pack_id} "
                "is missing from the store"
            )
        results = [
            result
            for result in (
                self.store.get_covenant_test_result(result_id)
                for result_id in pack.test_result_ids
            )
            if result is not None
        ]
        breaches = [
            breach
            for breach in (
                self.store.get_breach_case(breach_id)
                for breach_id in pack.breach_ids
            )
            if breach is not None
        ]
        open_breaches = [
            breach
            for breach in (
                self.store.get_breach_case(breach_id)
                for breach_id in pack.open_breach_ids
            )
            if breach is not None
        ]
        rating = (
            self.store.get_rating_assignment(pack.rating_assignment_id)
            if pack.rating_assignment_id
            else None
        )
        rating_override = (
            self.store.get_rating_override(pack.rating_override_id)
            if pack.rating_override_id
            else None
        )
        return ReviewInputs(
            credit_file=credit_file,
            covenant_set=covenant_set,
            review_number=pack.review_number,
            anchor_date=pack.anchor_date,
            anchor_basis=basis,
            period_start=pack.period_start,
            due_date=pack.due_date,
            as_of=pack.as_of,
            spread=spread,
            pack=computation_pack,
            pinned_report=pinned_report,
            current_report=current_report,
            readiness=readiness,
            results=results,
            standings={
                r.result_id: result_standing(r, spread) for r in results
            },
            breaches=breaches,
            open_breaches=open_breaches,
            blockers=readiness.blockers,
            rating=rating,
            rating_override=rating_override,
        )

    def record_review_outcome(
        self,
        credit_file_id: str,
        *,
        review_number: int,
        stance: str,
        reason_codes: list[str],
        reviewer_id: str,
        reviewer_role: str,
        pack_id: str | None = None,
        set_id: str | None = None,
        as_of: date | None = None,
        countersigner_id: str | None = None,
        countersigner_role: str | None = None,
        conditions: list[CommitteeCondition] | None = None,
        note: str = "",
    ) -> AnnualReviewOutcome:
        """Record a named credit officer's stance on one annual
        review — the act that closes the instance.

        The stance reads a pack: the one named, or the latest
        assembled for the instance, or one assembled now at the
        review's due date. Every discipline in reviews.py runs
        before anything is stored — officer role, coded reasons,
        the condition machinery, the self-review countersignature
        wall — and an instance closes exactly once: a second
        outcome for the same review is refused by name, because a
        changed stance is a later review's business, not an edit
        of this one. An escalate_to_committee stance mints the
        referral it hands to the committee machinery, in the same
        act."""
        credit_file = self._load(credit_file_id)
        covenant_set = self._review_set_for(credit_file_id, set_id)
        existing = [
            o for o in self.store.list_review_outcomes(credit_file_id)
            if o.set_id == covenant_set.set_id
            and o.review_number == review_number
        ]
        if existing:
            raise ValidationError(
                f"annual review {review_number} on covenant set "
                f"{covenant_set.set_id} already closed with "
                f"outcome {existing[0].outcome_id} "
                f"({existing[0].stance}): a review instance "
                "closes exactly once"
            )
        instance = self._review_instance_for(
            credit_file, covenant_set, review_number,
            as_of=as_of or date(1, 1, 1),
        )
        if pack_id is not None:
            pack = self.get_review_pack(credit_file_id, pack_id)
            if pack.set_id != covenant_set.set_id \
                    or pack.review_number != review_number:
                raise ValidationError(
                    f"review pack {pack_id} belongs to review "
                    f"{pack.review_number} on set {pack.set_id}, "
                    f"not review {review_number} on set "
                    f"{covenant_set.set_id}: a stance reads the "
                    "pack of the review it closes"
                )
        else:
            packs = [
                p for p in self.store.list_review_packs(credit_file_id)
                if p.set_id == covenant_set.set_id
                and p.review_number == review_number
            ]
            pack = packs[-1] if packs else self.assemble_review_pack(
                credit_file_id,
                review_number=review_number,
                set_id=covenant_set.set_id,
                as_of=as_of or instance.due_date,
            )
        memo = self._get_memo_raw(credit_file_id, covenant_set.memo_id)
        stored_conditions = validate_review_outcome(
            stance=stance,
            reason_codes=list(reason_codes or []),
            note=note,
            conditions=list(conditions or []),
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            memo=memo,
            memo_inputs=self._memo_inputs(memo),
            countersigner_id=countersigner_id,
            countersigner_role=countersigner_role,
        )
        descriptor = {
            "credit_file_id": credit_file_id,
            "set_id": covenant_set.set_id,
            "review_number": review_number,
            "pack_id": pack.pack_id,
            "stance": stance,
            "reason_codes": list(reason_codes or []),
            "reviewer_id": reviewer_id,
            "countersigner_id": countersigner_id,
            "conditions": [
                c.model_dump(mode="json") for c in stored_conditions
            ],
            "note": note,
        }
        outcome_id = f"reviewoutcome-{_canonical_hash(descriptor)[:16]}"
        referral: CommitteeReferral | None = None
        if stance == "escalate_to_committee":
            referral = CommitteeReferral(
                referral_id=(
                    f"referral-{_canonical_hash({'outcome': outcome_id})[:16]}"
                ),
                credit_file_id=credit_file_id,
                set_id=covenant_set.set_id,
                review_number=review_number,
                outcome_id=outcome_id,
                pack_id=pack.pack_id,
                memo_id=covenant_set.memo_id,
                reason_codes=list(reason_codes or []),
                reason_vocabulary=REVIEW_VOCABULARY_VERSION,
                referred_by=reviewer_id,
                referred_by_role=reviewer_role,
                note=note,
            )
        outcome = AnnualReviewOutcome(
            outcome_id=outcome_id,
            credit_file_id=credit_file_id,
            set_id=covenant_set.set_id,
            review_number=review_number,
            due_date=instance.due_date,
            pack_id=pack.pack_id,
            stance=stance,
            reason_codes=list(reason_codes or []),
            reason_vocabulary=REVIEW_VOCABULARY_VERSION,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            countersigner_id=countersigner_id,
            countersigner_role=countersigner_role,
            conditions=stored_conditions,
            referral_id=referral.referral_id if referral else None,
            note=note,
        )
        is_new = self.store.get_review_outcome(outcome_id) is None
        self.store.save_review_outcome(outcome)
        if referral is not None:
            self.store.save_committee_referral(referral)
        if is_new:
            detail = (
                f"annual review {review_number} on covenant set "
                f"{covenant_set.set_id} recorded {stance} by "
                f"{reviewer_id} ({reviewer_role}) on pack "
                f"{pack.pack_id}; reasons "
                f"({REVIEW_VOCABULARY_VERSION}): "
                + ", ".join(outcome.reason_codes)
            )
            if countersigner_id:
                detail += f"; countersigned by {countersigner_id}"
            if stored_conditions:
                detail += f"; {len(stored_conditions)} condition(s)"
            if referral is not None:
                detail += (
                    f"; escalated — committee referral "
                    f"{referral.referral_id} recorded on memo "
                    f"{referral.memo_id}"
                )
            self._audit(
                credit_file_id, "annual_review_recorded", detail,
                actor=reviewer_id,
            )
        return outcome

    def list_review_outcomes(
        self, credit_file_id: str
    ) -> list[AnnualReviewOutcome]:
        self._load(credit_file_id)
        return self.store.list_review_outcomes(credit_file_id)

    def list_committee_referrals(
        self, credit_file_id: str
    ) -> list[CommitteeReferral]:
        self._load(credit_file_id)
        return self.store.list_committee_referrals(credit_file_id)

    # -- Risk ratings (round 13) ------------------------------------
    # The coded scorecard assigns; a named officer may override
    # the final grade under the scorecard module's discipline;
    # the memo and the review pack carry the result as an input.
    # Nothing in this section decides anything: there is no path
    # from a rating to a gate, and the gates do not import it.

    def assign_rating(
        self,
        credit_file_id: str,
        *,
        as_of: date,
        scorecard_version: str | None = None,
    ) -> RatingAssignment:
        """Assign the file's risk rating under one scorecard
        version, over the file's current computation pack.

        The pack is recomputed through the existing engine over
        the current spread (content-addressed, so an unchanged
        file re-reads the stored pack); the covenant results are
        read with their restatement standing against that spread,
        and the breach cases as they stand. The assignment is
        content-addressed too: assigning the same file over the
        same artefacts at the same date returns the stored
        artefact, and the audit records only the first assignment
        of each distinct rating."""
        credit_file = self._load(credit_file_id)
        definition = get_scorecard(scorecard_version)
        pack = self.compute_metrics(credit_file_id)
        spread = next(
            (
                s
                for s in self.store.list_spreads(credit_file_id)
                if s.spread_id == pack.spread_id
            ),
            None,
        ) or self.store.get_latest_spread(credit_file_id)
        if spread is None or not spread.periods:
            raise ValidationError(
                f"credit file {credit_file_id} has no spread "
                "periods: a rating is scored over spread figures, "
                "and there are none on file"
            )
        results = self.store.list_covenant_test_results(credit_file_id)
        standings = {
            r.result_id: result_standing(r, spread) for r in results
        }
        breaches = self.store.list_breach_cases(credit_file_id)
        assignment = build_rating_assignment(
            definition,
            credit_file=credit_file,
            spread=spread,
            pack=pack,
            test_results=results,
            result_standings=standings,
            breaches=breaches,
            as_of=as_of,
        )
        is_new = (
            self.store.get_rating_assignment(assignment.assignment_id)
            is None
        )
        self.store.save_rating_assignment(assignment)
        if is_new:
            self._audit(
                credit_file_id,
                "risk_rating_assigned",
                f"risk rating {assignment.assignment_id}: grade "
                f"{assignment.assigned_grade} "
                f"({assignment.assigned_band}), composite "
                f"{assignment.composite_score} under scorecard "
                f"{assignment.scorecard_id} version "
                f"{assignment.scorecard_version}, over computation "
                f"pack {assignment.computation_pack_id} at "
                f"{as_of.isoformat()}",
            )
        return assignment

    def get_rating_assignment(
        self, credit_file_id: str, assignment_id: str
    ) -> RatingAssignment:
        self._load(credit_file_id)
        assignment = self.store.get_rating_assignment(assignment_id)
        if assignment is None or assignment.credit_file_id != credit_file_id:
            raise NotFoundError(
                f"rating assignment {assignment_id} not found on "
                f"credit file {credit_file_id}"
            )
        return assignment

    def list_rating_assignments(
        self, credit_file_id: str
    ) -> list[RatingAssignment]:
        self._load(credit_file_id)
        return self.store.list_rating_assignments(credit_file_id)

    def list_rating_overrides(
        self, credit_file_id: str
    ) -> list[RatingOverride]:
        self._load(credit_file_id)
        return self.store.list_rating_overrides(credit_file_id)

    def _overrides_for(
        self, assignment: RatingAssignment
    ) -> list[RatingOverride]:
        return [
            o
            for o in self.store.list_rating_overrides(
                assignment.credit_file_id
            )
            if o.assignment_id == assignment.assignment_id
        ]

    def _band_label_for(
        self, assignment: RatingAssignment, grade: int
    ) -> str:
        if grade == assignment.assigned_grade:
            return assignment.assigned_band
        try:
            return grade_band_label(
                get_scorecard(assignment.scorecard_version), grade
            )
        except ValidationError:
            return assignment.assigned_band

    def rating_view(self, assignment: RatingAssignment) -> dict:
        """One assignment as its readers see it: the assigned
        grade AND the final grade (after the override chain),
        the head override that produced it, and the full chain.
        Both grades, always — a moved grade is never shown
        without the grade it was moved from."""
        overrides = self._overrides_for(assignment)
        final, head = final_grade_of(assignment, overrides)
        return {
            "assignment": assignment,
            "assigned_grade": assignment.assigned_grade,
            "assigned_band": assignment.assigned_band,
            "final_grade": final,
            "final_band": self._band_label_for(assignment, final),
            "override": head,
            "overrides": overrides,
        }

    def current_rating(
        self, credit_file_id: str
    ) -> dict | None:
        """The rating view that stands current on the file: the
        latest stored assignment, or None when none exists."""
        assignments = self.list_rating_assignments(credit_file_id)
        if not assignments:
            return None
        return self.rating_view(assignments[-1])

    def _current_rating_pair(
        self, credit_file_id: str
    ) -> tuple[RatingAssignment, RatingOverride | None] | None:
        assignments = self.store.list_rating_assignments(credit_file_id)
        if not assignments:
            return None
        assignment = assignments[-1]
        _final, head = final_grade_of(
            assignment, self._overrides_for(assignment)
        )
        return assignment, head

    def _current_rating_facts(
        self, credit_file_id: str
    ) -> RatingFacts | None:
        pair = self._current_rating_pair(credit_file_id)
        if pair is None:
            return None
        assignment, head = pair
        final, _ = final_grade_of(
            assignment, self._overrides_for(assignment)
        )
        return RatingFacts(
            assignment_id=assignment.assignment_id,
            scorecard_id=assignment.scorecard_id,
            scorecard_version=assignment.scorecard_version,
            assigned_grade=assignment.assigned_grade,
            assigned_band=assignment.assigned_band,
            final_grade=final,
            final_band=self._band_label_for(assignment, final),
            override_id=head.override_id if head else None,
        )

    def rating_history(self, credit_file_id: str) -> list[dict]:
        """The file's rating trend: every assignment in stored
        order, each with its scorecard version and both grades.

        A version change never rewrites this history — earlier
        assignments stand under the version that produced them —
        and the view labels the comparison honestly: an entry
        whose version differs from its predecessor's is marked
        cross-version, because grades from two definitions are
        not directly comparable."""
        self._load(credit_file_id)
        history: list[dict] = []
        previous_version: str | None = None
        for assignment in self.store.list_rating_assignments(
            credit_file_id
        ):
            view = self.rating_view(assignment)
            cross_version = (
                previous_version is not None
                and assignment.scorecard_version != previous_version
            )
            view.update({
                "scorecard_version": assignment.scorecard_version,
                "cross_version": cross_version,
                "comparison_basis": (
                    "cross-version — grades from two scorecard "
                    "versions are not directly comparable"
                    if cross_version
                    else "same scorecard version as the prior entry"
                    if previous_version is not None
                    else "first recorded assignment"
                ),
            })
            history.append(view)
            previous_version = assignment.scorecard_version
        return history

    def override_rating(
        self,
        credit_file_id: str,
        assignment_id: str,
        *,
        officer_id: str,
        officer_role: str,
        new_grade: int,
        reason_code: str,
        countersigner_id: str | None = None,
        countersigner_role: str | None = None,
        note: str = "",
    ) -> RatingOverride:
        """Record a coded officer override of a rating's final
        grade. The scorecard module's validation disposes: the
        bound (±1 from the ASSIGNED grade), the coded reason, and
        the countersigning second officer are all refused or
        allowed there, in code. The assignment artefact is never
        touched — the override is stored beside it and the audit
        records the move with both grades."""
        self._load(credit_file_id)
        assignment = self.get_rating_assignment(
            credit_file_id, assignment_id
        )
        override = validate_override(
            assignment=assignment,
            overrides=self._overrides_for(assignment),
            new_grade=new_grade,
            reason_code=reason_code,
            note=note,
            officer_id=officer_id,
            officer_role=officer_role,
            countersigner_id=countersigner_id,
            countersigner_role=countersigner_role,
        )
        is_new = (
            self.store.get_rating_override(override.override_id) is None
        )
        self.store.save_rating_override(override)
        if is_new:
            self._audit(
                credit_file_id,
                "risk_rating_overridden",
                f"risk rating {assignment.assignment_id}: final "
                f"grade moved {override.previous_grade} → "
                f"{override.new_grade} by {officer_id} "
                f"({officer_role}), reason {reason_code} under "
                f"{override.reason_vocabulary}, countersigned by "
                f"{countersigner_id}; the assigned grade "
                f"{assignment.assigned_grade} stands on the "
                "assignment unchanged",
            )
        return override

    def review_due_work(self, *, as_of: date) -> list[dict]:
        """The annual review queue at `as_of`: every review
        instance standing at overdue / due / due_soon across the
        portfolio, worst first — which borrower, which review,
        due when, and whether its pack has been assembled."""
        queue: list[dict] = []
        for credit_file in self.store.list_credit_files():
            for covenant_set in self.store.list_covenant_sets(
                credit_file.credit_file_id
            ):
                for instance in self._review_instances(
                    credit_file, covenant_set, as_of=as_of
                ):
                    if instance.state in REVIEW_WORK_STATES:
                        queue.append({
                            "borrower": credit_file.borrower.legal_name,
                            "credit_file_id": credit_file.credit_file_id,
                            "instance": instance,
                        })
        order = {"overdue": 0, "due": 1, "due_soon": 2}
        queue.sort(key=lambda entry: (
            order[entry["instance"].state],
            entry["instance"].due_date,
            entry["borrower"],
        ))
        return queue

    def _open_due_reviews_for(
        self, credit_file: CreditFile, *, as_of: date
    ) -> list[dict]:
        """Assemble packs for one file's review instances that
        stand due or overdue without an outcome and without a
        pack — the scheduler opening the review, evidenced, so
        the officer's stance has something to read."""
        opened: list[dict] = []
        for covenant_set in self.store.list_covenant_sets(
            credit_file.credit_file_id
        ):
            for instance in self._review_instances(
                credit_file, covenant_set, as_of=as_of
            ):
                if instance.state not in ("due", "overdue"):
                    continue
                if instance.outcome_id is not None \
                        or instance.pack_id is not None:
                    continue
                pack = self.assemble_review_pack(
                    credit_file.credit_file_id,
                    review_number=instance.review_number,
                    set_id=covenant_set.set_id,
                    as_of=as_of,
                )
                opened.append({
                    "credit_file_id": credit_file.credit_file_id,
                    "borrower": credit_file.borrower.legal_name,
                    "set_id": covenant_set.set_id,
                    "review_number": instance.review_number,
                    "due_date": instance.due_date,
                    "pack_id": pack.pack_id,
                    "complete": pack.complete,
                })
        return opened

    def open_due_reviews(
        self, *, as_of: date, credit_file_id: str | None = None
    ) -> dict:
        """Open every due annual review in the portfolio (or on
        one file): assemble the pack for each instance standing
        due or overdue that has neither an outcome nor a pack.
        Idempotent — an opened review is not opened twice."""
        files = (
            [self._load(credit_file_id)]
            if credit_file_id is not None
            else self.store.list_credit_files()
        )
        opened: list[dict] = []
        for credit_file in files:
            opened.extend(
                self._open_due_reviews_for(credit_file, as_of=as_of)
            )
        return {"as_of": as_of, "reviews_opened": opened}

    def amend_covenant(
        self,
        credit_file_id: str,
        covenant_id: str,
        *,
        author_id: str,
        author_role: str,
        reason_code: str,
        decision_id: str,
        threshold: Decimal | None = None,
        operator: str | None = None,
        frequency: str | None = None,
        grace_days: int | None = None,
        note: str = "",
    ) -> CovenantAmendment:
        """Amend one covenant — by supersession, never by edit.

        Validates the whole discipline in covenants.py (named
        author with an amending role, coded reason, an authorising
        approval on this file, a change that changes something),
        mints the next version, and stores it as a
        CovenantAmendment beside the untouched set. The set's
        stored bytes are identical before and after; only the
        derived current covenants move."""
        self._load(credit_file_id)
        amendments = self.store.list_covenant_amendments(credit_file_id)
        target_set: CovenantSet | None = None
        current = None
        for covenant_set in self.store.list_covenant_sets(credit_file_id):
            try:
                current = get_covenant_or_raise(
                    covenant_set, amendments, covenant_id
                )
                target_set = covenant_set
                break
            except NotFoundError:
                continue
        if target_set is None or current is None:
            raise NotFoundError(
                f"covenant {covenant_id} not found on credit file "
                f"{credit_file_id}"
            )
        decision = self.store.get_committee_decision(decision_id)
        amended = validate_amendment(
            current,
            author_id=author_id,
            author_role=author_role,
            reason_code=reason_code,
            note=note,
            decision=decision,
            new_threshold=threshold,
            new_operator=operator,
            new_frequency=frequency,
            new_grace_days=grace_days,
        )
        record = amendment_record(
            target_set,
            current,
            amended,
            author_id=author_id,
            author_role=author_role,
            reason_code=reason_code,
            decision_id=decision_id,
            note=note,
        )
        self.store.save_covenant_amendment(record)
        self._audit(
            credit_file_id,
            "covenant_amended",
            f"covenant {current.covenant_id} ({current.name}) "
            f"amended by {author_id} ({author_role}): "
            f"{current.operator} {current.threshold} -> "
            f"{amended.operator} {amended.threshold}, frequency "
            f"{current.frequency} -> {amended.frequency}, grace "
            f"{current.grace_days}d -> {amended.grace_days}d; new "
            f"version {amended.version} is {amended.covenant_id}; "
            f"reason {reason_code} ({record.amendment_id}); "
            f"authorised by decision {decision_id}",
            actor=author_id,
        )
        return record

    def add_adjustment(
        self, credit_file_id: str, adjustment: SpreadAdjustment
    ) -> Spread:
        """Record an adjustment and rebuild the spread over it.

        Validation runs before anything is stored: an adjustment
        without provenance, without a coded + free-text reason, or
        naming a line the template does not have is rejected whole.
        """
        self._load(credit_file_id)
        validate_adjustment(adjustment, credit_file_id)
        self.store.save_adjustment(adjustment)
        self._audit(
            credit_file_id,
            "adjustment_recorded",
            f"{adjustment.line_key} FY{adjustment.fiscal_year} "
            f"{adjustment.amount} ({adjustment.reason_code.value}) "
            f"proposed by {adjustment.proposed_by}: {adjustment.reason}",
        )
        return self.build_spread(credit_file_id)

    # -- helpers -------------------------------------------------------
    def _load(self, credit_file_id: str) -> CreditFile:
        credit_file = self.store.get_credit_file(credit_file_id)
        if credit_file is None:
            raise NotFoundError(f"credit file {credit_file_id} not found")
        return credit_file

    def _audit(
        self,
        credit_file_id: str,
        action: str,
        detail: str,
        *,
        actor: str | None = None,
    ) -> None:
        self.store.append_audit(
            AuditEvent(
                credit_file_id=credit_file_id,
                actor=actor or self.actor,
                action=action,
                detail=detail,
            )
        )
