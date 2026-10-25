-- 0007: the committee gate (round 7). A committee decision is a
-- stored record: the memo decided, the outcome, the named members
-- and the quorum they met, the structured conditions of a
-- conditional approval, and — for a decline — the coded
-- adverse-action reasons, each tied to its evidence. Decisions
-- are append-only: a deferral superseded by re-presentment is a
-- second row, never an edit of the first. Committee convening and
-- decision events ride the memo_review_events stream (0006), so
-- a memo's status across both gates derives from one record.
CREATE TABLE IF NOT EXISTS committee_decisions (
    id BIGSERIAL PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    memo_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_committee_decisions_file
    ON committee_decisions(credit_file_id);
CREATE INDEX IF NOT EXISTS idx_committee_decisions_memo
    ON committee_decisions(memo_id);
