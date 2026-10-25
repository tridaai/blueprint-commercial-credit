-- 0008: committee readiness (round 9). A readiness report is a
-- stored, content-addressed artefact: the consolidated state of
-- every required input for a credit file at one evaluation date —
-- checklist items present/missing/stale, the policy pack's
-- statement-age and appraisal limits applied, and the computation
-- pack's not-computable figures named with the inputs they wait
-- on. Reports are append-only: a new evaluation date or refreshed
-- input produces a NEW report; an earlier report is never edited,
-- so it can never be made to bless a file that has gone stale.
CREATE TABLE IF NOT EXISTS readiness_reports (
    id BIGSERIAL PRIMARY KEY,
    report_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    as_of DATE NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_readiness_reports_file
    ON readiness_reports(credit_file_id);
