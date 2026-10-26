-- 0014: quarantine events, quarantine reviews, access denials
-- (round 17).
-- A quarantine event records one instruction-pattern match in
-- one borrower document, found by the deterministic scanner at
-- ingestion: the document, the field, the pattern, the exact
-- span, and the figures inside the span. A review is a separate
-- append-only record — a named officer's review never edits the
-- event and never un-fences the span. Access denials record
-- every refusal the retrieval access policy produces, so the
-- refusal record can be reconciled against what was asked.
-- Append-only: there is no update or delete path for any of
-- these tables.
CREATE TABLE IF NOT EXISTS quarantine_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quarantine_events_file
    ON quarantine_events(credit_file_id);

CREATE INDEX IF NOT EXISTS idx_quarantine_events_document
    ON quarantine_events(document_id);

CREATE TABLE IF NOT EXISTS quarantine_reviews (
    id BIGSERIAL PRIMARY KEY,
    review_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    credit_file_id TEXT NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quarantine_reviews_file
    ON quarantine_reviews(credit_file_id);

CREATE TABLE IF NOT EXISTS access_denials (
    id BIGSERIAL PRIMARY KEY,
    denial_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT,
    caller TEXT NOT NULL,
    role TEXT NOT NULL,
    family TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_access_denials_file
    ON access_denials(credit_file_id);
