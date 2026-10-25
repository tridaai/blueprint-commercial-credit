"""The intake pipeline, as a LangGraph graph.

Round 1 topology (later rounds extend the right-hand side):

    intake ──► checklist ──► document_intake ──► extraction ──► END

Each node does one thing and records an audit event; the graph is
the auditable spine of the deployment, so the same compiled graph
serves the API, the demo and the evals. Spreading, computation,
policy tests, memo drafting and the two human gates join this graph
in their rounds — as interrupts, where a person decides.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .checklist import generate_checklist
from .documents import read_document_verified, register_document
from .errors import NotFoundError
from .extraction import OfflineReferenceExtractor, receive_extraction
from .models import (
    AuditEvent,
    Borrower,
    CreditFile,
    CreditFileStatus,
    DocumentKind,
    FacilityRequest,
    new_id,
)
from .ports import CreditFileStore, DocumentStore, Extractor

log = logging.getLogger("commercial_credit.pipeline")


class PipelineState(TypedDict, total=False):
    borrower: Borrower
    facility: FacilityRequest
    credit_file_id: str
    pending_documents: list[dict]
    extracted_document_ids: list[str]
    extraction_count: int


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

    def _build(self):  # noqa: ANN202 - compiled LangGraph app
        graph = StateGraph(PipelineState)
        graph.add_node("intake", self._node_intake)
        graph.add_node("checklist", self._node_checklist)
        graph.add_node("document_intake", self._node_document_intake)
        graph.add_node("extraction", self._node_extraction)
        graph.add_edge(START, "intake")
        graph.add_edge("intake", "checklist")
        graph.add_edge("checklist", "document_intake")
        graph.add_edge("document_intake", "extraction")
        graph.add_edge("extraction", END)
        return graph.compile()

    # -- public runs ---------------------------------------------------
    def open_application(
        self,
        borrower: Borrower,
        facility: FacilityRequest,
        documents: list[dict] | None = None,
    ) -> CreditFile:
        """Run intake → checklist → document intake → extraction."""
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
