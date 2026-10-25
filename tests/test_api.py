import base64

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import statement_document
from commercial_credit.store import InMemoryCreditStore


@pytest.fixture()
def client():
    pipeline = CreditPipeline(store=InMemoryCreditStore(), documents=LocalDocumentStore())
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app)


def _borrower_payload() -> dict:
    return {
        "borrower": {
            "legal_name": "Hartline Manufacturing, Inc.",
            "ein_last4": "4817",
            "segment": "manufacturing",
            "state_of_formation": "OH",
            "years_in_business": 17,
            "primary_contact_name": "Dana Hartline",
            "primary_contact_email": "dana@hartlinefab.example",
        },
        "facility": {
            "facility_type": "cre_loan",
            "amount": "2400000",
            "purpose": "Acquire an industrial property",
            "term_months": 84,
        },
    }


def test_health_and_readiness(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/readiness")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.headers["x-request-id"]


def test_open_application_generates_cre_checklist(client):
    resp = client.post("/applications", json=_borrower_payload())
    assert resp.status_code == 201
    body = resp.json()
    kinds = {i["kind"] for i in body["checklist"]}
    assert "appraisal" in kinds and "rent_roll" in kinds
    checklist = client.get(f"/applications/{body['credit_file_id']}/checklist")
    assert checklist.status_code == 200
    assert len(checklist.json()) == len(body["checklist"])


def test_open_from_los_reference(client):
    resp = client.post("/applications/from-los", json={"application_ref": "LOS-2026-0117"})
    assert resp.status_code == 201
    assert resp.json()["borrower"]["legal_name"] == "Hartline Manufacturing, Inc."
    missing = client.post("/applications/from-los", json={"application_ref": "LOS-X"})
    assert missing.status_code == 404


def test_document_upload_extract_and_audit_flow(client):
    fid = client.post("/applications", json=_borrower_payload()).json()["credit_file_id"]
    payload = {
        "kind": "financial_statement",
        "filename": "hartline-fy2024-statements.json",
        "content_b64": base64.b64encode(statement_document(2024)).decode(),
        "period_label": "FY2024",
    }
    up = client.post(f"/applications/{fid}/documents", json=payload)
    assert up.status_code == 201
    doc = up.json()
    assert len(doc["sha256"]) == 64

    got = client.get(f"/applications/{fid}/documents/{doc['document_id']}")
    assert got.status_code == 200 and got.json()["hash_verified"] is True

    ext = client.post(f"/applications/{fid}/extract")
    assert ext.status_code == 200
    assert ext.json()["extraction_count"] == 19
    for v in ext.json()["values"]:
        assert v["provenance"]["document_id"] == doc["document_id"]
        assert v["provenance"]["page"] in (1, 2)
        assert v["provenance"]["line"]

    audit = client.get(f"/applications/{fid}/audit")
    assert audit.status_code == 200 and len(audit.json()) >= 3


def test_unknown_credit_file_is_404(client):
    assert client.get("/applications/cf-nope").status_code == 404
    assert client.get("/applications/cf-nope/checklist").status_code == 404
