"""Round 15 — the model gateway: profiles as governed data,
real adapters behind the composer ports, and the cost ledger.

The pins, in the round's own terms:

  fail-closed   a duplicate profile id, a duplicate or negative
    price, a live profile with no endpoint, a key-required
    profile naming no key variable, a profile whose model has
    no price entry, a malformed budget cap — each refuses the
    whole configuration at load. An unknown profile, or a live
    profile this deployment has not configured, refuses at
    wiring/call time. No probe ever reaches a transport, and no
    profile is ever substituted for the one that was named.
  priced exact  every call ledgers: profile, model, tokens with
    their basis, the price-table version and the unit prices
    applied, and the exact Decimal cost — hand-computed here
    (1200 in / 300 out at $3.00/$15.00 per MTok is $0.0081;
    2000/500 at $0.90/$0.90 is $0.00225; offline is $0).
  refused too   a live draft the figure verifier refuses is
    still ledgered at its exact price — it cost what it cost.
  budget first  at the per-file cap, a live call is refused
    BEFORE transport (the stub's call count proves it), the
    refusal is recorded as a ledger budget event and an audit
    event on the file, and the offline profile still composes
    afterwards — named by the caller, never switched to.
  provenance    the model-assisted extractor stands behind the
    round-1 port unchanged: a value without a page and line is
    the round-1 provenance refusal, batch whole; the pipeline
    path ledgers extraction per file and spreads from it.

The `live` composer stub of rounds 5/12 stays as the bare-stack
posture: a pipeline built without a gateway keeps the registry
(offline composer, `live` refusing loudly). The gateway is the
one configured live path, and it holds no `live` profile — the
test below pins that the name refuses as unknown there.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from commercial_credit import api as api_module
from commercial_credit.config import (
    MIGRATIONS_DIR,
    MODEL_PRICES_PATH,
    MODEL_PROFILES_PATH,
)
from commercial_credit.db import migration_files
from commercial_credit.documents import LocalDocumentStore
from commercial_credit.errors import (
    MemoVerificationError,
    ModelBudgetExceededError,
    ModelGatewayError,
    ModelProfileConfigError,
    ModelProfileNotConfiguredError,
    ModelResponseError,
    ProvenanceError,
    UnknownModelProfileError,
)
from commercial_credit.gateway import (
    DEFAULT_PRICES_DATA,
    DEFAULT_PROFILES_DATA,
    ModelAssistedExtractor,
    ModelGateway,
    default_gateway_config,
    load_gateway_config,
    load_gateway_files,
)
from commercial_credit.memo import OfflineMemoComposer
from commercial_credit.models import DocumentKind, SourceDocument
from commercial_credit.pipeline import CreditPipeline
from commercial_credit.seed import ReferenceLosFeed, statement_document
from commercial_credit.store import InMemoryCreditStore, SqliteCreditStore

from test_annual_reviews import deliver_statements
from test_covenant_testing import FY2026_FIGURES
from test_covenants import approved_calloway
from test_memo import coverage_inputs, open_hartline

FIXED_NOW = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)
SECRET = "sk-test-secret-not-a-real-key"


def fixed_clock() -> datetime:
    return FIXED_NOW


# ---------------------------------------------------------------------
# Stub transports: provider-shaped responses, call-counted.
# ---------------------------------------------------------------------


class StubTransport:
    """Records every call and answers through the responder.
    The call count is the proof in the budget and refusal tests:
    a refused call is one that never incremented it."""

    def __init__(self, responder):
        self.calls: list[dict] = []
        self._responder = responder

    def post_json(self, url, *, headers, payload, timeout):
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload}
        )
        return self._responder(url, payload)


def _candidate_text(prompt: str) -> str:
    return prompt.split("<<<CANDIDATE>>>", 1)[1].split("<<<END>>>", 1)[0].strip()


def anthropic_echo(*, usage_in=1200, usage_out=300, transform=None):
    def respond(url, payload):
        text = _candidate_text(payload["messages"][0]["content"])
        if transform is not None:
            text = transform(text)
        return {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": usage_in, "output_tokens": usage_out},
        }

    return respond


def openai_echo(*, usage_in=2000, usage_out=500, transform=None):
    def respond(url, payload):
        text = _candidate_text(payload["messages"][0]["content"])
        if transform is not None:
            text = transform(text)
        return {
            "choices": [{"message": {"content": text}}],
            "usage": {
                "prompt_tokens": usage_in,
                "completion_tokens": usage_out,
            },
        }

    return respond


def openai_document_reader(*, usage_in=5000, usage_out=900):
    """Reads the document JSON out of the extraction prompt and
    returns every labelled line with its coordinates — what a
    document-AI adapter returns for these structured documents."""

    def respond(url, payload):
        prompt = payload["messages"][0]["content"]
        raw = prompt.split("<<<DOCUMENT>>>", 1)[1].split("<<<END>>>", 1)[0]
        document = json.loads(raw)
        entries = []
        for page in document.get("pages", []):
            for line in page.get("lines", []):
                if line.get("label") and line.get("value") is not None:
                    entries.append(
                        {
                            "label": line["label"],
                            "value": line["value"],
                            "page": page["page"],
                            "line": line["line"],
                        }
                    )
        return {
            "choices": [
                {"message": {"content": json.dumps(entries)}}
            ],
            "usage": {
                "prompt_tokens": usage_in,
                "completion_tokens": usage_out,
            },
        }

    return respond


def make_gateway(store=None, *, environ=None, transport=None, config=None):
    store = store if store is not None else InMemoryCreditStore()
    gateway = ModelGateway(
        config if config is not None else default_gateway_config(),
        store,
        environ=environ if environ is not None else {},
        transport=transport,
        clock=fixed_clock,
        audit_store=store,
    )
    return gateway, store


def wired_pipeline(gateway, store, *, extractor=None) -> CreditPipeline:
    return CreditPipeline(
        store=store,
        documents=LocalDocumentStore(),
        extractor=extractor,
        gateway=gateway,
    )


# ---------------------------------------------------------------------
# The shipped files are the canonical data — pinned together.
# ---------------------------------------------------------------------


def test_shipped_files_match_the_canonical_data():
    profiles = json.loads(MODEL_PROFILES_PATH.read_text("utf-8"))
    prices = json.loads(MODEL_PRICES_PATH.read_text("utf-8"))
    assert profiles == DEFAULT_PROFILES_DATA
    assert prices == DEFAULT_PRICES_DATA
    config = load_gateway_files(MODEL_PROFILES_PATH, MODEL_PRICES_PATH)
    assert config.profiles_version == "2026.10.1"
    assert config.price_table.version == "2026.10.1"
    assert config.price_table.as_of == date(2026, 10, 1)
    assert config.file_budget_usd == Decimal("5.00")
    assert sorted(config.profiles) == [
        "hosted-frontier",
        "in-boundary",
        "offline",
    ]


def test_migration_0013_is_registered():
    names = [f.name for f in migration_files(MIGRATIONS_DIR)]
    assert "0013_model_ledger.sql" in names
    sql = (MIGRATIONS_DIR / "0013_model_ledger.sql").read_text()
    assert "model_call_ledger" in sql
    assert "model_budget_events" in sql


# ---------------------------------------------------------------------
# The loader is fail-closed.
# ---------------------------------------------------------------------


def _profiles_data():
    return copy.deepcopy(DEFAULT_PROFILES_DATA)


def _prices_data():
    return copy.deepcopy(DEFAULT_PRICES_DATA)


def test_loader_refuses_a_duplicate_profile_id():
    data = _profiles_data()
    data["profiles"].append(dict(data["profiles"][0]))
    with pytest.raises(ModelProfileConfigError, match="twice"):
        load_gateway_config(data, _prices_data())


def test_loader_refuses_a_duplicate_price():
    data = _prices_data()
    data["entries"].append(dict(data["entries"][1]))
    with pytest.raises(ModelProfileConfigError, match="twice"):
        load_gateway_config(_profiles_data(), data)


def test_loader_refuses_a_negative_price():
    data = _prices_data()
    data["entries"][1]["output_per_mtok_usd"] = "-1.00"
    with pytest.raises(ModelProfileConfigError, match="negative"):
        load_gateway_config(_profiles_data(), data)


def test_loader_refuses_a_profile_whose_model_is_unpriced():
    data = _profiles_data()
    data["profiles"][1]["model"] = "claude-unpriced-9"
    with pytest.raises(ModelProfileConfigError, match="no entry"):
        load_gateway_config(data, _prices_data())


def test_loader_refuses_a_live_profile_with_no_endpoint():
    data = _profiles_data()
    data["profiles"][2]["endpoint_env"] = None
    data["profiles"][2]["endpoint_default"] = None
    with pytest.raises(ModelProfileConfigError, match="no endpoint"):
        load_gateway_config(data, _prices_data())


def test_loader_refuses_a_key_required_profile_naming_no_variable():
    data = _profiles_data()
    data["profiles"][1]["api_key_env"] = None
    with pytest.raises(ModelProfileConfigError, match="api_key_env"):
        load_gateway_config(data, _prices_data())


def test_loader_refuses_a_malformed_budget_cap():
    data = _profiles_data()
    data["file_budget_usd"] = "five dollars"
    with pytest.raises(ModelProfileConfigError, match="not a decimal"):
        load_gateway_config(data, _prices_data())
    data = _profiles_data()
    data["file_budget_usd"] = "-1.00"
    with pytest.raises(ModelProfileConfigError, match="negative"):
        load_gateway_config(data, _prices_data())


def test_loader_refuses_unversioned_or_empty_profiles():
    data = _profiles_data()
    del data["version"]
    with pytest.raises(ModelProfileConfigError, match="no version"):
        load_gateway_config(data, _prices_data())
    data = _profiles_data()
    data["profiles"] = []
    with pytest.raises(ModelProfileConfigError, match="no profiles"):
        load_gateway_config(data, _prices_data())


def test_loader_refuses_a_malformed_profile_definition():
    data = _profiles_data()
    data["profiles"][1]["provider_kind"] = "carrier-pigeon"
    with pytest.raises(ModelProfileConfigError, match="malformed"):
        load_gateway_config(data, _prices_data())


def test_loader_refuses_a_malformed_price_table():
    data = _prices_data()
    del data["entries"][0]["model"]
    with pytest.raises(ModelProfileConfigError, match="malformed"):
        load_gateway_config(_profiles_data(), data)


def test_missing_configuration_files_refuse_not_fallback(tmp_path):
    with pytest.raises(ModelProfileConfigError, match="could not be read"):
        load_gateway_files(
            tmp_path / "no-profiles.json", tmp_path / "no-prices.json"
        )


# ---------------------------------------------------------------------
# Resolution refuses — unknown, unconfigured, never substituted.
# ---------------------------------------------------------------------


def test_unknown_and_unconfigured_profiles_refuse_before_transport():
    stub = StubTransport(anthropic_echo())
    gateway, _store = make_gateway(transport=stub)
    with pytest.raises(UnknownModelProfileError):
        gateway.generate(
            "no-such-profile",
            credit_file_id=None,
            purpose="memo_draft",
            prompt="anything",
        )
    # hosted-frontier: endpoint default exists, the key does not.
    with pytest.raises(ModelProfileNotConfiguredError, match="ANTHROPIC_API_KEY"):
        gateway.generate(
            "hosted-frontier",
            credit_file_id=None,
            purpose="memo_draft",
            prompt="anything",
        )
    # in-boundary: no endpoint in this deployment at all.
    with pytest.raises(ModelProfileNotConfiguredError, match="IN_BOUNDARY_MODEL_URL"):
        gateway.generate(
            "in-boundary",
            credit_file_id=None,
            purpose="memo_draft",
            prompt="anything",
        )
    assert stub.calls == []


def test_the_gateway_holds_no_live_stub_profile():
    # The rounds 5/12 `live` stub belongs to the bare-stack
    # registry. Through the gateway the name is simply unknown —
    # the live path here is the named, priced profiles.
    gateway, _store = make_gateway()
    with pytest.raises(UnknownModelProfileError):
        gateway.memo_composer("live", credit_file_id="cf-x")


def test_wiring_time_active_profile_must_resolve_configured():
    store = InMemoryCreditStore()
    with pytest.raises(UnknownModelProfileError):
        ModelGateway(
            default_gateway_config(), store,
            environ={}, active_profile="nope",
        )
    with pytest.raises(ModelProfileNotConfiguredError):
        ModelGateway(
            default_gateway_config(), store,
            environ={}, active_profile="hosted-frontier",
        )
    # The offline default wires cleanly.
    ModelGateway(
        default_gateway_config(), store,
        environ={}, active_profile="offline",
    )


def test_profile_summaries_never_carry_key_values():
    gateway, _store = make_gateway(environ={"ANTHROPIC_API_KEY": SECRET})
    summaries = gateway.profile_summaries()
    assert [s["profile_id"] for s in summaries] == [
        "hosted-frontier",
        "in-boundary",
        "offline",
    ]
    by_id = {s["profile_id"]: s for s in summaries}
    assert by_id["offline"]["configured"] is True
    assert by_id["hosted-frontier"]["configured"] is True
    assert by_id["hosted-frontier"]["api_key_env"] == "ANTHROPIC_API_KEY"
    assert by_id["hosted-frontier"]["input_per_mtok_usd"] == "3.00"
    assert by_id["hosted-frontier"]["output_per_mtok_usd"] == "15.00"
    assert by_id["in-boundary"]["configured"] is False
    assert SECRET not in json.dumps(summaries)


# ---------------------------------------------------------------------
# Offline through the gateway: the same draft, ledgered at $0.
# ---------------------------------------------------------------------


def test_offline_memo_through_the_gateway_ledgers_at_zero():
    gateway, store = make_gateway()
    pipeline = wired_pipeline(gateway, store)
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    memos = pipeline.list_memos(cf)
    assert len(memos) == 1
    assert memos[0].composer_profile == "offline"
    assert memos[0].verification.verified is True
    entries = gateway.list_call_entries(cf)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.purpose == "memo_draft"
    assert entry.profile_id == "offline"
    assert entry.model == "offline-deterministic-composer-v1"
    assert entry.tokens_in == 0 and entry.tokens_out == 0
    assert entry.token_basis == "reported"
    assert entry.cost_usd == Decimal("0")
    assert entry.price_table_version == "2026.10.1"
    assert entry.called_at == FIXED_NOW
    rollup = pipeline.file_model_costs(cf)
    assert rollup["calls"] == 1
    assert rollup["cost_usd"] == "0"
    assert rollup["by_purpose"]["memo_draft"]["calls"] == 1
    assert rollup["budget_cap_usd"] == "5.00"
    assert rollup["budget_refusals"] == 0


def test_offline_composer_output_is_the_bare_composers_output():
    gateway, _store = make_gateway()
    composer = gateway.memo_composer("offline", credit_file_id="cf-unit")
    inputs = coverage_inputs()
    assert composer.compose(inputs) == OfflineMemoComposer().compose(inputs)


# ---------------------------------------------------------------------
# Live profiles: real adapters, exact prices, same verifiers.
# ---------------------------------------------------------------------


def test_hosted_frontier_memo_is_priced_exactly_and_verifies():
    stub = StubTransport(anthropic_echo(usage_in=1200, usage_out=300))
    gateway, store = make_gateway(
        environ={"ANTHROPIC_API_KEY": SECRET}, transport=stub
    )
    pipeline = wired_pipeline(gateway, store)
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    memo = pipeline.compose_memo(cf, profile="hosted-frontier")
    assert memo.composer_profile == "hosted-frontier"
    assert memo.verification.verified is True
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == SECRET
    assert call["payload"]["model"] == "claude-sonnet-4-5"
    entries = [
        e for e in gateway.list_call_entries(cf)
        if e.profile_id == "hosted-frontier"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.purpose == "memo_draft"
    assert entry.tokens_in == 1200 and entry.tokens_out == 300
    assert entry.token_basis == "reported"
    assert entry.input_per_mtok_usd == Decimal("3.00")
    assert entry.output_per_mtok_usd == Decimal("15.00")
    # (1200 x 3.00 + 300 x 15.00) / 1,000,000 = $0.0081 exactly.
    assert entry.cost_usd == Decimal("0.0081")
    assert SECRET not in json.dumps(
        [e.model_dump(mode="json") for e in gateway.list_call_entries(None)]
    )


def test_in_boundary_memo_is_priced_from_the_internal_table():
    stub = StubTransport(openai_echo(usage_in=2000, usage_out=500))
    gateway, _store = make_gateway(
        environ={"IN_BOUNDARY_MODEL_URL": "http://models.bank.internal:8000/v1"},
        transport=stub,
    )
    composer = gateway.memo_composer("in-boundary", credit_file_id="cf-unit")
    inputs = coverage_inputs()
    sections = composer.compose(inputs)
    assert sections == OfflineMemoComposer().compose(inputs)
    assert stub.calls[0]["url"] == (
        "http://models.bank.internal:8000/v1/chat/completions"
    )
    entries = gateway.list_call_entries("cf-unit")
    assert len(entries) == 1
    # (2000 x 0.90 + 500 x 0.90) / 1,000,000 = $0.00225 exactly.
    assert entries[0].cost_usd == Decimal("0.00225")
    assert entries[0].model == "llama-3.3-70b-instruct"


def test_unreported_usage_is_labelled_an_estimate():
    def no_usage(url, payload):
        text = _candidate_text(payload["messages"][0]["content"])
        return {"content": [{"type": "text", "text": text}]}

    stub = StubTransport(no_usage)
    gateway, _store = make_gateway(
        environ={"ANTHROPIC_API_KEY": SECRET}, transport=stub
    )
    composer = gateway.memo_composer("hosted-frontier", credit_file_id="cf-est")
    composer.compose(coverage_inputs())
    entry = gateway.list_call_entries("cf-est")[0]
    assert entry.token_basis == "estimated:chars/4"
    assert entry.tokens_in > 0 and entry.tokens_out > 0
    expected = (
        Decimal(entry.tokens_in) * Decimal("3.00")
        + Decimal(entry.tokens_out) * Decimal("15.00")
    ) / Decimal(1_000_000)
    assert entry.cost_usd == expected


def test_a_refused_live_draft_is_still_ledgered():
    def invent_a_figure(candidate: str) -> str:
        paragraphs = json.loads(candidate)
        paragraphs[0] += (
            " The facility also carries a reserve of $4,242,424.24."
        )
        return json.dumps(paragraphs)

    stub = StubTransport(
        anthropic_echo(usage_in=100, usage_out=50, transform=invent_a_figure)
    )
    gateway, store = make_gateway(
        environ={"ANTHROPIC_API_KEY": SECRET}, transport=stub
    )
    pipeline = wired_pipeline(gateway, store)
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    with pytest.raises(MemoVerificationError):
        pipeline.compose_memo(cf, profile="hosted-frontier")
    entries = [
        e for e in gateway.list_call_entries(cf)
        if e.profile_id == "hosted-frontier"
    ]
    assert len(entries) == 1
    # (100 x 3.00 + 50 x 15.00) / 1,000,000 = $0.00105 — refused,
    # but priced and recorded all the same.
    assert entries[0].cost_usd == Decimal("0.00105")


def test_a_misshapen_live_reply_is_refused_and_ledgered():
    stub = StubTransport(anthropic_echo(transform=lambda _t: "no json here"))
    gateway, _store = make_gateway(
        environ={"ANTHROPIC_API_KEY": SECRET}, transport=stub
    )
    composer = gateway.memo_composer("hosted-frontier", credit_file_id="cf-x")
    with pytest.raises(ModelResponseError):
        composer.compose(coverage_inputs())
    assert len(gateway.list_call_entries("cf-x")) == 1


# ---------------------------------------------------------------------
# The budget: refused before transport, recorded, offline continues.
# ---------------------------------------------------------------------


def _small_budget_config(cap: str) -> object:
    profiles = _profiles_data()
    profiles["file_budget_usd"] = cap
    return load_gateway_config(profiles, _prices_data())


def test_budget_refusal_happens_before_transport_and_is_recorded():
    stub = StubTransport(anthropic_echo(usage_in=1200, usage_out=300))
    gateway, store = make_gateway(
        environ={"ANTHROPIC_API_KEY": SECRET},
        transport=stub,
        config=_small_budget_config("0.01"),
    )
    composer = gateway.memo_composer("hosted-frontier", credit_file_id="cf-budget")
    inputs = coverage_inputs()
    composer.compose(inputs)  # $0.0081 — under the cap
    composer.compose(inputs)  # $0.0081 again — spend now $0.0162
    assert len(stub.calls) == 2
    with pytest.raises(ModelBudgetExceededError) as caught:
        composer.compose(inputs)
    # The refusal happened before transport: still two calls.
    assert len(stub.calls) == 2
    assert caught.value.cap_usd == "0.01"
    assert caught.value.spent_usd == "0.0162"
    events = gateway.list_budget_events("cf-budget")
    assert len(events) == 1
    assert events[0].cap_usd == Decimal("0.01")
    assert events[0].spent_usd == Decimal("0.0162")
    assert events[0].profile_id == "hosted-frontier"
    audits = [
        e for e in store.list_audit("cf-budget")
        if e.action == "model_budget_refused"
    ]
    assert len(audits) == 1
    assert audits[0].actor == "model-gateway"
    rollup = gateway.file_rollup("cf-budget")
    assert rollup["budget_refusals"] == 1
    assert rollup["budget_spent_usd"] == "0.0162"
    # Offline is never capped, and choosing it is the caller's
    # act — the gateway switched nothing by itself.
    offline = gateway.memo_composer("offline", credit_file_id="cf-budget")
    offline.compose(inputs)
    entries = gateway.list_call_entries("cf-budget")
    assert len(entries) == 3
    assert entries[-1].profile_id == "offline"
    assert entries[-1].cost_usd == Decimal("0")


def test_system_scope_calls_are_ledgered_but_not_budgeted():
    stub = StubTransport(anthropic_echo(usage_in=1200, usage_out=300))
    gateway, _store = make_gateway(
        environ={"ANTHROPIC_API_KEY": SECRET},
        transport=stub,
        config=_small_budget_config("0.01"),
    )
    prompt = (
        "pre <<<CANDIDATE>>>\n[\"system text\"]\n<<<END>>> post"
    )
    for _ in range(3):
        text = gateway.generate(
            "hosted-frontier",
            credit_file_id=None,
            purpose="memo_draft",
            prompt=prompt,
        )
        assert text == '["system text"]'
    assert len(stub.calls) == 3
    assert len(gateway.list_call_entries(None)) == 3


# ---------------------------------------------------------------------
# Review summaries ride the same gateway.
# ---------------------------------------------------------------------


def _breach_year_wired(gateway, store):
    pipeline = wired_pipeline(gateway, store)
    credit_file, _final, _decision, _covenant_set = approved_calloway(pipeline)
    cf = credit_file.credit_file_id
    deliver_statements(pipeline, cf, 2026, FY2026_FIGURES)
    pipeline.run_due_covenant_tests(cf, as_of=date(2027, 3, 1))
    return pipeline, cf


def test_review_pack_offline_summary_ledgers_at_zero():
    gateway, store = make_gateway()
    pipeline, cf = _breach_year_wired(gateway, store)
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1)
    )
    assert pack.summary.composer_profile == "offline"
    entries = [
        e for e in gateway.list_call_entries(cf)
        if e.purpose == "review_summary"
    ]
    assert len(entries) == 1
    assert entries[0].cost_usd == Decimal("0")


def test_review_pack_live_summary_is_priced_and_verified():
    stub = StubTransport(anthropic_echo(usage_in=800, usage_out=200))
    gateway, store = make_gateway(
        environ={"ANTHROPIC_API_KEY": SECRET}, transport=stub
    )
    pipeline, cf = _breach_year_wired(gateway, store)
    pack = pipeline.assemble_review_pack(
        cf, review_number=1, as_of=date(2027, 3, 1),
        profile="hosted-frontier",
    )
    assert pack.summary.composer_profile == "hosted-frontier"
    assert pack.summary.verification.verified is True
    entries = [
        e for e in gateway.list_call_entries(cf)
        if e.purpose == "review_summary"
    ]
    assert len(entries) == 1
    # (800 x 3.00 + 200 x 15.00) / 1,000,000 = $0.0054 exactly.
    assert entries[0].cost_usd == Decimal("0.0054")


# ---------------------------------------------------------------------
# Extraction assist: the port's law does not move.
# ---------------------------------------------------------------------


def _statement_document_record(content: bytes) -> SourceDocument:
    import hashlib

    return SourceDocument(
        document_id="doc-extract-2024",
        credit_file_id="cf-extract",
        kind=DocumentKind.FINANCIAL_STATEMENT,
        filename="hartline-fy2024-statements.json",
        sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        storage_key="doc-extract-2024",
        period_label="FY2024",
    )


def test_extraction_assist_returns_provenanced_values_and_ledgers():
    stub = StubTransport(openai_document_reader())
    gateway, _store = make_gateway(
        environ={"IN_BOUNDARY_MODEL_URL": "http://models.bank.internal:8000/v1"},
        transport=stub,
    )
    extractor = ModelAssistedExtractor(
        gateway, "in-boundary", credit_file_id="cf-extract"
    )
    content = statement_document(2024)
    values = extractor.extract(_statement_document_record(content), content)
    reference_pipeline_values = len(values)
    assert reference_pipeline_values > 10
    by_field = {v.field: v for v in values}
    assert by_field["total_assets"].provenance.document_id == "doc-extract-2024"
    assert by_field["total_assets"].provenance.page == 1
    assert by_field["revenue"].provenance.page == 2
    assert all(v.fiscal_year == 2024 for v in values)
    entries = gateway.list_call_entries("cf-extract")
    assert len(entries) == 1
    assert entries[0].purpose == "extraction_assist"
    # (5000 x 0.90 + 900 x 0.90) / 1,000,000 = $0.00531 exactly.
    assert entries[0].cost_usd == Decimal("0.00531")


def test_extraction_assist_refuses_a_value_it_cannot_point_at():
    def drop_lines(url, payload):
        prompt = payload["messages"][0]["content"]
        raw = prompt.split("<<<DOCUMENT>>>", 1)[1].split("<<<END>>>", 1)[0]
        document = json.loads(raw)
        page = document["pages"][0]
        line = page["lines"][0]
        entries = [
            {
                "label": line["label"],
                "value": line["value"],
                "page": page["page"],
                # no "line": the figure cannot be pointed at
            }
        ]
        return {
            "choices": [{"message": {"content": json.dumps(entries)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        }

    stub = StubTransport(drop_lines)
    gateway, _store = make_gateway(
        environ={"IN_BOUNDARY_MODEL_URL": "http://models.bank.internal:8000/v1"},
        transport=stub,
    )
    extractor = ModelAssistedExtractor(
        gateway, "in-boundary", credit_file_id="cf-extract"
    )
    content = statement_document(2024)
    with pytest.raises(ProvenanceError):
        extractor.extract(_statement_document_record(content), content)
    # Refused — and still ledgered.
    assert len(gateway.list_call_entries("cf-extract")) == 1


def test_extraction_assist_refuses_a_non_json_reply():
    def not_json(url, payload):
        return {
            "choices": [{"message": {"content": "not json at all"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        }

    stub = StubTransport(not_json)
    gateway, _store = make_gateway(
        environ={"IN_BOUNDARY_MODEL_URL": "http://models.bank.internal:8000/v1"},
        transport=stub,
    )
    extractor = ModelAssistedExtractor(gateway, "in-boundary")
    content = statement_document(2024)
    with pytest.raises(ModelResponseError):
        extractor.extract(_statement_document_record(content), content)


def test_extraction_assist_requires_a_live_configured_profile():
    gateway, _store = make_gateway()
    with pytest.raises(ModelGatewayError, match="cannot read documents"):
        ModelAssistedExtractor(gateway, "offline")
    with pytest.raises(ModelProfileNotConfiguredError):
        ModelAssistedExtractor(gateway, "in-boundary")


def test_pipeline_extraction_assist_ledgers_per_file_and_spreads():
    stub = StubTransport(openai_document_reader())
    gateway, store = make_gateway(
        environ={"IN_BOUNDARY_MODEL_URL": "http://models.bank.internal:8000/v1"},
        transport=stub,
    )
    extractor = ModelAssistedExtractor(gateway, "in-boundary")
    pipeline = wired_pipeline(gateway, store, extractor=extractor)
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    values = store.get_extractions(cf)
    assert len(values) > 50  # three statement years, read by the model
    assert all(v.provenance.document_id for v in values)
    entries = [
        e for e in gateway.list_call_entries(cf)
        if e.purpose == "extraction_assist"
    ]
    assert len(entries) == 3  # one call per statement document
    spread = pipeline.get_spread(cf)
    assert spread is not None
    assert pipeline.list_memos(cf)[0].verification.verified is True


# ---------------------------------------------------------------------
# Rollups: per file and portfolio daily.
# ---------------------------------------------------------------------


def test_rollups_group_by_purpose_profile_and_day():
    stub = StubTransport(anthropic_echo(usage_in=1200, usage_out=300))
    gateway, store = make_gateway(
        environ={"ANTHROPIC_API_KEY": SECRET}, transport=stub
    )
    pipeline = wired_pipeline(gateway, store)
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    pipeline.compose_memo(cf, profile="hosted-frontier")
    rollup = pipeline.file_model_costs(cf)
    assert rollup["calls"] == 2
    assert rollup["cost_usd"] == "0.0081"
    assert rollup["by_profile"]["offline"]["calls"] == 1
    assert rollup["by_profile"]["hosted-frontier"]["cost_usd"] == "0.0081"
    assert rollup["by_purpose"]["memo_draft"]["tokens_in"] == 1200
    daily = pipeline.portfolio_model_costs()
    by_profile = {row["profile_id"]: row for row in daily}
    assert by_profile["offline"]["day"] == "2026-10-26"
    assert by_profile["offline"]["cost_usd"] == "0"
    assert by_profile["hosted-frontier"]["cost_usd"] == "0.0081"
    ledger = pipeline.file_model_ledger(cf)
    assert len(ledger) == 2
    assert ledger[0]["profile_id"] == "offline"
    assert ledger[1]["cost_usd"] == "0.0081"


# ---------------------------------------------------------------------
# Stores: the ledger is append-only in every store.
# ---------------------------------------------------------------------


def test_sqlite_ledger_round_trips(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    gateway, _ = make_gateway(store=store)
    prompt = "pre <<<CANDIDATE>>>\n[\"hello\"]\n<<<END>>> post"
    gateway.generate(
        "offline", credit_file_id="cf-x", purpose="memo_draft", prompt=prompt
    )
    reopened = SqliteCreditStore(tmp_path / "credit.db")
    entries = reopened.list_call_entries("cf-x")
    assert len(entries) == 1
    assert entries[0].purpose == "memo_draft"
    assert entries[0].cost_usd == Decimal("0")
    assert reopened.list_call_entries(None) == entries
    assert reopened.list_call_entries("cf-other") == []


def test_sqlite_budget_events_round_trip(tmp_path):
    store = SqliteCreditStore(tmp_path / "credit.db")
    stub = StubTransport(anthropic_echo(usage_in=1200, usage_out=300))
    gateway, _ = make_gateway(
        store=store,
        environ={"ANTHROPIC_API_KEY": SECRET},
        transport=stub,
        config=_small_budget_config("0.01"),
    )
    composer = gateway.memo_composer("hosted-frontier", credit_file_id="cf-b")
    inputs = coverage_inputs()
    composer.compose(inputs)
    composer.compose(inputs)
    with pytest.raises(ModelBudgetExceededError):
        composer.compose(inputs)
    reopened = SqliteCreditStore(tmp_path / "credit.db")
    events = reopened.list_budget_events("cf-b")
    assert len(events) == 1
    assert events[0].spent_usd == Decimal("0.0162")


# ---------------------------------------------------------------------
# API: profiles, ledger, costs — and the 429 budget refusal.
# ---------------------------------------------------------------------


@pytest.fixture()
def api_env():
    stub = StubTransport(anthropic_echo(usage_in=1200, usage_out=300))
    gateway, store = make_gateway(
        environ={"ANTHROPIC_API_KEY": SECRET}, transport=stub
    )
    pipeline = wired_pipeline(gateway, store)
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    return TestClient(api_module.app), gateway, stub


def test_api_model_surfaces(api_env):
    client, gateway, _stub = api_env
    profiles = client.get("/model/profiles")
    assert profiles.status_code == 200
    assert SECRET not in profiles.text
    assert {p["profile_id"] for p in profiles.json()} == {
        "offline",
        "hosted-frontier",
        "in-boundary",
    }
    opened = client.post(
        "/applications/from-los", json={"application_ref": "LOS-2026-0117"}
    )
    assert opened.status_code == 201
    # The LOS feed alone files no statements; deliver the three
    # statement years so intake's memo composes (and ledgers).
    cf = opened.json()["credit_file_id"]
    ledger = client.get(f"/applications/{cf}/model-ledger")
    assert ledger.status_code == 200
    costs = client.get(f"/applications/{cf}/model-costs")
    assert costs.status_code == 200
    assert costs.json()["credit_file_id"] == cf
    daily = client.get("/model-costs/daily")
    assert daily.status_code == 200
    assert isinstance(daily.json(), list)
    missing = client.get("/applications/cf-nope/model-ledger")
    assert missing.status_code == 404
    missing = client.get("/applications/cf-nope/model-costs")
    assert missing.status_code == 404


def test_api_compose_through_gateway_and_budget_429():
    stub = StubTransport(anthropic_echo(usage_in=1200, usage_out=300))
    gateway, store = make_gateway(
        environ={"ANTHROPIC_API_KEY": SECRET},
        transport=stub,
        config=_small_budget_config("0.01"),
    )
    pipeline = wired_pipeline(gateway, store)
    api_module.pipeline = pipeline
    api_module.store = pipeline.store
    client = TestClient(api_module.app)
    credit_file = open_hartline(pipeline)
    cf = credit_file.credit_file_id
    first = client.post(
        f"/applications/{cf}/memos", json={"profile": "hosted-frontier"}
    )
    assert first.status_code == 201
    second = client.post(
        f"/applications/{cf}/memos", json={"profile": "hosted-frontier"}
    )
    assert second.status_code == 201
    third = client.post(
        f"/applications/{cf}/memos", json={"profile": "hosted-frontier"}
    )
    assert third.status_code == 429
    assert "budget" in third.json()["detail"]
    ledger = client.get(f"/applications/{cf}/model-ledger")
    profiles_used = [e["profile_id"] for e in ledger.json()]
    assert profiles_used.count("hosted-frontier") == 2
    costs = client.get(f"/applications/{cf}/model-costs").json()
    assert costs["budget_refusals"] == 1
    # The offline path still composes after the refusal.
    offline = client.post(
        f"/applications/{cf}/memos", json={"profile": "offline"}
    )
    assert offline.status_code == 201


def test_wiring_builds_the_gateway_fail_closed(monkeypatch):
    from commercial_credit.config import Settings
    from commercial_credit.wiring import build_pipeline

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(
        database_url=None,
        sqlite_path=":memory:",
        s3_endpoint_url=None,
        s3_bucket=None,
        s3_region="us-east-1",
        model_profile="offline",
    )
    pipeline = build_pipeline(settings)
    summaries = {
        s["profile_id"]: s for s in pipeline.model_profiles()
    }
    assert summaries["offline"]["configured"] is True
    assert summaries["hosted-frontier"]["configured"] is False
    bad = Settings(
        database_url=None,
        sqlite_path=":memory:",
        s3_endpoint_url=None,
        s3_bucket=None,
        s3_region="us-east-1",
        model_profile="hosted-frontier",
    )
    with pytest.raises(ModelProfileNotConfiguredError):
        build_pipeline(bad)
