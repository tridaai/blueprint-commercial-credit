-- 0003: computation packs (round 3).
-- A computation pack is the stored artefact of one engine run over
-- one spread: every figure, its derivation steps, the covenant
-- headroom results, and the supplemental figures and definitions it
-- was handed — all in the JSONB payload. The pack id is derived from
-- a hash of those inputs, so recomputing the same inputs converges
-- on the same row instead of accumulating duplicates.
CREATE TABLE IF NOT EXISTS computation_packs (
    id             BIGSERIAL PRIMARY KEY,
    pack_id        TEXT NOT NULL UNIQUE,
    credit_file_id TEXT NOT NULL REFERENCES credit_files (credit_file_id),
    payload        JSONB NOT NULL,
    built_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS computation_packs_file_idx
    ON computation_packs (credit_file_id);
