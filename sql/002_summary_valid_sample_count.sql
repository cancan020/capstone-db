-- Apply once to the capstone PostgreSQL database to store valid_sample_count
-- sent by the radar PERIODIC_SUMMARY_STATS packets.
BEGIN;

ALTER TABLE public.vital_summaries
    ADD COLUMN valid_sample_count integer,
    ADD CONSTRAINT vital_summaries_valid_sample_count_chk
        CHECK (valid_sample_count IS NULL OR valid_sample_count >= 0);

COMMIT;
