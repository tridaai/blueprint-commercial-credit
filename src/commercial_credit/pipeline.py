"""The intake pipeline, as a LangGraph graph.

Topology (each round extends the right-hand side by one node):

    intake ──► checklist ──► document_intake ──► extraction ──► spreading
        ──► computation ──► END

Each node does one thing and records an audit event; the graph is
the auditable spine of the deployment, so the same compiled graph
serves the API, the demo and the evals. Policy tests, memo drafting
and the two human gates join this graph in their rounds — as
interrupts, where a person decides.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .checklist import generate_checklist
from .computations import (
    ENGINE_VERSION,
    compute_entity_pack,
    compute_global_pack,
)
from .documents import read_document_verified, register_document
from .errors import NotFoundError
from .extraction import OfflineReferenceExtractor, receive_extraction
from .models import (
    AuditEvent,
    Borrower,
    ComputationPack,
    CovenantDefinition,
    CreditFile,
    CreditFileStatus,
    DocumentKind,
    Elimination,
    ExtractedValue,
    FacilityRequest,
    Spread,
    SpreadAdjustment,
    new_id,
)
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
            if doc.kind != DocumentKind.FINANCIAL_STATEMENT:
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

    def _build(self):  # noqa: ANN202 - compiled LangGraph app
        graph = StateGraph(PipelineState)
        graph.add_node("intake", self._node_intake)
        graph.add_node("checklist", self._node_checklist)
        graph.add_node("document_intake", self._node_document_intake)
        graph.add_node("extraction", self._node_extraction)
        graph.add_node("spreading", self._node_spreading)
        graph.add_node("computation", self._node_computation)
        graph.add_edge(START, "intake")
        graph.add_edge("intake", "checklist")
        graph.add_edge("checklist", "document_intake")
        graph.add_edge("document_intake", "extraction")
        graph.add_edge("extraction", "spreading")
        graph.add_edge("spreading", "computation")
        graph.add_edge("computation", END)
        return graph.compile()

    # -- public runs ---------------------------------------------------
    def open_application(
        self,
        borrower: Borrower,
        facility: FacilityRequest,
        documents: list[dict] | None = None,
    ) -> CreditFile:
        """Run intake → checklist → documents → extraction → spreading
        → computation."""
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
            self._audit(
                credit_file_id,
                "spread_built",
                f"{len(spread.periods)} periods "
                f"({', '.join(str(y) for y in spread.periods)}), "
                f"{len(spread.lines)} template lines, "
                f"{len(spread.unmapped)} unmapped lines, "
                f"template {spread.template_version}",
            )
        return spread

    def get_spread(self, credit_file_id: str) -> Spread:
        """The file's current spread, rebuilt so it can never lag a
        restated document or a new extraction behind the store."""
        return self.build_spread(credit_file_id)

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

    def _audit(self, credit_file_id: str, action: str, detail: str) -> None:
        self.store.append_audit(
            AuditEvent(
                credit_file_id=credit_file_id,
                actor=self.actor,
                action=action,
                detail=detail,
            )
        )
