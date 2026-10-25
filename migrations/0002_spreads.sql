-- 0002: spreading (round 2).
-- Adjustments are first-class records: the spread line adjusted, the
-- coded + free-text reason, the source provenance and the proposer
-- all live in the JSONB payload, with the columns the pipeline
-- filters on broken out. Spreads are stored as built; a spread's id
-- is derived from its content, so a rebuild from the same inputs
-- converges on the same row instead of accumulating duplicates.
CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id  TEXT PRIMARY KEY,
    credit_file_id TEXT NOT NULL REFERENCES credit_files (credit_file_id),
    line_key       TEXT NOT NULL,
    fiscal_year    INT NOT NULL,
    payload        JSONB NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS adjustments_file_idx ON adjustments (credit_file_id);

CREATE TABLE IF NOT EXISTS spreads (
    id             BIGSERIAL PRIMARY KEY,
    spread_id      TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files (credit_file_id),
    payload        JSONB NOT NULL,
    built_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS spreads_file_idx ON spreads (credit_file_id);
