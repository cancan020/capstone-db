-- Apply once to the capstone PostgreSQL database before testing identified packets.
BEGIN;

ALTER TABLE public.vital_event_logs
    ADD COLUMN source_id text,
    ADD COLUMN message_id text,
    ADD CONSTRAINT vital_event_logs_message_pair_chk
        CHECK ((source_id IS NULL) = (message_id IS NULL)),
    ADD CONSTRAINT vital_event_logs_message_key UNIQUE (source_id, message_id);

ALTER TABLE public.vital_summaries
    ADD COLUMN source_id text,
    ADD COLUMN message_id text,
    ADD CONSTRAINT vital_summaries_message_pair_chk
        CHECK ((source_id IS NULL) = (message_id IS NULL)),
    ADD CONSTRAINT vital_summaries_message_key UNIQUE (source_id, message_id);

COMMIT;
