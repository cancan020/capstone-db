-- Apply once to the capstone PostgreSQL database after 001 and 002.
-- Adds long-term daily_summaries and weekly_reports tables (365-day retention)
-- plus composite indexes for time-series queries and 14-day retention pruning.
BEGIN;

CREATE TABLE IF NOT EXISTS public.daily_summaries (
    daily_summary_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id bigint NOT NULL REFERENCES public.subjects (subject_id),
    summary_date date NOT NULL,
    calc_version text NOT NULL DEFAULT 'v1',
    summary_count integer NOT NULL DEFAULT 0 CHECK (summary_count >= 0),
    measured_seconds integer NOT NULL DEFAULT 0 CHECK (measured_seconds >= 0),
    unmeasured_seconds integer NOT NULL DEFAULT 86400 CHECK (unmeasured_seconds >= 0 AND unmeasured_seconds <= 86400),
    valid_sample_count integer NOT NULL DEFAULT 0 CHECK (valid_sample_count >= 0),
    breath_rate_weighted_sum numeric,
    breath_rate_weight numeric NOT NULL DEFAULT 0 CHECK (breath_rate_weight >= 0),
    weighting_method text NOT NULL DEFAULT 'none'
        CHECK (weighting_method IN ('valid_sample_count', 'period_seconds', 'mixed', 'none')),
    avg_breath_rate numeric(5, 2) CHECK (avg_breath_rate IS NULL OR avg_breath_rate >= 0),
    min_breath_rate numeric(5, 2) CHECK (min_breath_rate IS NULL OR min_breath_rate >= 0),
    max_breath_rate numeric(5, 2) CHECK (max_breath_rate IS NULL OR max_breath_rate >= 0),
    active_time_sec numeric CHECK (active_time_sec IS NULL OR active_time_sec >= 0),
    warning_log_count integer NOT NULL DEFAULT 0 CHECK (warning_log_count >= 0),
    emergency_log_count integer NOT NULL DEFAULT 0 CHECK (emergency_log_count >= 0),
    warning_dawn_count integer NOT NULL DEFAULT 0 CHECK (warning_dawn_count >= 0),
    warning_day_count integer NOT NULL DEFAULT 0 CHECK (warning_day_count >= 0),
    warning_night_count integer NOT NULL DEFAULT 0 CHECK (warning_night_count >= 0),
    detail_pruned_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT daily_summaries_subject_date_ver_key UNIQUE (subject_id, summary_date, calc_version),
    CONSTRAINT daily_summaries_breath_weight_consistency_chk
        CHECK ((breath_rate_weight = 0 AND breath_rate_weighted_sum IS NULL AND avg_breath_rate IS NULL)
               OR (breath_rate_weight > 0 AND breath_rate_weighted_sum IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS public.weekly_reports (
    report_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id bigint NOT NULL REFERENCES public.subjects (subject_id),
    report_date date NOT NULL,
    prev_start_date date NOT NULL,
    prev_end_date date NOT NULL,
    recent_start_date date NOT NULL,
    recent_end_date date NOT NULL,
    calc_version text NOT NULL DEFAULT 'v1',
    comparison_status text NOT NULL
        CHECK (comparison_status IN ('ready', 'insufficient_observations')),
    prev_avg_breath_rate numeric(5, 2),
    recent_avg_breath_rate numeric(5, 2),
    breath_rate_change_bpm numeric(5, 2),
    prev_avg_daily_activity_sec numeric,
    recent_avg_daily_activity_sec numeric,
    prev_warning_count integer NOT NULL DEFAULT 0 CHECK (prev_warning_count >= 0),
    recent_warning_count integer NOT NULL DEFAULT 0 CHECK (recent_warning_count >= 0),
    prev_emergency_count integer NOT NULL DEFAULT 0 CHECK (prev_emergency_count >= 0),
    recent_emergency_count integer NOT NULL DEFAULT 0 CHECK (recent_emergency_count >= 0),
    report_payload jsonb NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT weekly_reports_subject_date_ver_key UNIQUE (subject_id, report_date, calc_version)
);

CREATE INDEX IF NOT EXISTS idx_observations_subject_observed_at
    ON public.observations (subject_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_vital_summaries_subject_summary_at
    ON public.vital_summaries (subject_id, summary_at);

CREATE INDEX IF NOT EXISTS idx_vital_summaries_summary_at
    ON public.vital_summaries (summary_at);

CREATE INDEX IF NOT EXISTS idx_vital_event_logs_subject_logged_at
    ON public.vital_event_logs (subject_id, logged_at DESC, log_id DESC);

CREATE INDEX IF NOT EXISTS idx_vital_event_logs_logged_at
    ON public.vital_event_logs (logged_at);

CREATE INDEX IF NOT EXISTS idx_daily_summaries_subject_date
    ON public.daily_summaries (subject_id, summary_date);

CREATE INDEX IF NOT EXISTS idx_daily_summaries_summary_date
    ON public.daily_summaries (summary_date);

CREATE INDEX IF NOT EXISTS idx_weekly_reports_subject_date
    ON public.weekly_reports (subject_id, report_date DESC);

CREATE INDEX IF NOT EXISTS idx_weekly_reports_generated_at
    ON public.weekly_reports (generated_at);

COMMIT;
