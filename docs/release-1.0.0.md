# Release notes — 1.0.0

Commercial Credit Memo & Covenant Monitoring Agent (Trida AI
Blueprint #16), for the fictional bank Aldergrove Bank. This is
the frozen twenty-round build: every gate re-run on the
finished tree at release (see CHANGELOG.md for the
round-by-round account and the README's Honest status for the
per-round evidence).

## What a deployer gets

A governed commercial-credit agent: the system a bank's
commercial lending team needs around an AI model. A credit
application arrives; the agent spreads the borrower's
statements, computes the ratios, tests lending policy, drafts
the committee memo, and — after closing — re-tests covenants
on schedule from fresh financials. It hangs on one rule: **the
model never computes a number.** Every figure is read from a
borrower document with a pointer to its page and line, or
computed in code from figures that were. The agent assembles,
drafts and monitors; the credit officer and the credit
committee decide. Nothing in this system approves credit. In
the box:

- The full credit lifecycle: intake through a LOS port →
  facility-type checklists citing their policy rules → hashed
  document intake with provenance-carrying extraction →
  spreading onto the bank's template (unmapped lines carried,
  never dropped) → the computation engine (every figure a
  derivation chain a verifier re-performs) → the versioned
  policy pack (pass / fail / *not evaluable*, the third
  outcome naming its missing input) → memo drafting behind a
  figure verifier that refuses any draft carrying an
  untraceable figure → the credit officer gate (named
  officers, edits re-verified, every approval's diff
  recorded) → the committee gate (quorum of named members,
  structured conditions, declines carrying coded ECOA reasons
  mechanically tied to evidence, the borrower notice generated
  from those codes) → committee readiness (stale or missing
  inputs block the convene, by name; there is no waiver) →
  the covenant loop (sets created at approval, schedules
  derived in code, tests executed through the same engines,
  breach cases worked as coded acts) → annual reviews (packs
  assembled from the record, policy run under the pinned and
  the current pack, differences surfaced) → portfolio views
  over the whole book.
- The governance layers: disagreement handling — audited,
  tax-return and management figures carried side by side
  under a stated precedence, never averaged, with figures
  over contested lines flagged and the memo's disagreements
  section verified verbatim; a coded risk-rating scorecard
  with one-grade, countersigned overrides, wired in as an
  input and kept out of the gates; the officer console, a
  single static page that decides nothing and renders
  borrower text as inert text; the model gateway — profiles
  as data, loaded fail-closed, every call priced and
  ledgered before any verifier runs, per-file budgets
  enforced before transport; injection defences (a
  deterministic pattern scanner, quarantine events,
  fencing on every model surface, planted figures grounding
  nothing); and retrieval access control (a versioned role
  policy, existence hiding, coded refusals, every denial
  recorded).
- The production profile, verified: the whole suite runs
  against PostgreSQL 16 with outcome parity against the
  local profile, the migrations are graded on a fresh
  database, and a production smoke walks one credit file
  through the whole lifecycle over HTTP.
- The gates that grade it all: the pytest suite, an eval
  gate with zero-tolerance counters (provenance and trace
  completeness at 100%, the silent-pass counter at zero,
  threshold boundaries graded on the line, the access matrix
  graded cell by cell), and the demo, which walks the sample
  application end to end, offline.

## How to run it

```bash
make install          # uv sync --extra dev (pinned, frozen constraints)
make demo             # the sample application, intake to extraction, traced
make test             # pytest (local store profile by default)
make evals            # the eval gate (provenance completeness = 100% required)
make prod-smoke       # production smoke on Postgres: needs DATABASE_URL set
make serve            # the API on http://localhost:8000 (console at /console)
```

No `uv`? `make install-pip` is the two-step fallback: install
the frozen `constraints.txt`, then the project with
`--no-deps -e .`.

**Postgres:** set `DATABASE_URL` and the same code runs on
PostgreSQL 16 — numbered migrations apply at startup, payloads
are JSONB by design. This profile is live-verified: run the
suite with `CREDIT_TEST_DATABASE_URL` set and every test
executes against the real store with a fresh migrated schema;
run the eval gate with `CREDIT_EVAL_DATABASE_URL` set and it
additionally grades the migrations, the production smoke, and
the cross-profile parity gap. Unset, the local profile uses
SQLite (the graded default for tests and the demo).

**Configuration** is environment-only; see the README's
Configuration table. The load-bearing ones: `DATABASE_URL`
(store profile), `S3_ENDPOINT_URL` / `S3_BUCKET` (document
store — the compose stack's SeaweedFS at `http://minio:8333`),
`CREDIT_MODEL_PROFILE` (the deployment's drafting profile,
validated at startup; `offline` by default),
`CREDIT_EXTRACTION_PROFILE` (the model-assisted extractor,
unset by default), and the live profiles' key/endpoint
variables — variable names only; values live in the
deployment's environment, never in the repo.

`docker compose up` brings up the API, Postgres 16 + pgvector
and the SeaweedFS S3 gateway in the portfolio's standard
shape; the Dockerfile is multi-stage and non-root. Stated
plainly: the image build and the compose stack are verified
by inspection only (see the honest limits).

## The freeze

Every gate below was run on the release tree at 1.0.0; the
figures are the gate outputs, not summaries of earlier rounds.

- **pytest:** 395 passed — with `CREDIT_TEST_DATABASE_URL`
  set, so the suite ran against the Postgres profile, not
  the doubles.
- **Eval gate:** PASS — with `CREDIT_EVAL_DATABASE_URL` set:
  `pg_migration_failures` 0, `prod_smoke_step_failures` 0,
  `pg_suite_parity_gap` 0 over 393 tests compared,
  `pg_checks_skipped` 0. (Two tests are profile-conditional
  by design and are graded by their own legs rather than the
  cross-profile comparison — that is why the comparison
  covers 393 of the 395.) The default-profile counters hold
  as throughout: provenance and trace completeness 100%, the
  silent-pass counter 0.
- **Production smoke:** 21/21 steps passed — the API booted
  on a fresh Postgres database, one credit file walked over
  HTTP from opening to a covenant test that records an honest
  failure and opens a real breach case.
- **Demo:** exit 0 — the sample application walked end to
  end, offline, every extracted figure printed with the
  document, page and line it came from.

## The honest limits

- **No real LOS or core-banking integration.** Intake,
  documents and storage sit behind ports with a reference
  feed and local implementations; a deployment wires its own
  systems behind those ports. Nothing here has spoken to a
  real origination system.
- **Document extraction is stub-tested.** The default
  extractor is the offline reference extractor over the
  generated borrower documents; the round-15 model-assisted
  extractor sits behind the same port and inherits the same
  provenance contract, and it has been exercised against stub
  transports with provider-shaped responses — no live
  provider key has been used, and no claim is made about
  scanned PDFs today.
- **Live-model drafting is likewise stub-tested.** The
  gateway's live adapters are real HTTP adapters, graded
  against stub transports with hand-computed prices; drafting
  defaults to the offline profile, and the price table is
  governed reference data for the shipped models, maintained
  by the deployment.
- **The risk-rating scorecard is a coded stand-in.** The
  versioning, traceability and override discipline around it
  are real; the factor bands are illustrative — not a
  validated, back-tested model under a model-risk programme.
- **Nothing approves credit — the committee gate is the end
  of the line.** The committee's decisions are records with
  coded reasons; convening the bank's actual committee,
  booking the loan, and enforcing or waiving covenants in the
  bank's own systems are outside this build. Covenant
  detection is the agent's job; enforcement is the bank's.
- **The Docker image build was never run.** This build
  environment has no Docker daemon: the Dockerfile and the
  compose stack are verified by inspection only, and no run
  is claimed. The S3 document store is likewise verified to
  its port — the compose stack's SeaweedFS has not been run
  end to end.
- **Access control is an application-level policy, not an
  identity provider.** Round 17's role policy governs the
  API's surfaces under caller headers; asserting those
  headers behind real authentication is the deployment's.
- **The sample book is generated.** The borrower book is
  generated for this blueprint, its figures known by
  construction so extraction can be graded against ground
  truth; that is the repo's one data note.
- **The README's work-in-progress notice stays on.** The
  automated gates are green; hands-on developer testing and
  the companion blog post are still outstanding, and the
  notice comes off when both are done — not before.

## What a deployment still has to do

- Wire the real systems behind the ports: the LOS/core feed
  behind intake, the document store behind the S3 port, and
  the bank's own document-AI behind the extraction port —
  under the same provenance contract (a value without a page
  and line is refused whole).
- Put the deployment's authentication in front of the API and
  assert the caller headers the round-17 access policy
  judges; review the shipped role policy against the bank's
  own read rights before relying on it.
- Configure a live drafting profile if model-polished
  narrative is wanted (`CREDIT_MODEL_PROFILE`, with the key
  held in the deployment's environment), state contracted
  model prices by superseding the price table, and set the
  per-file budget to the bank's own number.
- Point `DATABASE_URL` at PostgreSQL 16, and run the suite
  and the eval gate against it (the parity legs this release
  ran against a scratch cluster).
- Build and run the Docker image (the build itself, not the
  static review), and run the compose stack's SeaweedFS end
  to end against the document port.
- Govern the scorecard as a model: validate, back-test, and
  bring the factor bands under the bank's model-risk
  programme before the rating informs anything beyond this
  blueprint's own reporting.
- Decide how committee decisions of record are handed to the
  bank's booking and covenant-enforcement processes — this
  build ends at the decision record, deliberately.
