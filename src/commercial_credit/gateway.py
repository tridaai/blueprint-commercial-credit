"""The model gateway (round 15): named profiles, real adapters,
and the cost ledger.

Rounds 5 and 12 put drafting behind composer ports with named
profiles: a deterministic ``offline`` profile, and a ``live``
profile defined but not configured — it refused loudly rather
than silently substituting the offline draft. Round 15 builds
the full gateway on those ports — the one configured path every
model call goes through when a deployment wires it:

**Profiles are data.** A profile is a definition (id, provider
kind, model, the *names* of the environment variables holding
its endpoint and key, a timeout) — never a code branch, and
never a secret value. Three ship: ``offline`` (the deterministic
composer, zero cost), ``hosted-frontier`` (a hosted frontier
model over the Anthropic Messages shape), and ``in-boundary``
(an open-weight model served inside the bank's boundary over an
OpenAI-compatible endpoint). Every profile's model must appear
in the versioned price table. The loader is **fail-closed**: a
duplicate id, a live profile with no endpoint, a key-required
profile naming no key variable, a missing or malformed price
entry, or a negative price refuses the whole configuration at
load time. Selecting an unknown profile, or a live profile whose
endpoint/key the deployment has not set, refuses loudly at
wiring or call time. There is no fallback anywhere in this
module: the gateway never substitutes one profile for another,
at load, at wiring, or mid-file.

**The model writes narrative only — the verifier still
decides.** A live adapter receives the deterministic composer's
candidate paragraphs plus the binding instruction (polish the
wording; introduce, alter or drop no figure) and returns the
finished paragraphs. Tables, exception lists and every other
structured part of an artefact never pass through the model at
all — they are rendered in code and stay in code. Whatever the
adapter returns is parsed back into the artefact's shape (a
reply that does not parse, or whose paragraph count moved, is
refused whole) and then faces exactly the verifier the offline
draft faces: the memo's figure verifier (round 5), the review
summary's checks (round 12). The gateway changes nothing about
extraction provenance either: the model-assisted extractor
behind the extraction port returns values through the same
construction as every extractor, so a value the model cannot
point at (document, page, line) raises the round-1 provenance
refusal — it does not enter a spread.

**Every call is priced and ledgered.** A call through the
gateway appends one :class:`ModelCallEntry` — credit file (or
system scope), purpose, profile, model, tokens, the price-table
version and the unit prices it was priced with, the exact
Decimal cost, latency, and the injected clock's timestamp. The
entry is written when the call returns, *before* any shape
check or verifier runs: a refused draft still cost what it
cost, and the ledger says so. Offline calls ledger at zero —
the ledger is the complete record of drafting cost, not just
the paid part.

**Budgets are enforced before transport.** A deployment sets a
per-file cap (configuration data). When a file's priced spend
reaches the cap, further live-profile calls for that file are
refused *before any transport is touched*, and the refusal is
recorded — a budget event in the ledger store and an audit
event on the file. The offline profile is never capped, and
continuing a file offline after a refusal is a normal recorded
path — taken by the caller naming ``offline``, never by the
gateway switching on its own.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from pydantic import ValidationError

from .disagreements import SOURCE_TAX, source_type_for_document
from .errors import (
    ModelBudgetExceededError,
    ModelGatewayError,
    ModelProfileConfigError,
    ModelProfileNotConfiguredError,
    ModelResponseError,
    ProvenanceError,
    UnknownModelProfileError,
)
from .extraction import STATEMENT_FIELDS, unmapped_field_for
from .memo import MemoInputs, MemoSection, OfflineMemoComposer
from .models import (
    AuditEvent,
    DocumentKind,
    ExtractedValue,
    ModelBudgetEvent,
    ModelCallEntry,
    ModelPrice,
    ModelProfileDefinition,
    PriceTable,
    Provenance,
    ProviderKind,
    SourceDocument,
    utcnow,
)
from .ports import CreditFileStore, ModelLedgerStore
from .reviews import OfflineReviewComposer, ReviewInputs

#: Purpose names the wired surfaces call under. The vocabulary
#: is open (a purpose is a label on the ledger entry), but these
#: are the model surfaces the blueprint ships.
PURPOSE_MEMO_DRAFT = "memo_draft"
PURPOSE_REVIEW_SUMMARY = "review_summary"
PURPOSE_EXTRACTION_ASSIST = "extraction_assist"

#: Actor name on the audit record a budget refusal writes.
GATEWAY_ACTOR = "model-gateway"


# ---------------------------------------------------------------------------
# Canonical configuration data. ``data/model_profiles.json`` and
# ``data/model_prices.json`` carry exactly this content for the
# file-based wiring; a test pins the two together so they cannot
# drift. Prices are governed reference data (see PriceTable).
# ---------------------------------------------------------------------------

DEFAULT_PROFILES_DATA: dict = {
    "version": "2026.10.1",
    "file_budget_usd": "5.00",
    "profiles": [
        {
            "profile_id": "offline",
            "provider_kind": "offline",
            "model": "offline-deterministic-composer-v1",
        },
        {
            "profile_id": "hosted-frontier",
            "provider_kind": "anthropic",
            "model": "claude-sonnet-4-5",
            "endpoint_env": "ANTHROPIC_BASE_URL",
            "endpoint_default": "https://api.anthropic.com",
            "api_key_env": "ANTHROPIC_API_KEY",
            "key_required": True,
        },
        {
            "profile_id": "in-boundary",
            "provider_kind": "openai-compatible",
            "model": "llama-3.3-70b-instruct",
            "endpoint_env": "IN_BOUNDARY_MODEL_URL",
            "endpoint_default": None,
            "api_key_env": "IN_BOUNDARY_MODEL_API_KEY",
            "key_required": False,
        },
    ],
}

DEFAULT_PRICES_DATA: dict = {
    "version": "2026.10.1",
    "as_of": "2026-10-01",
    "entries": [
        {
            "model": "offline-deterministic-composer-v1",
            "input_per_mtok_usd": "0",
            "output_per_mtok_usd": "0",
            "basis": "deterministic composer — no model call, no "
            "cost; priced so every call can ledger",
        },
        {
            "model": "claude-sonnet-4-5",
            "input_per_mtok_usd": "3.00",
            "output_per_mtok_usd": "15.00",
            "basis": "provider list price at the table's as-of "
            "date — governed reference data, maintained by the "
            "deployment",
        },
        {
            "model": "llama-3.3-70b-instruct",
            "input_per_mtok_usd": "0.90",
            "output_per_mtok_usd": "0.90",
            "basis": "internal serving-cost allocation for the "
            "bank's own inference boundary, set by the deployment",
        },
    ],
}


# ---------------------------------------------------------------------------
# Configuration loading — fail-closed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayConfig:
    """Validated gateway configuration: profiles + price table +
    the per-file budget cap, ready to wire."""

    profiles_version: str
    file_budget_usd: Decimal | None
    profiles: dict[str, ModelProfileDefinition]
    price_table: PriceTable
    price_by_model: dict[str, ModelPrice]


def load_gateway_config(
    profiles_data: dict, prices_data: dict
) -> GatewayConfig:
    """Validate raw profile/price data into a GatewayConfig.

    Fail-closed: any defect refuses the whole configuration with
    :class:`ModelProfileConfigError`. A deployment with a broken
    profile table does not start, and no defect is ever repaired
    by substituting a different profile or a guessed price.
    """
    try:
        price_table = PriceTable.model_validate(prices_data)
    except ValidationError as exc:
        raise ModelProfileConfigError(
            f"price table is malformed: {exc}"
        ) from exc
    price_by_model: dict[str, ModelPrice] = {}
    for entry in price_table.entries:
        if entry.model in price_by_model:
            raise ModelProfileConfigError(
                f"price table {price_table.version} prices model "
                f"{entry.model!r} twice; a model has exactly one "
                "price per table version"
            )
        if entry.input_per_mtok_usd < 0 or entry.output_per_mtok_usd < 0:
            raise ModelProfileConfigError(
                f"price table {price_table.version} carries a "
                f"negative price for model {entry.model!r}; prices "
                "are reference data and a negative price is a "
                "malformed entry, not a discount"
            )
        price_by_model[entry.model] = entry

    version = profiles_data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ModelProfileConfigError(
            "model profiles data carries no version; profiles are "
            "governed configuration and an unversioned set is "
            "refused"
        )
    raw_budget = profiles_data.get("file_budget_usd")
    file_budget: Decimal | None = None
    if raw_budget is not None:
        try:
            file_budget = Decimal(str(raw_budget))
        except Exception as exc:
            raise ModelProfileConfigError(
                f"file_budget_usd {raw_budget!r} is not a decimal "
                "amount; the per-file budget cap is configuration "
                "and a malformed cap is refused, not ignored"
            ) from exc
        if file_budget < 0:
            raise ModelProfileConfigError(
                "file_budget_usd is negative; a budget cap is a "
                "non-negative amount"
            )

    raw_profiles = profiles_data.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ModelProfileConfigError(
            "model profiles data names no profiles; a gateway with "
            "no profiles is a misconfiguration, not a default"
        )
    profiles: dict[str, ModelProfileDefinition] = {}
    for raw in raw_profiles:
        try:
            definition = ModelProfileDefinition.model_validate(raw)
        except ValidationError as exc:
            raise ModelProfileConfigError(
                f"model profile definition is malformed: {exc}"
            ) from exc
        if definition.profile_id in profiles:
            raise ModelProfileConfigError(
                f"model profile {definition.profile_id!r} is "
                "defined twice; profile ids are unique"
            )
        if definition.provider_kind is not ProviderKind.OFFLINE:
            if not definition.endpoint_env and not definition.endpoint_default:
                raise ModelProfileConfigError(
                    f"live model profile {definition.profile_id!r} "
                    "names no endpoint (neither endpoint_env nor "
                    "endpoint_default); a live profile without an "
                    "endpoint is refused at load, not at call time"
                )
            if definition.key_required and not definition.api_key_env:
                raise ModelProfileConfigError(
                    f"model profile {definition.profile_id!r} "
                    "requires an API key but names no api_key_env; "
                    "the key's variable must be named in the "
                    "definition"
                )
        if definition.model not in price_by_model:
            raise ModelProfileConfigError(
                f"model profile {definition.profile_id!r} names "
                f"model {definition.model!r}, which has no entry "
                f"in price table {price_table.version}; an "
                "unpriceable call is a configuration error, "
                "refused here — never discovered on an invoice"
            )
        profiles[definition.profile_id] = definition
    return GatewayConfig(
        profiles_version=version,
        file_budget_usd=file_budget,
        profiles=profiles,
        price_table=price_table,
        price_by_model=price_by_model,
    )


def default_gateway_config() -> GatewayConfig:
    """The gateway configuration from the canonical in-module
    data — the same content the shipped JSON files carry."""
    return load_gateway_config(DEFAULT_PROFILES_DATA, DEFAULT_PRICES_DATA)


def load_gateway_files(
    profiles_path: Path, prices_path: Path
) -> GatewayConfig:
    """Load + validate the gateway configuration from JSON files.
    A missing or unreadable file is a configuration error, not a
    reason to fall back to built-in defaults."""
    try:
        profiles_data = json.loads(Path(profiles_path).read_text("utf-8"))
        prices_data = json.loads(Path(prices_path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelProfileConfigError(
            f"gateway configuration files could not be read "
            f"({profiles_path}, {prices_path}): {exc}"
        ) from exc
    return load_gateway_config(profiles_data, prices_data)


# ---------------------------------------------------------------------------
# Transports and adapters.
# ---------------------------------------------------------------------------


@runtime_checkable
class ModelTransport(Protocol):
    """One JSON POST to a model endpoint. The real implementation
    is :class:`HttpxTransport`; tests inject stubs returning
    provider-shaped responses — the adapters are exercised against
    the same response shapes either way."""

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict,
        timeout: float,
    ) -> dict: ...


class HttpxTransport:
    """The production transport: plain HTTPS JSON via httpx (the
    repo's one HTTP client)."""

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict,
        timeout: float,
    ) -> dict:
        import httpx

        try:
            response = httpx.post(
                url, headers=headers, json=payload, timeout=timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise ModelGatewayError(
                f"model endpoint call failed ({url}): {exc}"
            ) from exc


@dataclass
class ModelCompletion:
    """One adapter call's raw product: the returned text (not yet
    shape-checked or verified — the gateway's callers do that)
    and the call's token accounting, with the basis the counts
    stand on."""

    text: str
    tokens_in: int
    tokens_out: int
    token_basis: str


def _estimate_tokens(text: str) -> int:
    """The adapters' stated estimate when a provider reports no
    usage: one token per four characters, rounded up, minimum 1.
    Named on the ledger entry as ``estimated:chars/4`` — an
    estimate is labelled an estimate wherever it is recorded."""
    return max(1, -(-len(text) // 4))


_CANDIDATE_OPEN = "<<<CANDIDATE>>>"
_CANDIDATE_CLOSE = "<<<END>>>"


def _candidate_of(prompt: str) -> str | None:
    """The deterministic candidate embedded in a drafting prompt,
    or None when the prompt carries none (an extraction prompt —
    there is no candidate text to echo)."""
    if _CANDIDATE_OPEN not in prompt:
        return None
    return prompt.split(_CANDIDATE_OPEN, 1)[1].split(_CANDIDATE_CLOSE, 1)[0].strip()


class _OfflineAdapter:
    """The ``offline`` profile's adapter: no model call at all.
    It returns the prompt's embedded candidate verbatim — the
    deterministic composer's own paragraphs — at zero tokens, so
    an offline call ledgers at $0 and produces exactly the draft
    the bare offline composer produces. A prompt with no
    candidate (extraction) cannot be served offline: the offline
    profile is a composer, not a document reader, and asking it
    to generate is refused, not improvised."""

    def complete(self, prompt: str, *, max_tokens: int) -> ModelCompletion:
        candidate = _candidate_of(prompt)
        if candidate is None:
            raise ModelGatewayError(
                "the offline profile cannot generate text: it "
                "returns the deterministic composer's candidate, "
                "and this call carries none (extraction assist "
                "requires a live profile)"
            )
        return ModelCompletion(
            text=candidate,
            tokens_in=0,
            tokens_out=0,
            token_basis="reported",
        )


class _AnthropicAdapter:
    """A hosted frontier model over the Anthropic Messages API."""

    def __init__(
        self,
        definition: ModelProfileDefinition,
        endpoint: str,
        api_key: str | None,
        transport: ModelTransport,
    ) -> None:
        self._definition = definition
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._transport = transport

    def complete(self, prompt: str, *, max_tokens: int) -> ModelCompletion:
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        body = self._transport.post_json(
            f"{self._endpoint}/v1/messages",
            headers=headers,
            payload={
                "model": self._definition.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self._definition.timeout_seconds,
        )
        text = "".join(
            str(block.get("text", ""))
            for block in body.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        usage = body.get("usage") or {}
        if "input_tokens" in usage and "output_tokens" in usage:
            return ModelCompletion(
                text=text,
                tokens_in=int(usage["input_tokens"]),
                tokens_out=int(usage["output_tokens"]),
                token_basis="reported",
            )
        return ModelCompletion(
            text=text,
            tokens_in=_estimate_tokens(prompt),
            tokens_out=_estimate_tokens(text),
            token_basis="estimated:chars/4",
        )


class _OpenAICompatibleAdapter:
    """An open-weight model served inside the boundary over an
    OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        definition: ModelProfileDefinition,
        endpoint: str,
        api_key: str | None,
        transport: ModelTransport,
    ) -> None:
        self._definition = definition
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._transport = transport

    def complete(self, prompt: str, *, max_tokens: int) -> ModelCompletion:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        body = self._transport.post_json(
            f"{self._endpoint}/chat/completions",
            headers=headers,
            payload={
                "model": self._definition.model,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self._definition.timeout_seconds,
        )
        choices = body.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            text = str(
                (choices[0].get("message") or {}).get("content", "")
            ).strip()
        usage = body.get("usage") or {}
        if "prompt_tokens" in usage and "completion_tokens" in usage:
            return ModelCompletion(
                text=text,
                tokens_in=int(usage["prompt_tokens"]),
                tokens_out=int(usage["completion_tokens"]),
                token_basis="reported",
            )
        return ModelCompletion(
            text=text,
            tokens_in=_estimate_tokens(prompt),
            tokens_out=_estimate_tokens(text),
            token_basis="estimated:chars/4",
        )


# ---------------------------------------------------------------------------
# Drafting prompts: the candidate, and the instruction that binds
# every live profile.
# ---------------------------------------------------------------------------


def _polish_prompt(paragraphs: list[str], *, artefact: str) -> str:
    """The live adapters' drafting prompt: the deterministic
    composer's candidate paragraphs (as a JSON array) and the
    instruction that binds the model to them. The figures in the
    candidate were computed in code from provenanced inputs; the
    model's task is wording only, and the artefact's verifier —
    not this prompt — is what enforces that afterwards."""
    candidate = json.dumps(paragraphs, indent=2)
    return (
        f"You are polishing the narrative paragraphs of a bank "
        f"{artefact}. The paragraphs below were assembled in code "
        "from the credit file's pinned artefacts; every figure in "
        "them was read from a borrower document with provenance "
        "or computed deterministically.\n\n"
        "Candidate paragraphs (a JSON array):\n"
        f"{_CANDIDATE_OPEN}\n{candidate}\n{_CANDIDATE_CLOSE}\n\n"
        "Return ONLY a JSON array of the same paragraphs, in the "
        "same order and the same count, with the wording polished. "
        "You must not introduce, alter, or drop any figure, date, "
        "name, or identifier: every number in your paragraphs "
        "must appear in the candidate above. A draft carrying an "
        "untraceable figure is refused by a verifier in code."
    )


def _parse_paragraphs(text: str, *, expected: int, profile_id: str) -> list[str]:
    """Parse a live reply back into the candidate's paragraph
    shape. Anything else — no JSON array, a non-string member, a
    moved count — refuses the whole reply: a partially mapped
    draft would silently drop or invent narrative."""
    start = text.find("[")
    end = text.rfind("]")
    parsed: object = None
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            parsed = None
    if (
        not isinstance(parsed, list)
        or any(not isinstance(p, str) for p in parsed)
        or len(parsed) != expected
    ):
        raise ModelResponseError(
            f"model profile {profile_id!r} returned narrative "
            "that does not parse back into the draft's shape "
            f"(expected a JSON array of {expected} paragraph(s)); "
            "the reply is refused whole — the call is recorded "
            "in the cost ledger, because a refused draft still "
            "cost what it cost"
        )
    return parsed


# ---------------------------------------------------------------------------
# The gateway.
# ---------------------------------------------------------------------------


@dataclass
class _ResolvedProfile:
    """A profile definition plus what the deployment's environment
    resolved for it at wiring time. The API key value lives only
    here, in memory — never on a record, a ledger entry, or a log
    line; summaries expose only ``configured``."""

    definition: ModelProfileDefinition
    endpoint: str | None
    api_key: str | None

    @property
    def is_live(self) -> bool:
        return self.definition.provider_kind is not ProviderKind.OFFLINE

    @property
    def configured(self) -> bool:
        if not self.is_live:
            return True
        if not self.endpoint:
            return False
        if self.definition.key_required and not self.api_key:
            return False
        return True


class ModelGateway:
    """The configured path for every model call: profile
    resolution (fail-closed), budget enforcement, adapter
    dispatch, pricing, and the cost ledger.

    ``ledger_store`` is required — a gateway without its ledger
    would make unrecorded calls, which is the one thing this
    module exists to prevent. ``clock`` is injected so timestamps
    on ledger entries are testable; production wiring takes the
    default wall clock, and no verdict anywhere derives from a
    ledger timestamp.
    """

    def __init__(
        self,
        config: GatewayConfig,
        ledger_store: ModelLedgerStore,
        *,
        environ: dict[str, str] | None = None,
        transport: ModelTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        audit_store: CreditFileStore | None = None,
        active_profile: str | None = None,
    ) -> None:
        self._config = config
        self._ledger = ledger_store
        self._transport = transport or HttpxTransport()
        self._clock = clock or utcnow
        self._audit = audit_store
        env = os.environ if environ is None else environ
        self._resolved: dict[str, _ResolvedProfile] = {}
        for profile_id, definition in config.profiles.items():
            endpoint = None
            if definition.endpoint_env:
                endpoint = env.get(definition.endpoint_env) or None
            endpoint = endpoint or definition.endpoint_default
            api_key = None
            if definition.api_key_env:
                api_key = env.get(definition.api_key_env) or None
            self._resolved[profile_id] = _ResolvedProfile(
                definition=definition, endpoint=endpoint, api_key=api_key
            )
        if active_profile is not None:
            # Wiring-time fail-closed: the profile a deployment
            # names as its own must exist and be fully configured
            # before the stack starts — not on its first call.
            resolved = self._require_profile(active_profile)
            self._require_configured(resolved)

    # -- configuration surface -------------------------------------

    @property
    def config(self) -> GatewayConfig:
        return self._config

    @property
    def price_table(self) -> PriceTable:
        return self._config.price_table

    @property
    def file_budget_usd(self) -> Decimal | None:
        return self._config.file_budget_usd

    def profile_summaries(self) -> list[dict]:
        """Every profile, its model, its price, and whether this
        deployment has configured it. Key *values* never appear —
        only the variable names and the configured flag."""
        summaries = []
        for profile_id in sorted(self._resolved):
            resolved = self._resolved[profile_id]
            definition = resolved.definition
            price = self._config.price_by_model[definition.model]
            summaries.append(
                {
                    "profile_id": profile_id,
                    "provider_kind": definition.provider_kind.value,
                    "model": definition.model,
                    "configured": resolved.configured,
                    "endpoint_env": definition.endpoint_env,
                    "api_key_env": definition.api_key_env,
                    "key_required": definition.key_required,
                    "input_per_mtok_usd": format(
                        price.input_per_mtok_usd, "f"
                    ),
                    "output_per_mtok_usd": format(
                        price.output_per_mtok_usd, "f"
                    ),
                    "price_table_version": self._config.price_table.version,
                }
            )
        return summaries

    # -- resolution (fail-closed) -----------------------------------

    def _require_profile(self, profile_id: str) -> _ResolvedProfile:
        resolved = self._resolved.get(profile_id)
        if resolved is None:
            raise UnknownModelProfileError(
                f"unknown model profile {profile_id!r}; the gateway "
                f"holds: {', '.join(sorted(self._resolved))}. No "
                "profile is substituted for the one that was named"
            )
        return resolved

    def _require_configured(self, resolved: _ResolvedProfile) -> None:
        definition = resolved.definition
        if resolved.configured:
            return
        missing = []
        if not resolved.endpoint:
            missing.append(
                f"endpoint (set {definition.endpoint_env})"
                if definition.endpoint_env
                else "endpoint"
            )
        if definition.key_required and not resolved.api_key:
            missing.append(f"API key (set {definition.api_key_env})")
        raise ModelProfileNotConfiguredError(
            f"model profile {definition.profile_id!r} is not "
            f"configured in this deployment — missing: "
            f"{', '.join(missing)}. The call is refused; no other "
            "profile is substituted"
        )

    def check_profile(self, profile_id: str) -> ModelProfileDefinition:
        """Resolve a profile for a factory or a wiring decision:
        it must exist and be configured, here, before any work
        is bound to it."""
        resolved = self._require_profile(profile_id)
        self._require_configured(resolved)
        return resolved.definition

    def _adapter_for(self, resolved: _ResolvedProfile):
        definition = resolved.definition
        if definition.provider_kind is ProviderKind.OFFLINE:
            return _OfflineAdapter()
        if definition.provider_kind is ProviderKind.ANTHROPIC:
            return _AnthropicAdapter(
                definition,
                resolved.endpoint or "",
                resolved.api_key,
                self._transport,
            )
        return _OpenAICompatibleAdapter(
            definition,
            resolved.endpoint or "",
            resolved.api_key,
            self._transport,
        )

    # -- the one call path -------------------------------------------

    def _price(
        self, model: str, tokens_in: int, tokens_out: int
    ) -> tuple[ModelPrice, Decimal]:
        price = self._config.price_by_model[model]
        cost = (
            Decimal(tokens_in) * price.input_per_mtok_usd
            + Decimal(tokens_out) * price.output_per_mtok_usd
        ) / Decimal(1_000_000)
        return price, cost

    def generate(
        self,
        profile_id: str,
        *,
        credit_file_id: str | None,
        purpose: str,
        prompt: str,
        max_tokens: int = 4096,
    ) -> str:
        """One model call, whole: resolve the profile
        (fail-closed), enforce the file's budget *before*
        transport, dispatch to the profile's adapter, price the
        call from the governed table, append the ledger entry,
        and return the adapter's text. Shape checks and the
        artefact verifiers run on the returned text afterwards,
        in the callers — a refusal there never un-ledgers the
        call, because the entry is already written here."""
        resolved = self._require_profile(profile_id)
        self._require_configured(resolved)
        definition = resolved.definition
        if resolved.is_live and credit_file_id is not None:
            self._enforce_budget(resolved, credit_file_id, purpose)
        adapter = self._adapter_for(resolved)
        started = time.perf_counter()
        completion = adapter.complete(prompt, max_tokens=max_tokens)
        latency_ms = int(round((time.perf_counter() - started) * 1000))
        price, cost = self._price(
            definition.model, completion.tokens_in, completion.tokens_out
        )
        self._ledger.add_call_entry(
            ModelCallEntry(
                credit_file_id=credit_file_id,
                purpose=purpose,
                profile_id=definition.profile_id,
                model=definition.model,
                tokens_in=completion.tokens_in,
                tokens_out=completion.tokens_out,
                token_basis=completion.token_basis,
                price_table_version=self._config.price_table.version,
                input_per_mtok_usd=price.input_per_mtok_usd,
                output_per_mtok_usd=price.output_per_mtok_usd,
                cost_usd=cost,
                latency_ms=latency_ms,
                called_at=self._clock(),
            )
        )
        return completion.text

    def list_call_entries(
        self, credit_file_id: str | None
    ) -> list[ModelCallEntry]:
        """Ledger reads: one file's priced calls, or every call
        in the ledger at ``None`` (the portfolio scope)."""
        return self._ledger.list_call_entries(credit_file_id)

    def list_budget_events(
        self, credit_file_id: str
    ) -> list[ModelBudgetEvent]:
        """One file's recorded budget refusals."""
        return self._ledger.list_budget_events(credit_file_id)

    # -- budget -------------------------------------------------------

    def file_spend(self, credit_file_id: str) -> Decimal:
        """The file's total priced spend across all profiles and
        purposes, from the ledger — the figure the cap tests."""
        total = Decimal("0")
        for entry in self._ledger.list_call_entries(credit_file_id):
            total += entry.cost_usd
        return total

    def _enforce_budget(
        self,
        resolved: _ResolvedProfile,
        credit_file_id: str,
        purpose: str,
    ) -> None:
        cap = self._config.file_budget_usd
        if cap is None:
            return
        spent = self.file_spend(credit_file_id)
        if spent < cap:
            return
        definition = resolved.definition
        event = ModelBudgetEvent(
            credit_file_id=credit_file_id,
            profile_id=definition.profile_id,
            model=definition.model,
            purpose=purpose,
            cap_usd=cap,
            spent_usd=spent,
            recorded_at=self._clock(),
        )
        self._ledger.add_budget_event(event)
        if self._audit is not None:
            self._audit.append_audit(
                AuditEvent(
                    credit_file_id=credit_file_id,
                    actor=GATEWAY_ACTOR,
                    action="model_budget_refused",
                    detail=(
                        f"live model call refused before transport: "
                        f"profile {definition.profile_id} "
                        f"({definition.model}), purpose {purpose}; "
                        f"priced spend ${format(spent, 'f')} has "
                        f"reached the per-file cap "
                        f"${format(cap, 'f')} (budget event "
                        f"{event.event_id})"
                    ),
                )
            )
        raise ModelBudgetExceededError(
            credit_file_id,
            definition.profile_id,
            format(cap, "f"),
            format(spent, "f"),
        )

    # -- rollups ------------------------------------------------------

    def file_rollup(self, credit_file_id: str) -> dict:
        """The file's model cost, broken down by purpose and by
        profile, with the cap position. All money exact Decimal,
        rendered as strings at this boundary."""
        entries = self._ledger.list_call_entries(credit_file_id)
        by_purpose: dict[str, dict] = {}
        by_profile: dict[str, dict] = {}
        total_cost = Decimal("0")
        total_in = 0
        total_out = 0
        for entry in entries:
            total_cost += entry.cost_usd
            total_in += entry.tokens_in
            total_out += entry.tokens_out
            for bucket, key in (
                (by_purpose, entry.purpose),
                (by_profile, entry.profile_id),
            ):
                row = bucket.setdefault(
                    key,
                    {
                        "calls": 0,
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "cost_usd": Decimal("0"),
                    },
                )
                row["calls"] += 1
                row["tokens_in"] += entry.tokens_in
                row["tokens_out"] += entry.tokens_out
                row["cost_usd"] += entry.cost_usd

        def render(bucket: dict[str, dict]) -> dict:
            return {
                key: {
                    "calls": row["calls"],
                    "tokens_in": row["tokens_in"],
                    "tokens_out": row["tokens_out"],
                    "cost_usd": format(row["cost_usd"], "f"),
                }
                for key, row in sorted(bucket.items())
            }

        cap = self._config.file_budget_usd
        return {
            "credit_file_id": credit_file_id,
            "calls": len(entries),
            "tokens_in": total_in,
            "tokens_out": total_out,
            "cost_usd": format(total_cost, "f"),
            "by_purpose": render(by_purpose),
            "by_profile": render(by_profile),
            "budget_cap_usd": (
                format(cap, "f") if cap is not None else None
            ),
            "budget_spent_usd": format(total_cost, "f"),
            "budget_refusals": len(
                self._ledger.list_budget_events(credit_file_id)
            ),
            "price_table_version": self._config.price_table.version,
        }

    def portfolio_rollup(self) -> list[dict]:
        """Every ledger entry grouped by UTC day + profile: the
        portfolio's model spend, day by day. No verdict derives
        from these timestamps — this is cost reporting, grouped by
        the injected clock's recorded instants."""
        rows: dict[tuple[str, str], dict] = {}
        for entry in self._ledger.list_call_entries(None):
            day = entry.called_at.date().isoformat()
            row = rows.setdefault(
                (day, entry.profile_id),
                {
                    "day": day,
                    "profile_id": entry.profile_id,
                    "model": entry.model,
                    "calls": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "cost_usd": Decimal("0"),
                },
            )
            row["calls"] += 1
            row["tokens_in"] += entry.tokens_in
            row["tokens_out"] += entry.tokens_out
            row["cost_usd"] += entry.cost_usd
        return [
            {
                **row,
                "cost_usd": format(row["cost_usd"], "f"),
            }
            for _, row in sorted(rows.items())
        ]

    # -- composer factories -------------------------------------------

    def memo_composer(
        self, profile_id: str, *, credit_file_id: str | None
    ) -> "_GatewayMemoComposer":
        """A MemoComposer bound to one profile + file, satisfying
        the round-5 composer port. Resolution is fail-closed
        here, before any drafting begins."""
        self.check_profile(profile_id)
        return _GatewayMemoComposer(self, profile_id, credit_file_id)

    def review_composer(
        self, profile_id: str, *, credit_file_id: str | None
    ) -> "_GatewayReviewComposer":
        """A ReviewComposer bound to one profile + file,
        satisfying the round-12 composer port."""
        self.check_profile(profile_id)
        return _GatewayReviewComposer(self, profile_id, credit_file_id)


class _GatewayMemoComposer:
    """The memo composer a pipeline drafts through when the
    gateway is wired: the deterministic composer renders the
    candidate, the gateway's profile polishes its narrative
    paragraphs (offline: returns them unchanged), and the
    pipeline's verifier judges the result exactly as it judges
    the bare offline draft. Tables and every structured part of
    the candidate pass through untouched — the model never sees
    them as editable text."""

    def __init__(
        self,
        gateway: ModelGateway,
        profile_id: str,
        credit_file_id: str | None,
    ) -> None:
        self._gateway = gateway
        self._credit_file_id = credit_file_id
        self.name = profile_id

    def compose(self, inputs: MemoInputs) -> list[MemoSection]:
        candidate = OfflineMemoComposer().compose(inputs)
        slots: list[tuple[int, int]] = []
        paragraphs: list[str] = []
        for s_idx, section in enumerate(candidate):
            for p_idx, paragraph in enumerate(section.paragraphs):
                slots.append((s_idx, p_idx))
                paragraphs.append(paragraph)
        if not paragraphs:
            return candidate
        text = self._gateway.generate(
            self.name,
            credit_file_id=self._credit_file_id,
            purpose=PURPOSE_MEMO_DRAFT,
            prompt=_polish_prompt(paragraphs, artefact="credit memo"),
        )
        polished = _parse_paragraphs(
            text, expected=len(paragraphs), profile_id=self.name
        )
        by_section: dict[int, dict[int, str]] = {}
        for (s_idx, p_idx), paragraph in zip(slots, polished):
            by_section.setdefault(s_idx, {})[p_idx] = paragraph
        return [
            section.model_copy(
                update={
                    "paragraphs": [
                        by_section[s_idx][p_idx]
                        for p_idx in range(len(section.paragraphs))
                    ]
                }
            )
            if s_idx in by_section
            else section
            for s_idx, section in enumerate(candidate)
        ]


class _GatewayReviewComposer:
    """The review-summary composer, same discipline as the memo's:
    the deterministic composer's paragraphs are the candidate;
    the profile polishes wording only; the round-12 summary
    verifier judges the result."""

    def __init__(
        self,
        gateway: ModelGateway,
        profile_id: str,
        credit_file_id: str | None,
    ) -> None:
        self._gateway = gateway
        self._credit_file_id = credit_file_id
        self.name = profile_id

    def compose(self, inputs: ReviewInputs) -> list[str]:
        candidate = OfflineReviewComposer().compose(inputs)
        if not candidate:
            return candidate
        text = self._gateway.generate(
            self.name,
            credit_file_id=self._credit_file_id,
            purpose=PURPOSE_REVIEW_SUMMARY,
            prompt=_polish_prompt(
                candidate, artefact="annual credit review summary"
            ),
        )
        return _parse_paragraphs(
            text, expected=len(candidate), profile_id=self.name
        )


# ---------------------------------------------------------------------------
# The model-assisted extractor: a live profile behind the
# extraction port.
# ---------------------------------------------------------------------------


def _extraction_prompt(document: SourceDocument, text: str) -> str:
    return (
        "You are reading one borrower document for a bank credit "
        "file. The document's text follows. Extract every labelled "
        "figure line you can point at exactly.\n\n"
        "Return ONLY a JSON array. Each element is an object with:\n"
        '  "label": the line\'s label, exactly as printed;\n'
        '  "value": the figure, as a number;\n'
        '  "page": the page number the figure appears on;\n'
        '  "line": the line number or cell coordinate of the '
        "figure on that page.\n\n"
        "A figure you cannot point at with a page and a line does "
        "not belong in the array: the receiving system rejects any "
        "value without complete provenance, and one bad value "
        "rejects the batch. Do not compute, round, or infer any "
        "figure — read only what is printed.\n\n"
        f"Document {document.document_id} "
        f"({document.kind.value}), text:\n"
        f"<<<DOCUMENT>>>\n{text}\n{_CANDIDATE_CLOSE}"
    )


class ModelAssistedExtractor:
    """A document-AI profile behind the round-1 Extractor port.

    The live profile reads the document text and returns the
    figures it can point at, as JSON; this class maps labels to
    the spreading taxonomy's field names exactly as the reference
    extractor does, stamps the source type the same way, and
    builds each value through the same :class:`ExtractedValue`
    construction — so provenance is enforced by the same
    validators, and the pipeline's ``receive_extraction`` checks
    the batch again on receipt. A value the model returns
    without a page or a line raises :class:`ProvenanceError`:
    the gateway changes nothing about law 1. A reply that is not
    the contracted JSON raises :class:`ModelResponseError`. The
    call is ledgered (purpose ``extraction_assist``) before
    either check runs.

    The profile must be live and configured — checked at
    construction: the offline profile is a composer, not a
    document reader, and an unconfigured endpoint is a wiring
    error, not a first-call surprise. ``for_file`` binds the
    credit file the extraction serves, so its calls ledger (and
    budget) against that file; unbound, they ledger at system
    scope.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        profile_id: str,
        *,
        credit_file_id: str | None = None,
    ) -> None:
        definition = gateway.check_profile(profile_id)
        if definition.provider_kind is ProviderKind.OFFLINE:
            raise ModelGatewayError(
                "the offline profile cannot read documents; "
                "extraction assist requires a live profile "
                "(hosted-frontier or in-boundary)"
            )
        self._gateway = gateway
        self._profile_id = profile_id
        self._credit_file_id = credit_file_id

    def for_file(self, credit_file_id: str) -> "ModelAssistedExtractor":
        """A copy bound to one credit file, so the extraction
        calls it makes ledger against that file."""
        return ModelAssistedExtractor(
            self._gateway, self._profile_id, credit_file_id=credit_file_id
        )

    def extract(
        self, document: SourceDocument, content: bytes
    ) -> list[ExtractedValue]:
        text = content.decode("utf-8", errors="replace")
        reply = self._gateway.generate(
            self._profile_id,
            credit_file_id=self._credit_file_id,
            purpose=PURPOSE_EXTRACTION_ASSIST,
            prompt=_extraction_prompt(document, text),
            max_tokens=8192,
        )
        return self._values(document, content, reply)

    def _values(
        self, document: SourceDocument, content: bytes, reply: str
    ) -> list[ExtractedValue]:
        start = reply.find("[")
        end = reply.rfind("]")
        parsed: object = None
        if start != -1 and end > start:
            try:
                parsed = json.loads(reply[start : end + 1])
            except json.JSONDecodeError:
                parsed = None
        if not isinstance(parsed, list):
            raise ModelResponseError(
                f"model profile {self._profile_id!r} returned an "
                "extraction payload that is not the contracted "
                "JSON array; the batch is refused whole — the "
                "call is recorded in the cost ledger"
            )
        # The source stamp + period facts are read from the
        # document itself, exactly as the reference extractor
        # reads them — the model supplies figures and
        # coordinates, never the frame they sit in.
        payload: dict = {}
        try:
            decoded = json.loads(content.decode("utf-8"))
            if isinstance(decoded, dict):
                payload = decoded
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if (
            document.kind == DocumentKind.TAX_RETURN
            or payload.get("document_type") == "tax_return"
        ):
            source_type = SOURCE_TAX
        else:
            source_type = source_type_for_document(
                document.kind.value, payload.get("basis")
            )
        period_label = (
            payload.get("period_label") or document.period_label or ""
        )
        fiscal_year = payload.get("fiscal_year")
        values: list[ExtractedValue] = []
        for item in parsed:
            if not isinstance(item, dict):
                raise ModelResponseError(
                    "extraction payload carries a non-object "
                    "entry; the batch is refused whole"
                )
            label = item.get("label")
            if not isinstance(label, str) or not label.strip():
                raise ModelResponseError(
                    "extraction payload carries a value with no "
                    "label; the batch is refused whole"
                )
            page = item.get("page")
            line = item.get("line")
            if (
                not isinstance(page, int)
                or isinstance(page, bool)
                or page < 1
                or line is None
                or (isinstance(line, str) and not line.strip())
            ):
                raise ProvenanceError(
                    f"model-extracted value {label!r} arrived "
                    "without complete provenance (page and line "
                    "are mandatory); the batch is refused — a "
                    "figure that cannot be pointed at does not "
                    "enter the credit file"
                )
            try:
                amount = Decimal(str(item.get("value")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ModelResponseError(
                    f"extraction payload value for {label!r} is "
                    "not a number; the batch is refused whole"
                ) from exc
            line_ref = f"L{line}" if isinstance(line, int) else str(line)
            values.append(
                ExtractedValue(
                    field=STATEMENT_FIELDS.get(label)
                    or unmapped_field_for(label),
                    value=amount,
                    period_label=period_label,
                    fiscal_year=fiscal_year,
                    provenance=Provenance(
                        document_id=document.document_id,
                        page=page,
                        line=line_ref,
                    ),
                    source_label=label,
                    source_type=source_type,
                )
            )
        return values
