-- Initial schema for a new capstone database, based on the PostgreSQL ERD.
-- Apply 001_message_dedup.sql after this file.
BEGIN;

CREATE TABLE public.subjects (
    subject_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    display_name varchar(50) NOT NULL
);

CREATE TABLE public.observations (
    observation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id bigint NOT NULL CONSTRAINT fk_observations_subjects REFERENCES public.subjects (subject_id),
    observed_at timestamptz NOT NULL,
    breathing_rate_bpm numeric(5, 2),
    status varchar(20)
);

CREATE TABLE public.events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id bigint NOT NULL CONSTRAINT fk_events_subjects REFERENCES public.subjects (subject_id),
    event_type varchar(30) NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz
);

CREATE TABLE public.vital_event_logs (
    log_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id bigint NOT NULL REFERENCES public.subjects (subject_id),
    logged_at timestamptz NOT NULL,
    status_level text NOT NULL,
    event_type text NOT NULL,
    duration_sec numeric,
    breath_rate numeric,
    heart_rate numeric,
    breath_deviation numeric,
    location_x numeric,
    location_y numeric,
    location_z numeric,
    received_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE public.vital_summaries (
    summary_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id bigint NOT NULL REFERENCES public.subjects (subject_id),
    summary_at timestamptz NOT NULL,
    status_level text NOT NULL,
    period_seconds integer NOT NULL,
    avg_breath_rate numeric,
    min_breath_rate numeric,
    max_breath_rate numeric,
    avg_heart_rate numeric,
    active_time_sec numeric,
    received_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMIT;
