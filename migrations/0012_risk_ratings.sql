-- 0012: risk rating assignments and overrides (round 13).
-- The rating is assigned by the coded scorecard over a stored
-- computation pack: the assignment is a content-addressed
-- artefact — scorecard id + version, the pack and spread it
-- read, the as-of date, every factor's input, trace, band and
-- score — stored insert-once and never rewritten, so a
-- scorecard version change appends a new assignment and the
-- history stands. An override is its own insert-once artefact
-- beside the assignment it moves: bounded to one grade from
-- the assigned grade, coded, countersigned. There is no update
-- path for either table's payloads, because a grade changes by
-- a new recorded act or not at all.
CREATE TABLE IF NOT EXISTS rating_assignments (
    id BIGSERIAL PRIMARY KEY,
    assignment_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    scorecard_version TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rating_assignments_file
    ON rating_assignments(credit_file_id);

CREATE TABLE IF NOT EXISTS rating_overrides (
    id BIGSERIAL PRIMARY KEY,
    override_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    assignment_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rating_overrides_file
    ON rating_overrides(credit_file_id);
