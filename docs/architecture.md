# Architecture — Commercial Credit Memo & Covenant Monitoring Agent

## Shape

```
LOS ──► intake ──► checklist ──► document intake ──► extraction ──► spreading ──► computation ──► [later rounds:
        (LangGraph pipeline)        (S3 port,          (port: values    (template,     (engine:        policy → memo →
                                     SHA-256 recorded)   + provenance)   adjustments)   ratios, DSCR,   officer gate →
                                                                                       global CF)      committee gate →
                                                                                                       covenant loop]
```

The pipeline is a LangGraph graph (`pipeline.py`). Rounds 1–3 wire
the first six nodes end to end; later rounds extend the graph to the
right, with the two human gates as graph interrupts. The same
compiled graph serves the API, the demo and the evals — there is one
spine, not three near-copies.

## Ports and their implementations

| Port | Contract | Round-1 implementations |
| --- | --- | --- |
| `LosFeed` | application ref → borrower + facility request | `ReferenceLosFeed` (sample application) |
| `DocumentStore` | put/get/exists over S3-compatible storage | `S3DocumentStore` (boto3), `LocalDocumentStore` (double) |
| `Extractor` | document → values, **every value with provenance** | `OfflineReferenceExtractor` (generated structured documents) |
| `CreditFileStore` | credit files, extractions, audit, spreads, computation packs | `PostgresCreditStore`, `SqliteCreditStore`, `InMemoryCreditStore` (doubles) |

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

## Computation (round 3)

`computations.py` is a pure engine over the spread. Its definitions
are written down in the module itself — debt service is scheduled
principal (the prior period's current portion of long-term debt)
plus interest plus capital lease payments when a provenanced figure
supplies them; EBITDA is operating income plus depreciation &
amortisation on the pack's view, the R2 adjustments named in its
derivation; tangible net worth deducts intangibles exactly as the
statements report them. Two honesty rules apply uniformly: a
required input that is absent makes the figure *not computable*,
naming the input (nothing is zero-filled); an optional component
the statements do not report is recorded as "not reported" and left
out, so the artefact always shows which components entered.

Every figure carries its derivation as recorded steps. Step inputs
name spread lines with the provenance chain of the extraction that
fed the cell (plus adjustment ids and sources on the adjusted
view), supplemental figures with their provenance, earlier figures,
or — in a global pack — named eliminations. `verify_trace`
re-performs each step's arithmetic and walks every chain to its
leaves; the eval gate requires completeness of 1.000. A run's
output is a `ComputationPack`, content-addressed like the spread:
engine version, spread id, view, supplemental figures and covenant
definitions hash into the pack id, so recomputation converges on
the stored artefact (migration 0003) instead of duplicating it.

Global cash flow combines entity packs — the borrower's and one per
guarantor spread — applying eliminations as first-class records: an
elimination names its amount, its target (combined cash flow or
combined debt service) and its reason, and an unexplained one is
refused on receipt (`require_valid_elimination`). A guarantor named
for the global view with no spread is likewise refused, unless the
caller explicitly excludes them; the pack then carries an exclusion
record naming them. Covenant headroom is computed against
definitions *given* to the run (metric, operator, threshold,
period); storing covenant definitions and scheduling their tests is
round 10, and the lending-policy pack is round 4.

## Policy (round 4)

`policy.py` holds the packs as data: an id (`aldergrove-policy`), a
version, an effective window, an issuing note, the stated capital
base, and the test set. A `PolicyTest` names its kind (figure or
file_fact), the exact inputs it reads, its operator/threshold (or
segment threshold table), its severity, and the citation text of
the policy it encodes; model validation refuses a test that names
no input. Two versions ship — v1 (2025 issue) and v2 (2026
refresh), differing only in three tightened numbers — and packs are
never edited in place: a policy change is a new version.

`evaluate_policy` is a pure function of (credit file, borrower
pack, optional global pack, pinned version, as-of date). Figure
tests read the figure for the most recent fiscal year in the pack;
when that figure is not computable, the outcome is *not_evaluable*
carrying the figure's own missing-input list — the evaluator has
no code path from a missing figure to a pass, and the eval gate
re-verifies every pass against its pack (silent-pass counter,
required 0). Applicability is read from file facts: the global
DSCR test requires guarantors named on the facility and a global
pack handed in; the appraisal test applies only to facility types
the pack lists. Ages are complete calendar months — statements
from the fiscal year-end (31 December of the pack's latest year),
appraisals from the most recent appraisal document's received
date — against the evaluation's as-of date, which the report
records.

The output is a `PolicyReport`, content-addressed like the packs
it reads: the report id hashes the pack version and a digest of
the pack itself, the computation pack ids, the as-of date, and a
digest of exactly the credit-file facts the tests read, so the id
moves when — and only when — an input moves. Reports are stored
behind the credit-file port (migration 0004) and never overwritten:
a re-evaluation under a newer version appends a new report. The
pipeline's policy node evaluates under the current version at
intake; `CreditPipeline.evaluate_policy` pins a version explicitly
and appends a `PolicyHistoryEntry` (pack, version, report id,
as-of) to the credit file the first time those inputs are seen.
The memo (round 5) will quote the report and its pinned version;
the gates (rounds 6–7) will read blocker failures from it.

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
