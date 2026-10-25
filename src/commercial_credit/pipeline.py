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
from datetime import date
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
from .documents import read_document_verified, register_document
from .errors import NotFoundError, ValidationError
from .extraction import OfflineReferenceExtractor, receive_extraction
from .models import (
    AdverseActionReason,
    AuditEvent,
    Borrower,
    CommitteeCondition,
    CommitteeDecision,
    CommitteeMember,
    ComputationPack,
    CovenantDefinition,
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
    PolicyReport,
    Spread,
    SpreadAdjustment,
    new_id,
    utcnow,
)
from .memo import MemoInputs, _canonical_hash, verify_memo
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
    ) -> None:
        self.store = store
        self.documents = documents
        self.extractor = extractor or OfflineReferenceExtractor()
        self.actor = actor
        self._graph = self._build()

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
            values = receive_extraction(self.extractor.extract(doc, content))
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
            composer=get_composer(profile),
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
        # The verifier decides — over the final rendered text,
        # exactly as it decided for the draft. A refusal raises
        # MemoVerificationError and nothing below runs.
        verification = verify_memo(candidate, self._memo_inputs(memo))
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
        self.store.save_memo(final)
        self.store.save_memo_diff(diff)
        self._record_review_event(MemoReviewEvent(
            credit_file_id=credit_file_id,
            memo_id=memo_id,
            action=action,
            actor_id=officer_id,
            actor_role=officer_role,
            resulting_memo_id=final.memo_id,
            note=note,
        ))
        self._audit(
            credit_file_id,
            "memo_approved",
            f"memo {memo_id} {action.replace('_', ' ')} by "
            f"{officer_id} ({officer_role}); officer-final memo "
            f"{final.memo_id}, diff {diff.diff_id}: "
            f"{len(diff.section_changes)} section(s) and "
            f"{len(diff.figure_changes)} figure(s) changed",
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
            composer=get_composer(old.composer_profile),
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
    ) -> CreditMemo:
        """Bring an officer-final memo before the committee.

        Lawful from officer_approved (first presentment) and from
        deferred (re-presentment after the officer has addressed
        the deferral's requirements). Convening is an event on the
        memo's stream — the derived status becomes in_committee —
        and the decision is a separate act (decide_committee).
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
