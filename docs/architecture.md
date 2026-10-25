# Architecture — Commercial Credit Memo & Covenant Monitoring Agent

## Shape

```
LOS ──► intake ──► checklist ──► document intake ──► extraction ──► spreading ──► [later rounds:
        (LangGraph pipeline)        (S3 port,          (port: values    (template,     computation →
                                     SHA-256 recorded)   + provenance)   adjustments)   policy → memo →
                                                                          officer gate →
                                                                          committee gate →
                                                                          covenant loop]
```

The pipeline is a LangGraph graph (`pipeline.py`). Rounds 1–2 wire
the first five nodes end to end; later rounds extend the graph to the
right, with the two human gates as graph interrupts. The same
compiled graph serves the API, the demo and the evals — there is one
spine, not three near-copies.

## Ports and their implementations

| Port | Contract | Round-1 implementations |
| --- | --- | --- |
| `LosFeed` | application ref → borrower + facility request | `ReferenceLosFeed` (sample application) |
| `DocumentStore` | put/get/exists over S3-compatible storage | `S3DocumentStore` (boto3), `LocalDocumentStore` (double) |
| `Extractor` | document → values, **every value with provenance** | `OfflineReferenceExtractor` (generated structured documents) |
| `CreditFileStore` | credit files, extractions, audit | `PostgresCreditStore`, `SqliteCreditStore`, `InMemoryCreditStore` (doubles) |

Swapping a port implementation is a wiring change (`wiring.py`),
never a pipeline change.

## The provenance contract

`ExtractedValue` requires a `Provenance` (document id, page ≥ 1, and
a line or a cell). Enforcement is layered, deliberately:

1. The Pydantic models reject a missing provenance object and a
   provenance with neither line nor cell.
2. `require_complete_provenance` re-checks a whole batch; extraction
   adapters call it on their own output.
3. `receive_extraction` — the pipeline's receiving side — calls it
   again before anything is stored. One bad value rejects the batch.

The point of the layering: the adapter that a deployment swaps in
(document AI over scanned PDFs) cannot weaken the contract by being
less careful than the reference extractor — its output crosses the
same checkpoint.

## Document integrity

Intake hashes every document (SHA-256) and records the digest on the
credit file. Reads go through `read_document_verified`, which
re-hashes the stored bytes and raises `DocumentIntegrityError` on a
mismatch. The digest lives in the system of record, the bytes in
object storage; neither alone can quietly rewrite history.

## Spreading (round 2)

`spreading.py` holds the bank's template as data — a line taxonomy
over the balance-sheet, income-statement and cash-flow sections, each
line keyed to the extraction field that feeds it — and a pure builder
that turns a credit file's extractions + adjustments into a `Spread`.
Determinism is a design constraint, not a hope: inputs are canonically
sorted, the spread carries no timestamps or random ids, and its id is
a hash of its canonical content, so rebuilds converge byte-identically
(the eval gate rebuilds and compares). Within a period, the latest
filed statement version supplies every value; earlier versions are
named in `period_sources.superseded_document_ids` and their
extractions remain in the store. Adjustments are validated on receipt
(`require_valid_adjustment` + taxonomy membership) before they are
stored, and the spread carries both the unadjusted values and the
derived adjusted view per line and period.

## Storage

PostgreSQL is the deployment store. `migrations/` holds numbered SQL
files; `db.run_migrations` applies each once, in order, at startup,
recording it in `schema_migrations`. Payloads are the domain models
serialized to JSONB with the filterable columns (borrower, facility
type, status) broken out. SQLite and in-memory stores implement the
same port for local runs and tests and make no deployment claims;
`/readiness` names the live profile.

## Round map

R1 core: credit file model, intake, document port, provenance-carrying
extraction · R2 spreading engine · R3 computation engine (ratios,
DSCR, global cash flow) · R4 policy pack v1 · R5 memo drafting +
figure verifier · R6 credit officer gate + diff recording · R7
committee gate + coded adverse-action reasons · R8 disagreement
handling · R9 staleness blocks · R10 covenant model + scheduler ·
R11 covenant re-spreading + breach cases · R12 annual review packs ·
R13 risk-rating scorecard support · R14 officer console · R15 model
gateway profiles + cost ledger · R16 Postgres profile + prod smoke ·
R17 injection defences + retrieval ACLs · R18 eval depth · R19
portfolio views · R20 release hardening.

## What a production deploy still needs

Real LOS/core integration behind the ports, a document-AI key for
the extraction adapter, SSO in front of the API, TLS on every hop,
secrets from a manager rather than the environment, and the bank's
own model-risk governance (SR 11-7 style) around the scorecard the
later rounds assemble inputs for. The blueprint's subject is the
deterministic control structure around the model; those controls
are what is tested here.
