-- 0006: the credit officer gate (round 6). Review events are the
-- append-only record of every gate action — submission, approval,
-- return with coded reasons, flags, recomposition — each naming
-- the actor and their role; a memo's review status is derived
-- from its events, never stored on the memo. Diffs record what an
-- approval changed between the draft and the officer-final memo:
-- section text before/after and figure changes with their trace
-- sources. Both are stored once and never overwritten.
CREATE TABLE IF NOT EXISTS memo_review_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    memo_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memo_review_events_file
    ON memo_review_events(credit_file_id);
CREATE INDEX IF NOT EXISTS idx_memo_review_events_memo
    ON memo_review_events(memo_id);

CREATE TABLE IF NOT EXISTS memo_diffs (
    id BIGSERIAL PRIMARY KEY,
    diff_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    from_memo_id TEXT NOT NULL,
    to_memo_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memo_diffs_credit_file
    ON memo_diffs(credit_file_id);
CREATE INDEX IF NOT EXISTS idx_memo_diffs_final
    ON memo_diffs(to_memo_id);
