-- Apply once if sql/003_daily_weekly_retention.sql was already applied earlier.
-- Adds detail_pruned_at so finalized daily_summaries rows are locked against late-packet overwrites.
BEGIN;

ALTER TABLE public.daily_summaries
    ADD COLUMN IF NOT EXISTS detail_pruned_at timestamptz;

COMMIT;
