-- 0010: covenant test results and breach cases (round 11). Round
-- 10 stored the covenant sets and derived their schedules; this
-- round executes the tests. A test result is a content-addressed
-- artefact — the instance, the covenant version tested, the
-- spread and computation pack the figure came out of, and the
-- outcome — stored insert-once and never rewritten: a re-test is
-- a new result naming the one it supersedes, and a restatement
-- flags the old result's standing at read time, in the open.
-- A breach case is the bank's workflow object for one failing
-- result: a living record whose state moves only by recorded
-- acts (review, escalation, cure, waiver, default path), each
-- appended to its event history. Its row is upserted on those
-- acts, like the credit file's own row; the events inside the
-- payload are append-only, and there is no delete path.
CREATE TABLE IF NOT EXISTS covenant_test_results (
    id BIGSERIAL PRIMARY KEY,
    result_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    instance_id TEXT NOT NULL,
    covenant_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_covenant_test_results_file
    ON covenant_test_results(credit_file_id);

CREATE TABLE IF NOT EXISTS breach_cases (
    id BIGSERIAL PRIMARY KEY,
    breach_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_breach_cases_file
    ON breach_cases(credit_file_id);
