-- 0005: draft credit memos (round 5). One row per memo; memos are
-- content-addressed — the memo_id derives from the pinned artefact
-- ids, the composer profile, the section content and the
-- verification record — and are never overwritten. A memo row
-- exists only because the figure verifier cleared it: the
-- verification record travels inside the payload.
CREATE TABLE IF NOT EXISTS memos (
    id BIGSERIAL PRIMARY KEY,
    memo_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    policy_version TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memos_credit_file ON memos(credit_file_id);
