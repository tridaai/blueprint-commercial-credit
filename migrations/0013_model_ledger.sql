-- 0013: the model cost ledger (round 15).
-- Every call through the model gateway appends one call entry:
-- the credit file it served (NULL is the system scope — a call
-- not made for any one file, so no foreign key), purpose,
-- profile, model, tokens, the price-table version and unit
-- prices it was priced with, and the exact Decimal cost. Budget
-- events record per-file cap refusals the same way. Append-only:
-- a later price-table version prices future calls; it never
-- reprices recorded ones. There is no update or delete path for
-- either table.
CREATE TABLE IF NOT EXISTS model_call_ledger (
    id BIGSERIAL PRIMARY KEY,
    entry_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT,
    purpose TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    model TEXT NOT NULL,
    price_table_version TEXT NOT NULL,
    called_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_call_ledger_file
    ON model_call_ledger(credit_file_id);

CREATE INDEX IF NOT EXISTS idx_model_call_ledger_called
    ON model_call_ledger(called_at);

CREATE TABLE IF NOT EXISTS model_budget_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_budget_events_file
    ON model_budget_events(credit_file_id);
