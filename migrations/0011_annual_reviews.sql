-- 0011: annual review packs, outcomes and committee referrals
-- (round 12). Round 10 set the covenant clocks running and
-- round 11 executes their tests; this round stores the annual
-- review of the whole credit. The pack is a content-addressed
-- artefact — the review instance, the as-of date, every pinned
-- artefact id (spread, computation pack, both policy reports,
-- the readiness report) and the verified summary — stored
-- insert-once and never rewritten: a re-assembly over changed
-- inputs is a new pack, and the officer's stance names the pack
-- it read. The outcome is likewise content-addressed and
-- insert-once: a review instance closes exactly once, and a
-- changed stance is a later review's business, not an edit.
-- The referral is the recorded hand-off an escalating outcome
-- creates for the committee machinery; it is insert-once too.
CREATE TABLE IF NOT EXISTS annual_review_packs (
    id BIGSERIAL PRIMARY KEY,
    pack_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    set_id TEXT NOT NULL,
    review_number INTEGER NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_annual_review_packs_file
    ON annual_review_packs(credit_file_id);

CREATE TABLE IF NOT EXISTS annual_review_outcomes (
    id BIGSERIAL PRIMARY KEY,
    outcome_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    set_id TEXT NOT NULL,
    review_number INTEGER NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_annual_review_outcomes_file
    ON annual_review_outcomes(credit_file_id);

CREATE TABLE IF NOT EXISTS committee_referrals (
    id BIGSERIAL PRIMARY KEY,
    referral_id TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files(credit_file_id),
    outcome_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_committee_referrals_file
    ON committee_referrals(credit_file_id);
