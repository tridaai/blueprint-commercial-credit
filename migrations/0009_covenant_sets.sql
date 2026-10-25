-- 0009: covenant sets and amendments (round 10). A covenant set
-- is created once, by one committee decision that approved a
-- memo: the decision's financial-covenant conditions plus the
-- policy pack's standard template for the facility type. It pins
-- the policy pack version and the decision id it came from, so a
-- later policy refresh never silently re-judges a live facility.
-- Sets are insert-once and never updated: a covenant's terms
-- change only by an amendment — a new covenant version recorded
-- as its own append-only artefact, superseding the old, with a
-- named author, a coded reason and the committee decision that
-- authorised it. The schedule itself is derived in code from the
-- set (pure date arithmetic over the closing date and term), so
-- no schedule rows are stored: the same set at the same date
-- always produces the same schedule.
CREATE TABLE IF NOT EXISTS covenant_sets (
    id BIGSERIAL PRIMARY KEY,
    set_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    decision_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_covenant_sets_file
    ON covenant_sets(credit_file_id);

CREATE TABLE IF NOT EXISTS covenant_amendments (
    id BIGSERIAL PRIMARY KEY,
    amendment_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    set_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_covenant_amendments_file
    ON covenant_amendments(credit_file_id);
