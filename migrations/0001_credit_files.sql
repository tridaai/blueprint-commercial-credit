-- 0001: the credit file system of record (round 1).
-- Credit files, their documents and audit events are stored as JSONB
-- payloads plus the columns the pipeline filters on; extractions keep
-- their provenance inline so a figure and its source travel together.
CREATE TABLE IF NOT EXISTS credit_files (
    credit_file_id TEXT PRIMARY KEY,
    borrower_name  TEXT NOT NULL,
    facility_type  TEXT NOT NULL,
    status         TEXT NOT NULL,
    payload        JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS extractions (
    id             BIGSERIAL PRIMARY KEY,
    credit_file_id TEXT NOT NULL REFERENCES credit_files (credit_file_id),
    field          TEXT NOT NULL,
    period_label   TEXT NOT NULL,
    payload        JSONB NOT NULL,
    extracted_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS extractions_file_idx ON extractions (credit_file_id);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id       TEXT PRIMARY KEY,
    credit_file_id TEXT NOT NULL,
    actor          TEXT NOT NULL,
    action         TEXT NOT NULL,
    payload        JSONB NOT NULL,
    at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_file_idx ON audit_events (credit_file_id);
