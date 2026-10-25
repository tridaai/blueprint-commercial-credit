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
(the eval gate rebuilds and compares). Within a period and a source
type (round 8), the latest filed version of that type supplies its
values; earlier versions are named in
`period_sources.superseded_document_ids` and their extractions remain
in the store. Across source types nothing supersedes — see the
disagreements section below. Adjustments are validated on receipt
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
The memo (round 5) quotes the report and its pinned version;
the gates (rounds 6–7) will read blocker failures from it.

## Memo drafting and the figure verifier (round 5)

`memo.py` turns the pinned artefacts into the draft credit memo —
and is where design law 1 is enforced hardest, because prose is
where figures escape their provenance. A `CreditMemo` is a stored,
content-addressed artefact (migration 0005): its header pins the
borrower and facility, the spread id, the computation pack id, the
policy pack id + version and the report id, and the report's as-of
date. Its sections are fixed in shape: request summary, borrower
overview, financial analysis (per-period narrative plus spread and
computed-measures tables), strengths, risks and mitigants, policy
exceptions, and two placeholders — recommendation and conditions —
that the credit officer completes at the gate (round 6). The memo
states facts and policy outcomes; it never recommends.

Drafting goes through a composer port with named profiles. The
default `offline` profile is deterministic: it assembles the
narrative from the structured artefacts, formatting every figure
through the module's helpers (`fmt_money`, `fmt_ratio`,
`fmt_percent`), so the same inputs reproduce the identical memo,
id included. A `live` profile is defined behind the same port for
a model-backed drafter and refuses to compose until a provider key
is wired — there is no silent fallback.

The verifier is code over the final rendered text, not over the
composer's intentions. Rendering produces the memo exactly as read
(header, sections, tables); artefact ids and pack versions are
rendered in code spans and are not figures, and neither are ISO
dates, FY tags or policy section references. Everything else
numeric — $-amounts, ×-ratios, percents, bare counts — is
extracted and must equal a value in the trace universe built from
the pinned inputs: computation-pack figures, covenant results and
supplemental inputs; spread line values in both views and their
period changes; policy thresholds, the values the tests read, and
pack facts such as the capital base; and stated facts of the
credit file (facility amount and term, years in business,
guarantor ownership, checklist and document counts, fiscal
years). An untraceable figure refuses the memo — it is never
stored as a draft — and the refusal names the figures. The traces
that cleared (figure as rendered → source artefact + field, per
occurrence) are stored with the memo as its verification record
and served by the API.

Exceptions integrity is structural, not editorial: the exceptions
section carries the test ids it accounts for, and the verifier
requires them to be exactly the pinned report's non-pass outcomes.
A dropped blocker fail — or a not_evaluable rewritten as a pass —
fails verification even when every figure in the doctored text
still traces. The pipeline's memo node composes at intake from
the policy node's report; `CreditPipeline.compose_memo` pins a
report or a pack version explicitly, stores the memo
content-addressed, and audits `memo_composed` the first time
those inputs are seen.

## Credit officer gate (round 6)

`officer_gate.py` holds the gate's discipline — identity checks,
the coded return vocabulary, status derivation, edit application
and diff computation — and `CreditPipeline` runs the workflow
over the stores. The gate acts on the stored draft memo, not
inside the intake graph: states move draft → in_officer_review →
officer_approved | returned, and the transitions are enforced in
code.

No action is anonymous. Every action is a stored, append-only
`MemoReviewEvent` (migration 0006) naming the actor and their
role, mirrored onto the file's audit trail under the same actor;
approvals, returns and flags additionally require a credit
officer role. A memo's review status is DERIVED from its event
stream by `derive_status` — artefacts are never mutated, so the
stored draft stays byte-identical to what the verifier cleared
in round 5, and reads overlay the derived status.

Approval builds a candidate from the draft — the officer's
paragraph edits applied (tables are machine-rendered and not
editable; the `policy_exceptions` section is locked, as it
renders the pinned policy report verbatim) — and runs it through
the same `verify_memo` as the draft. Only a cleared candidate is
stored, as a new content-addressed artefact: status
`officer_approved`, `supersedes_memo_id` naming the draft,
`reviewed_by` naming the officer, and the memo's committee flags
carried over. Alongside it, `compute_memo_diff` records the
`MemoDiff`: per-section paragraph before/after, and figure
changes computed by walking each section's traced figure
occurrences in reading order — a same-position swap is one
`altered` change with both sides' trace sources, never a silent
remove-plus-add.

Returns carry codes from `RETURN_REASON_CODES`
(`officer-return-reasons-v1`); the version is stamped on the
event. A returned memo is terminal as a draft: `recompose_memo`
composes a fresh draft from the file's current pinned artefacts,
stamps the supersession lineage on it, and gives it its own
review life. Flags come in two kinds — `committee_attention`,
carried onto the final memo for round 7's committee, and
`conflict_of_interest`, which makes `approve_memo` refuse that
officer's approval by name of the flag. The API exposes the
whole gate: submit, approve, return, flag, recompose, plus a
memo's version history (its supersession chain with derived
statuses, its events, and its diff) and the diff record itself.

## Committee gate (round 7)

`committee.py` holds the second gate's discipline — the quorum
rules, the adverse-action and deferral vocabularies, condition
and reason validation, full-status derivation, and the notice
renderer — and `CreditPipeline` runs the workflow:
`convene_committee` brings the officer-final memo before the
committee, `decide_committee` records the decision. Convening
and deciding are events on the same review-event stream the
officer gate writes, so `derive_full_status` reads one ordered
record across both gates: a memo that has not reached officer
approval has no committee state; past it, convened and decided
events land in_committee and then the outcome's status
(committee_approved, committee_approved_conditional, declined,
deferred). The decidable artefact is checked before any state
check: a memo superseded by a newer version, or one that is not
the officer-final artefact, is refused by name.

A decision is a stored, content-addressed `CommitteeDecision`
(migration 0007): outcome, members, quorum, recorder, and the
outcome's payload. The quorum discipline is mechanical —
`validate_quorum` requires at least two distinct named members
with committee roles, the recorder among them, whatever quorum
the caller states (a stated quorum of 1 is itself refused).
Outcome payloads are exclusive: conditions exist only on
approve_with_conditions, adverse reasons only on a decline,
deferral reasons and re-presentment requirements only on a
deferral.

Conditions are validated against the memo's own inputs. A
financial covenant must name a figure key of the pinned
computation pack, and its threshold must be a key of the same
trace universe `verify_memo` builds (`build_trace_universe` over
the memo's pinned artefacts); the found trace is stamped onto
the stored condition. A decline's reasons are validated tie by
tie (`_tie_problem`): the pinned report is consulted for the
cited test's outcome — pass is an automatic refusal — the code
must be the one that outcome speaks to (coverage failures,
leverage, concentration, documentation, staleness each have
their tests; a not_evaluable supports only the
insufficiency-of-information and documentation codes), a figure
tie must match an occurrence in the memo's verification record
(text, value and source), and a file-fact tie must name a
required checklist item the file actually lacks. The
adverse-action notice is a pure rendering of the stored
decision over the pinned report and credit file — no text is
accepted at decision time, so nothing unproven can reach the
borrower.

A deferral is not terminal: the decision record stays, the
memo's derived status reads `deferred`, and `convene_committee`
accepts the memo again from that state (re-presentment). The
next decision's id is derived over a descriptor that includes
its sequence among the memo's decisions, so the deferral and
its successor coexist as separate records and the history shows
both. The API exposes convene, decide, the committee state
(status + decisions + latest), a decision by id, and the
adverse-action notice for a memo's latest decline; memo history
gains the memo's committee decisions alongside its versions,
events and diff.

## Document disagreements (round 8)

`disagreements.py` holds the source-type vocabulary (audited
statements, tax return, management statements), the precedence
between them (audited > tax > management), and the disagreement
policy as versioned data: tolerances per pair and line, and the
materiality thresholds. Extraction stamps every value with its
source type; the spreading builder resolves, per (line, period,
source type), the type's current-version figure, takes the
highest-precedence reporter as the spread's working value, and
runs every multi-source line through `detect_disagreement`. A
difference beyond tolerance becomes a `Disagreement` record on
the spread — every source's value, provenance and SHA, deltas
against the working figure, the materiality verdict, the policy
version — and, being spread content, is covered by the spread id.

Downstream, the record is load-bearing in two places. The
computation engine walks each figure's derivation steps
(transitively, through step and figure inputs, and across the
global pack's entity boundary via the entity packs' own flags)
and stamps `disagreement_ids` on figures resting on a materially
disagreed cell; immaterial disagreements flag nothing. The memo
verifier holds the disagreements section to the exceptions
discipline: the section must exist when the spread records
disagreements, its structured id list must equal the spread's
material set exactly, and each material disagreement must appear
as the canonical paragraph `render_disagreement_paragraph`
produces — so rewording a material difference into smaller
language is a mechanical refusal, not an editorial judgement.
Flagged figures trace in the memo with a source string naming
the disagreement, so a contested figure never verifies as a
clean one. The officer gate locks the section against edits.
The API serves the records at
`/applications/{id}/disagreements[/{disagreement_id}]` with a
materiality filter; the spread payload carries them in full, so
no new tables or migration were needed.

## Committee readiness (round 9)

`readiness.py` is a pure engine over the artefacts the earlier
rounds produced. `build_readiness_report` takes a credit file, an
evaluation date (always passed in — no wall clock inside), a
policy version, and the pinned computation packs and policy
report, and returns a content-addressed `ReadinessReport`: one
entry per required input, from three sources consolidated — the
checklist (presence, plus a freshness verdict), the computation
packs (every not-computable figure, with its missing inputs), and
the policy report (not-evaluable outcomes linked onto the entries
that explain them, or standing as their own entries when no
figure entry exists). Freshness limits are read from data: the
statement-age and appraisal limits from the pinned policy pack's
own tests, the receipt-age limits for insurance certificates, tax
returns and interim statements from the versioned readiness
policy (`aldergrove-readiness` v1). An item ages by the document
that satisfied it — except single-item kinds, which age by the
latest document of the kind, because a refresh arrives as a new
version of the same document. Policy fails are never blockers
(they are the committee's judgment to make, carried as memo
exceptions); structurally non-applicable tests (a global test
with no guarantors, an appraisal test on a term loan) are never
entered.

The pipeline owns the two moments readiness is computed. At
officer approval, the report over the memo's pinned artefacts is
stored and its id + blocker count stamped onto the approval
event and the MemoDiff. At convene, readiness is recomputed at
the convene's own `as_of` and any blocker raises the refusal —
whose text, generated by `readiness_refusal_message`, names
every blocker and is the API's 422 body verbatim. Because the
report id hashes the entries, the artefacts and the date,
recomputation is also the storage discipline: identical state
records nothing new, and an earlier report is immutable evidence
of an earlier state, never an editable blessing. There is no
waiver path in the module or the pipeline — no flag, no note, no
method — by design; `render_outstanding_items` turns the same
blocker entries into the borrower-facing list, so the two can
never disagree. Storage is one table (`readiness_reports`,
migration 0008) behind the store port, payload-shaped like the
policy reports.

## Covenants (round 10)

`covenants.py` holds the monitoring structure design law 6 names.
Three separations keep it honest. First, **the set is an artefact,
the schedule is a derivation**: a `CovenantSet` (content-addressed,
migration 0009) is created once by one committee decision — its
financial-covenant conditions plus the policy template for the
facility type, held as data in the module with its policy
citations — and pins the decision id and the memo's policy pack
version. The schedule is never stored: `derive_schedule` recomputes
every test instance from the set, the file's current spread years
and an `as_of` passed in, so the same inputs always produce the
same instances, ids included. Second, **dates are lattice
arithmetic, not day-stepping**: fiscal period ends are computed on
a (year, month) lattice with the day clamped to the month's last
day, which is what makes quarter ends land on Mar 31 (not Mar 30),
leap-day closings clamp cleanly, and a June fiscal year end shift
the whole calendar. Tests run to the facility's maturity (closing
+ term) and a point-in-time covenant adds a final test at maturity
itself. A reporting deliverable's due date is its period end plus
the policy's reporting lag (90 days annual, 60 semiannual, 45
quarterly); a deliverable whose period is already spread is met
and leaves the schedule.

State is derived per instance: a fiscal test whose period is not
yet spread stands at `awaiting_inputs` from the moment its chasing
window opens, naming the spread inputs it needs — missing inputs
dominate the deadline states for a test, because a test that
cannot be run is not late, it is unready; for a deliverable the
date states stand, because the missing thing is the obligation
itself. The scheduler is the pipeline's `tick_covenants`: it walks
the derived schedules and writes an audit event the first time an
instance is seen at `due` or `overdue`, keyed by the instance id
in the event detail, so re-running the tick at the same `as_of`
records nothing twice. `covenant_due_work` folds every file's
work-state instances into one ordered queue (overdue, due,
awaiting inputs, due soon), each naming its borrower and inputs.

Third, **terms change by supersession, never by edit**. The stores
expose no update path for a set or a covenant — save is
insert-once, amendments are their own append-only artefacts
(`covenant_amendments`, migration 0009). `validate_amendment`
checks the whole discipline purely: a named author in an amending
role, a coded reason from `covenant-amendment-reasons-v1`, an
authorising decision that is an approval on the same file, and a
change that actually changes something; the minted covenant is
version n+1 naming its predecessor, and the set's current
covenants are derived as the latest version of each lineage —
amending by a superseded id walks the lineage rather than forking
it. The set's closing anchor is likewise a recorded fact, not an
edit: `CreditFile.closing_date` is set only through
`record_closing`, which audits the first recording and names a
later change as a correction; a set created before any closing
exists waits in the named `awaiting_closing_date` state, and the
schedule derivation applies the file's closing date when it
arrives without rewriting the stored set.

## Covenant testing and breach cases (round 11)

Execution deliberately adds no math. `covenant_testing.py` holds
the discipline — result identity, restatement standing, lineage,
the breach state machine and its validators, all pure — and the
pipeline supplies the engines: a due test builds a
`CovenantDefinition` from the current covenant version and calls
`compute_metrics`, which re-spreads through the round 2 engine
and recomputes through the round 3 engine; the test reads its
figure out of the stored pack's own covenant result and compares
it with the engine's operator table, so a result can never
disagree with the headroom the pack recorded. The result id is a
content hash over the instance, the covenant version, the spread
and pack ids, the outcome and the result it supersedes — the
same test over the same inputs is the same artefact, stored
insert-once in all three stores (migration `0010`).

Standing is derived, never stored: each result pins the tested
period's sources (document id, hash, version per source type,
from the spread's `PeriodSource` records), and a result reads
`superseded_by_restatement` when the current spread's sources
for that period no longer match its pin. The scheduler tick —
extended, still idempotent — runs due tests after recording
crossings: an instance whose head result is superseded, or whose
awaiting result's statements have arrived, is re-tested with the
new result naming its predecessor. A point-in-time instance
whose current period moves when newer statements arrive is
re-tested over the new period the same way.

A breach case is the one living record of this round: opened by
the failing test (its id derived from the result id), upserted
as its acts accumulate, its event history append-only inside the
payload. The validators are mechanical — a cure must name a
stored result that passes, postdates the failure, and tests the
same covenant lineage (walked across amendment supersessions);
a waiver must name a stored committee decision of the same file
with an approval outcome; escalation and defaulting must carry
codes from `breach-resolution-reasons-v1`; terminal states
refuse all transitions — and the store surface for breaches is
exactly save/get/list, which the tests pin.

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
handling · R9 committee readiness (staleness blocks) · R10 covenant model + scheduler ·
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
