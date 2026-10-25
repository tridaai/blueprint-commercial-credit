"""Round 14 — the officer console.

The console is a reader and a form set, nothing more. These
tests pin the four claims the round makes:

  the page        one static file, served by the API, carrying
                  the portfolio footer, the identity fields and
                  the as-of control — no build step, no external
                  assets
  the surface     every path literal in the page is a declared
                  route in the console manifest, and every
                  manifest entry is a real route of the app —
                  the reads are this round's aggregates, the
                  actions are gated routes from earlier rounds.
                  A console-only write path cannot ship.
  the rendering   stored content is data: the page contains no
                  HTML-parsing sink at all, and a script carried
                  in a borrower name and a document filename
                  round-trips through the API verbatim, as text
  the contracts   the queue agrees with the API's own file
                  list, readiness counts and breach list; the
                  workspace aggregates every artefact kind a
                  fully-worked file produces; the vocabularies
                  the forms offer are the gates' own constants;
                  and the actions the console offers ride the
                  gated routes — anonymous and uncoded acts are
                  refused there exactly as everywhere else
"""

from __future__ import annotations

import base64
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit import committee as committee_module
from commercial_credit import covenant_testing as covenant_testing_module
from commercial_credit import officer_gate as officer_gate_module
from commercial_credit import reviews as reviews_module
from commercial_credit import scorecard as scorecard_module
from commercial_credit.console import (
    CONSOLE_ACTIONS,
    CONSOLE_READS,
    STAGES,
    route_manifest,
)
from commercial_credit.console_page import CONSOLE_HTML
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import statement_document
from commercial_credit.store import InMemoryCreditStore

from test_annual_reviews import breach_year_pipeline
from test_committee_gate import (
    CHAIR,
    CHAIR_ROLE,
    OFFICER,
    OFFICER_ROLE,
    RM,
    RM_ROLE,
    convened_final,
    hartline_pipeline,
    open_hartline,
)
from test_disagreements import hartline_pipeline as disagreement_pipeline

AS_OF = "2027-03-01"


@pytest.fixture()
def client():
    pipeline = CreditPipeline(
        store=InMemoryCreditStore(), documents=LocalDocumentStore()
    )
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app), pipeline


def worked_client():
    """The breach-year Calloway file, fully worked, behind the
    API — plus a rating and an assembled review pack, so the
    workspace carries every artefact kind there is."""
    pipeline, credit_file, final, decision, covenant_set = (
        breach_year_pipeline()
    )
    cf = credit_file.credit_file_id
    pipeline.assign_rating(cf, as_of=date(2027, 3, 1))
    pipeline.assemble_review_pack(cf, review_number=1, as_of=date(2027, 3, 1))
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app), pipeline, cf


# ---------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------


def test_console_page_serves_with_the_portfolio_standard(client):
    test_client, _ = client
    resp = test_client.get("/console")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    page = resp.text
    # The portfolio console footer: logo, the two links.
    assert "https://trida.ai/logos-rounded/trida-rounded.png" in page
    assert "Visit Trida AI →" in page
    assert "View all blueprints →" in page
    # The identity discipline is on the page: name, role, as-of.
    assert 'id="officer-name"' in page
    assert 'id="officer-role"' in page
    assert 'id="as-of"' in page
    assert "will not submit an anonymous action" in page


def test_console_page_has_no_html_parsing_sink():
    """Stored content renders as text or it does not render.
    The page builds nodes and assigns textContent; a parsing
    sink anywhere in the file would be the hole a script in a
    document filename walks through."""
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                 "document.write", "eval("):
        assert sink not in CONSOLE_HTML, sink
    assert "textContent" in CONSOLE_HTML
    assert "createElement" in CONSOLE_HTML
    # One fetch call site — the api() helper — so the surface
    # scan below really does cover every call the page can make.
    assert CONSOLE_HTML.count("fetch(") == 1


# ---------------------------------------------------------------------
# The surface scan
# ---------------------------------------------------------------------


def _normalise(template: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", template)


def _page_paths() -> set[str]:
    literals = re.findall(
        r"""["'`](/(?:console|applications|covenant-due-work"""
        r"""|annual-review-due-work|breach-cases)[^"'`\s]*)["'`]""",
        CONSOLE_HTML,
    )
    paths = set()
    for literal in literals:
        path = literal.split("?")[0]
        paths.add(re.sub(r"\$\{[^}]*\}", "{}", path))
    return paths


def _app_routes() -> set[tuple[str, str]]:
    routes = set()
    for route in api_module.app.routes:
        for method in getattr(route, "methods", set()) or set():
            if method in ("GET", "POST"):
                routes.add((method, _normalise(route.path)))
    return routes


def test_every_page_path_is_declared_and_every_declaration_is_used():
    manifest = {
        _normalise(path) for _, path in route_manifest()
    }
    assert _page_paths() == manifest


def test_console_manifest_routes_all_exist_in_the_app():
    app_routes = _app_routes()
    for method, path in route_manifest():
        assert (method, _normalise(path)) in app_routes, (method, path)
    for method, path in CONSOLE_ACTIONS:
        assert method == "POST"
        assert (method, _normalise(path)) in app_routes, (method, path)


def test_console_actions_are_gated_routes_not_console_routes():
    """No action path lives under /console — the console writes
    nothing of its own; every write is an earlier round's gate."""
    for _, path in CONSOLE_ACTIONS:
        assert not path.startswith("/console")


# ---------------------------------------------------------------------
# Vocabularies: the forms offer the gates' own codes
# ---------------------------------------------------------------------


def test_vocabularies_are_the_gates_own_constants(client):
    test_client, _ = client
    vocab = test_client.get("/console/vocabularies").json()
    assert vocab["officer_return_reasons"]["codes"] == dict(
        officer_gate_module.RETURN_REASON_CODES
    )
    assert vocab["officer_return_reasons"]["version"] == (
        officer_gate_module.RETURN_REASONS_VERSION
    )
    assert vocab["adverse_action_reasons"]["codes"] == dict(
        committee_module.ADVERSE_REASON_CODES
    )
    assert vocab["committee_deferral_reasons"]["codes"] == dict(
        committee_module.DEFERRAL_REASON_CODES
    )
    assert vocab["committee_outcomes"] == sorted(committee_module.OUTCOMES)
    assert vocab["breach_reasons"]["escalation_codes"] == dict(
        covenant_testing_module.BREACH_ESCALATION_REASON_CODES
    )
    assert vocab["breach_reasons"]["default_codes"] == dict(
        covenant_testing_module.BREACH_DEFAULT_REASON_CODES
    )
    assert vocab["annual_review"]["codes"] == dict(
        reviews_module.REVIEW_REASON_CODES
    )
    assert vocab["annual_review"]["stances"] == sorted(
        reviews_module.REVIEW_STANCES
    )
    assert vocab["rating_override_reasons"]["codes"] == dict(
        scorecard_module.OVERRIDE_REASON_CODES
    )


# ---------------------------------------------------------------------
# The queue contract
# ---------------------------------------------------------------------


def _borrower_payload(name="Prospect Manufacturing, Inc.") -> dict:
    return {
        "borrower": {
            "legal_name": name,
            "ein_last4": "4817",
            "segment": "manufacturing",
            "state_of_formation": "OH",
            "years_in_business": 9,
            "primary_contact_name": "Pat Prospect",
            "primary_contact_email": "pat@prospect.example",
        },
        "facility": {
            "facility_type": "term_loan",
            "amount": "750000",
            "purpose": "Equipment purchase",
            "term_months": 60,
        },
    }


def test_queue_matches_the_apis_own_file_list(client):
    test_client, pipeline = client
    created = test_client.post("/applications", json=_borrower_payload())
    assert created.status_code == 201
    fresh_id = created.json()["credit_file_id"]
    queue = test_client.get("/console/queue", params={"as_of": AS_OF}).json()
    api_files = test_client.get("/applications").json()
    assert [r["credit_file_id"] for r in queue] == [
        f["credit_file_id"] for f in api_files
    ]
    row = next(r for r in queue if r["credit_file_id"] == fresh_id)
    # Intake runs the whole graph on opening — extraction,
    # spreading, policy, and a first composed draft — so a file
    # opened through the API stands at memo draft, with its
    # readiness blockers counted, never waved through as zero.
    assert row["stage"] == "memo draft"
    assert row["stage"] in STAGES
    assert row["borrower_name"] == "Prospect Manufacturing, Inc."
    assert row["rating"] is None
    assert row["open_breaches"] == 0
    assert row["readiness_blockers"] is not None
    assert row["readiness_blockers"] > 0


class _StubMemo:
    def __init__(self, status):
        self.status = status


class _StubPipeline:
    """The stage derivation's whole input surface: covenant
    sets, memos with derived statuses, spreads."""

    def __init__(self, *, sets=(), memos=(), spreads=()):
        self._sets = list(sets)
        self._memos = list(memos)
        self._spreads = list(spreads)
        self.store = self

    def list_covenant_sets(self, credit_file_id):
        return self._sets

    def list_spreads(self, credit_file_id):
        return self._spreads

    def list_memos(self, credit_file_id):
        return self._memos


class _StubFile:
    credit_file_id = "cf-stub"


@pytest.mark.parametrize(
    "stub, expected",
    [
        (_StubPipeline(), "intake"),
        (_StubPipeline(spreads=[object()]), "spreading"),
        (_StubPipeline(memos=[_StubMemo("draft")]), "memo draft"),
        (_StubPipeline(memos=[_StubMemo("returned")]), "memo draft"),
        (_StubPipeline(memos=[_StubMemo("in_review")]), "officer review"),
        (_StubPipeline(memos=[_StubMemo("officer_approved")]), "committee"),
        (_StubPipeline(memos=[_StubMemo("in_committee")]), "committee"),
        (_StubPipeline(memos=[_StubMemo("deferred")]), "committee"),
        (_StubPipeline(memos=[_StubMemo("committee_approved")]), "approved"),
        (
            _StubPipeline(memos=[_StubMemo("committee_approved_conditional")]),
            "approved",
        ),
        (_StubPipeline(memos=[_StubMemo("declined")]), "declined"),
        (
            _StubPipeline(sets=[object()], memos=[_StubMemo("declined")]),
            "monitoring",
        ),
    ],
)
def test_stage_derivation_reads_the_record(stub, expected):
    from commercial_credit.console import _stage_of

    assert _stage_of(stub, _StubFile()) == expected
    assert expected in STAGES


def test_queue_stage_blockers_and_rating_track_the_record():
    test_client, pipeline, cf = worked_client()
    queue = test_client.get("/console/queue", params={"as_of": AS_OF}).json()
    row = next(r for r in queue if r["credit_file_id"] == cf)
    assert row["stage"] == "monitoring"
    report = test_client.get(
        f"/applications/{cf}/readiness-report", params={"as_of": AS_OF}
    ).json()
    expected_blockers = sum(1 for e in report["entries"] if e["blocker"])
    assert row["readiness_blockers"] == expected_blockers
    breaches = test_client.get(
        "/breach-cases", params={"as_of": AS_OF, "credit_file_id": cf}
    ).json()
    expected_open = sum(
        1 for b in breaches
        if b["state"] not in ("cured", "waived", "defaulted_path")
    )
    assert row["open_breaches"] == expected_open == 3
    ratings = test_client.get(f"/applications/{cf}/risk-ratings").json()
    assert row["rating"]["final_grade"] == ratings[-1]["final_grade"]
    assert row["rating"]["assignment_id"] == (
        ratings[-1]["assignment"]["assignment_id"]
    )


def test_queue_stage_follows_the_memo(client):
    test_client, pipeline = client
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    pipeline.compose_memo(cf)
    queue = test_client.get("/console/queue", params={"as_of": AS_OF}).json()
    row = next(r for r in queue if r["credit_file_id"] == cf)
    assert row["stage"] == "memo draft"


# ---------------------------------------------------------------------
# The workspace contract
# ---------------------------------------------------------------------


def test_workspace_aggregates_every_artefact_kind():
    test_client, pipeline, cf = worked_client()
    ws = test_client.get(
        f"/console/workspace/{cf}", params={"as_of": AS_OF}
    ).json()
    assert ws["stage"] == "monitoring"
    assert ws["file"]["borrower"]["legal_name"].startswith("Calloway")
    assert len(ws["checklist"]) >= 1
    assert len(ws["documents"]) >= 1
    assert all(len(d["sha256"]) == 64 for d in ws["documents"])
    assert ws["readiness"] is not None
    assert ws["spread"] is not None and ws["spread"]["periods"]
    packs = ws["computation_packs"]
    assert packs, "a worked file carries computation packs"
    figures = packs[-1]["figures"]
    assert figures and any(f["steps"] for f in figures), (
        "figure traces expand to their derivation steps"
    )
    assert ws["policy_reports"], "a worked file carries policy reports"
    latest_report = ws["policy_reports"][-1]
    assert latest_report["policy_version"]  # the pinned version is named
    assert latest_report["outcomes"]
    assert ws["memos"], "a worked file carries memos"
    memo = ws["memos"][-1]
    assert memo["verification"]["verified"] is True
    assert memo["committee"]["decisions"], "the decision stands in the workspace"
    assert ws["ratings"], "the assigned rating stands in the workspace"
    factors = ws["ratings"][-1]["assignment"]["factors"]
    assert len(factors) == 7
    assert all(f["trace"] or f["missing_inputs"] for f in factors)
    assert len(ws["covenant_sets"]) == 1
    assert ws["covenant_schedule"]["sets"]
    assert len(ws["covenant_test_results"]) >= 1
    assert len(ws["breach_cases"]) == 3
    assert len(ws["annual_review_packs"]) == 1
    assert ws["due_work"], "the file's due work is on its workspace"


def test_workspace_carries_disagreements_with_provenance(client):
    test_client, _ = client
    pipeline, credit_file = disagreement_pipeline()
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    ws = test_client.get(
        f"/console/workspace/{credit_file.credit_file_id}",
        params={"as_of": AS_OF},
    ).json()
    assert ws["disagreements"], "the Hartline file's disagreements ride along"
    record = ws["disagreements"][0]
    assert record["material"] is True
    assert len(record["sources"]) >= 2
    assert all(s["value"] is not None for s in record["sources"])


def test_workspace_unknown_file_is_404(client):
    test_client, _ = client
    resp = test_client.get(
        "/console/workspace/cf-nope", params={"as_of": AS_OF}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------
# Escaping: stored content is data, and it round-trips as data
# ---------------------------------------------------------------------


def test_script_carrying_content_round_trips_as_inert_data(client):
    test_client, _ = client
    payload_name = '<script>alert("borrower")</script> Holdings, Inc.'
    created = test_client.post(
        "/applications", json=_borrower_payload(name=payload_name)
    )
    assert created.status_code == 201
    cf = created.json()["credit_file_id"]
    payload_file = '<img src=x onerror=alert("doc")>.json'
    content = base64.b64encode(statement_document(2024)).decode()
    registered = test_client.post(
        f"/applications/{cf}/documents",
        json={
            "kind": "financial_statement",
            "filename": payload_file,
            "content_b64": content,
            "period_label": "FY2024",
        },
    )
    assert registered.status_code == 201
    # The API carries the strings verbatim — they are data.
    assert registered.json()["filename"] == payload_file
    queue = test_client.get("/console/queue", params={"as_of": AS_OF}).json()
    row = next(r for r in queue if r["credit_file_id"] == cf)
    assert row["borrower_name"] == payload_name
    ws = test_client.get(
        f"/console/workspace/{cf}", params={"as_of": AS_OF}
    ).json()
    assert ws["file"]["borrower"]["legal_name"] == payload_name
    assert ws["documents"][0]["filename"] == payload_file
    # And the page that would render them has no sink to render
    # them through: the payload is not in the page, and the page
    # assigns text only (pinned in the sink test above).
    assert payload_name not in CONSOLE_HTML
    assert payload_file not in CONSOLE_HTML


# ---------------------------------------------------------------------
# The actions ride the gated routes
# ---------------------------------------------------------------------


def test_officer_actions_through_console_routes_are_gated(client):
    test_client, pipeline = client
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    memo = pipeline.compose_memo(cf)
    base = f"/applications/{cf}/memos/{memo.memo_id}"
    submitted = test_client.post(
        base + "/submit", json={"actor": RM, "actor_role": RM_ROLE}
    )
    assert submitted.status_code == 200
    # Anonymous return — refused by the gate, as everywhere.
    anonymous = test_client.post(
        base + "/return",
        json={"officer_id": "", "officer_role": "", "reason_codes": []},
    )
    assert anonymous.status_code == 422
    # An uncoded return — refused.
    code = next(iter(officer_gate_module.RETURN_REASON_CODES))
    uncoded = test_client.post(
        base + "/return",
        json={
            "officer_id": OFFICER, "officer_role": OFFICER_ROLE,
            "reason_codes": ["not_a_real_code"],
        },
    )
    assert uncoded.status_code == 422
    # The lawful act lands, with the console's exact payload shape.
    lawful = test_client.post(
        base + "/return",
        json={
            "officer_id": OFFICER, "officer_role": OFFICER_ROLE,
            "reason_codes": [code], "note": "",
        },
    )
    assert lawful.status_code == 200
    assert pipeline.memo_status(cf, memo.memo_id) == "returned"


def test_committee_quorum_refusal_through_the_console_route(client):
    test_client, pipeline = client
    credit_file, draft, final = convened_final(pipeline)
    cf = credit_file.credit_file_id
    members = [{"member_id": CHAIR, "member_role": CHAIR_ROLE}]
    refused = test_client.post(
        f"/applications/{cf}/memos/{final.memo_id}/committee/decide",
        json={
            "recorder_id": CHAIR, "recorder_role": CHAIR_ROLE,
            "members": members, "outcome": "approve", "quorum_required": 2,
        },
    )
    assert refused.status_code == 422
    assert "quorum" in refused.json()["detail"].lower()
