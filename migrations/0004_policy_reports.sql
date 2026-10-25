-- 0004: policy reports (round 4).
-- A policy report is the stored artefact of one evaluation of one
-- credit file against one pinned policy pack version: the per-test
-- outcomes, the values read, the thresholds applied and the policy
-- citations — all in the JSONB payload, with the pack version broken
-- out for filtering. The report id is derived from a hash of the
-- evaluation's inputs (pack version, computation packs read, as-of
-- date, credit-file facts), so re-evaluating unchanged inputs
-- converges on the same row; a new pack version is a new report and
-- never rewrites an earlier one.
CREATE TABLE IF NOT EXISTS policy_reports (
    id             BIGSERIAL PRIMARY KEY,
    report_id      TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files (credit_file_id),
    policy_version TEXT NOT NULL,
    payload        JSONB NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS policy_reports_file_idx
    ON policy_reports (credit_file_id);
