"""Aggregate radar vital_summaries (weighted by valid_sample_count) into daily summaries and 14-day weekly reports."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, NamedTuple

from postgres_db import connect_db, psycopg

KST = timezone(timedelta(hours=9))
DAY_SECONDS = 86400
DEFAULT_CALC_VERSION = "v1"
DEFAULT_MIN_MEASURED_DAYS = 5
DISCLAIMER = "본 리포트는 의료 진단 목적이 아닌 보조적 참고 정보로 제공됩니다."

ReportSource = Literal["auto", "live", "daily_summaries"]


class AggregatedDay(NamedTuple):
    subject_id: int
    summary_date: date
    summary_ids: tuple[int, ...]
    log_ids: tuple[int, ...]


class DailySummaryRow(NamedTuple):
    summary_date: date
    summary_count: int
    measured_seconds: int
    unmeasured_seconds: int
    valid_sample_count: int
    breath_rate_weighted_sum: Decimal | None
    breath_rate_weight: Decimal
    weighting_method: str
    min_breath_rate: Decimal | None
    max_breath_rate: Decimal | None
    active_time_sec: Decimal | None
    warning_log_count: int
    emergency_log_count: int
    warning_dawn_count: int
    warning_day_count: int
    warning_night_count: int
    row_source: str


class WeeklyMetrics(NamedTuple):
    start_date: date
    end_date: date
    summary_count: int
    measured_days: int
    measured_seconds: int
    unmeasured_seconds: int
    valid_sample_count: int
    breath_rate_weighted_sum: Decimal | None
    breath_rate_weight: Decimal
    weighting_method: str
    average_bpm: Decimal | None
    min_bpm: Decimal | None
    max_bpm: Decimal | None
    activity_days: int
    total_activity_seconds: Decimal | None
    average_daily_activity_seconds: Decimal | None
    warning_log_count: int
    emergency_log_count: int
    warning_logs_by_time: dict[str, int]


AGGREGATE_COMPLETED_DAYS_SQL = """
WITH summary_daily AS (
    SELECT
        s.subject_id,
        (s.summary_at AT TIME ZONE 'Asia/Seoul')::date AS summary_date,
        COUNT(*)::int AS summary_count,
        LEAST(86400, COALESCE(SUM(s.period_seconds) FILTER (WHERE s.avg_breath_rate IS NOT NULL), 0))::int AS measured_seconds,
        COALESCE(SUM(s.valid_sample_count) FILTER (WHERE s.avg_breath_rate IS NOT NULL AND s.valid_sample_count > 0), 0)::int AS valid_sample_count,
        COUNT(*) FILTER (WHERE s.avg_breath_rate IS NOT NULL AND s.valid_sample_count > 0)::int AS sample_weighted_rows,
        COUNT(*) FILTER (WHERE s.avg_breath_rate IS NOT NULL AND (s.valid_sample_count IS NULL OR s.valid_sample_count = 0))::int AS period_weighted_rows,
        SUM(
            s.avg_breath_rate * CASE
                WHEN s.valid_sample_count IS NOT NULL AND s.valid_sample_count > 0
                THEN s.valid_sample_count::numeric
                ELSE s.period_seconds::numeric
            END
        ) FILTER (WHERE s.avg_breath_rate IS NOT NULL) AS breath_rate_weighted_sum,
        COALESCE(
            SUM(
                CASE
                    WHEN s.valid_sample_count IS NOT NULL AND s.valid_sample_count > 0
                    THEN s.valid_sample_count::numeric
                    ELSE s.period_seconds::numeric
                END
            ) FILTER (WHERE s.avg_breath_rate IS NOT NULL),
            0::numeric
        ) AS breath_rate_weight,
        MIN(COALESCE(s.min_breath_rate, s.avg_breath_rate))
            FILTER (WHERE s.avg_breath_rate IS NOT NULL OR s.min_breath_rate IS NOT NULL) AS min_breath_rate,
        MAX(COALESCE(s.max_breath_rate, s.avg_breath_rate))
            FILTER (WHERE s.avg_breath_rate IS NOT NULL OR s.max_breath_rate IS NOT NULL) AS max_breath_rate,
        SUM(s.active_time_sec) FILTER (WHERE s.active_time_sec IS NOT NULL) AS active_time_sec,
        ARRAY_AGG(s.summary_id ORDER BY s.summary_id) AS summary_ids
    FROM public.vital_summaries AS s
    WHERE (%s::bigint IS NULL OR s.subject_id = %s::bigint)
      AND (%s::date IS NULL OR s.summary_at >= (%s::date::timestamp AT TIME ZONE 'Asia/Seoul'))
      AND s.summary_at < (%s::date::timestamp AT TIME ZONE 'Asia/Seoul')
    GROUP BY s.subject_id, (s.summary_at AT TIME ZONE 'Asia/Seoul')::date
),
event_daily AS (
    SELECT
        l.subject_id,
        (l.logged_at AT TIME ZONE 'Asia/Seoul')::date AS summary_date,
        COUNT(*) FILTER (WHERE UPPER(TRIM(l.status_level)) = 'WARNING')::int AS warning_log_count,
        COUNT(*) FILTER (WHERE UPPER(TRIM(l.status_level)) = 'EMERGENCY')::int AS emergency_log_count,
        COUNT(*) FILTER (
            WHERE UPPER(TRIM(l.status_level)) = 'WARNING'
              AND EXTRACT(HOUR FROM l.logged_at AT TIME ZONE 'Asia/Seoul') < 6
        )::int AS warning_dawn_count,
        COUNT(*) FILTER (
            WHERE UPPER(TRIM(l.status_level)) = 'WARNING'
              AND EXTRACT(HOUR FROM l.logged_at AT TIME ZONE 'Asia/Seoul') >= 6
              AND EXTRACT(HOUR FROM l.logged_at AT TIME ZONE 'Asia/Seoul') < 18
        )::int AS warning_day_count,
        COUNT(*) FILTER (
            WHERE UPPER(TRIM(l.status_level)) = 'WARNING'
              AND EXTRACT(HOUR FROM l.logged_at AT TIME ZONE 'Asia/Seoul') >= 18
        )::int AS warning_night_count,
        ARRAY_AGG(l.log_id ORDER BY l.log_id) AS log_ids
    FROM public.vital_event_logs AS l
    WHERE (%s::bigint IS NULL OR l.subject_id = %s::bigint)
      AND (%s::date IS NULL OR l.logged_at >= (%s::date::timestamp AT TIME ZONE 'Asia/Seoul'))
      AND l.logged_at < (%s::date::timestamp AT TIME ZONE 'Asia/Seoul')
    GROUP BY l.subject_id, (l.logged_at AT TIME ZONE 'Asia/Seoul')::date
),
combined AS (
    SELECT
        COALESCE(s.subject_id, e.subject_id) AS subject_id,
        COALESCE(s.summary_date, e.summary_date) AS summary_date,
        COALESCE(s.summary_count, 0) AS summary_count,
        COALESCE(s.measured_seconds, 0) AS measured_seconds,
        GREATEST(0, 86400 - COALESCE(s.measured_seconds, 0))::int AS unmeasured_seconds,
        COALESCE(s.valid_sample_count, 0) AS valid_sample_count,
        s.breath_rate_weighted_sum,
        COALESCE(s.breath_rate_weight, 0::numeric) AS breath_rate_weight,
        CASE
            WHEN COALESCE(s.sample_weighted_rows, 0) > 0 AND COALESCE(s.period_weighted_rows, 0) > 0 THEN 'mixed'
            WHEN COALESCE(s.sample_weighted_rows, 0) > 0 THEN 'valid_sample_count'
            WHEN COALESCE(s.period_weighted_rows, 0) > 0 THEN 'period_seconds'
            ELSE 'none'
        END AS weighting_method,
        CASE
            WHEN COALESCE(s.breath_rate_weight, 0) > 0
            THEN ROUND(s.breath_rate_weighted_sum / s.breath_rate_weight, 2)
            ELSE NULL
        END AS avg_breath_rate,
        ROUND(s.min_breath_rate, 2) AS min_breath_rate,
        ROUND(s.max_breath_rate, 2) AS max_breath_rate,
        s.active_time_sec,
        COALESCE(e.warning_log_count, 0) AS warning_log_count,
        COALESCE(e.emergency_log_count, 0) AS emergency_log_count,
        COALESCE(e.warning_dawn_count, 0) AS warning_dawn_count,
        COALESCE(e.warning_day_count, 0) AS warning_day_count,
        COALESCE(e.warning_night_count, 0) AS warning_night_count,
        COALESCE(s.summary_ids, ARRAY[]::bigint[]) AS summary_ids,
        COALESCE(e.log_ids, ARRAY[]::bigint[]) AS log_ids
    FROM summary_daily AS s
    FULL OUTER JOIN event_daily AS e
        ON s.subject_id = e.subject_id
       AND s.summary_date = e.summary_date
),
upserted AS (
    INSERT INTO public.daily_summaries AS ds (
        subject_id, summary_date, calc_version, summary_count, measured_seconds, unmeasured_seconds,
        valid_sample_count, breath_rate_weighted_sum, breath_rate_weight, weighting_method,
        avg_breath_rate, min_breath_rate, max_breath_rate, active_time_sec,
        warning_log_count, emergency_log_count, warning_dawn_count, warning_day_count, warning_night_count,
        updated_at
    )
    SELECT
        subject_id, summary_date, %s, summary_count, measured_seconds, unmeasured_seconds,
        valid_sample_count, breath_rate_weighted_sum, breath_rate_weight, weighting_method,
        avg_breath_rate, min_breath_rate, max_breath_rate, active_time_sec,
        warning_log_count, emergency_log_count, warning_dawn_count, warning_day_count, warning_night_count,
        CURRENT_TIMESTAMP
    FROM combined
    ON CONFLICT (subject_id, summary_date, calc_version) DO UPDATE SET
        summary_count = EXCLUDED.summary_count,
        measured_seconds = EXCLUDED.measured_seconds,
        unmeasured_seconds = EXCLUDED.unmeasured_seconds,
        valid_sample_count = EXCLUDED.valid_sample_count,
        breath_rate_weighted_sum = EXCLUDED.breath_rate_weighted_sum,
        breath_rate_weight = EXCLUDED.breath_rate_weight,
        weighting_method = EXCLUDED.weighting_method,
        avg_breath_rate = EXCLUDED.avg_breath_rate,
        min_breath_rate = EXCLUDED.min_breath_rate,
        max_breath_rate = EXCLUDED.max_breath_rate,
        active_time_sec = EXCLUDED.active_time_sec,
        warning_log_count = EXCLUDED.warning_log_count,
        emergency_log_count = EXCLUDED.emergency_log_count,
        warning_dawn_count = EXCLUDED.warning_dawn_count,
        warning_day_count = EXCLUDED.warning_day_count,
        warning_night_count = EXCLUDED.warning_night_count,
        updated_at = CURRENT_TIMESTAMP
    WHERE ds.detail_pruned_at IS NULL
    RETURNING ds.subject_id, ds.summary_date
)
SELECT
    u.subject_id,
    u.summary_date,
    c.summary_ids,
    c.log_ids
FROM upserted AS u
JOIN combined AS c
    ON c.subject_id = u.subject_id
   AND c.summary_date = u.summary_date
ORDER BY u.subject_id, u.summary_date
"""


LATE_SUMMARIES_ON_PRUNED_DAYS_SQL = """
SELECT COUNT(*)
FROM public.vital_summaries AS s
JOIN public.daily_summaries AS ds
    ON ds.subject_id = s.subject_id
   AND ds.summary_date = (s.summary_at AT TIME ZONE 'Asia/Seoul')::date
   AND ds.calc_version = %s
   AND ds.detail_pruned_at IS NOT NULL
WHERE (%s::bigint IS NULL OR s.subject_id = %s::bigint)
  AND (%s::date IS NULL OR s.summary_at >= (%s::date::timestamp AT TIME ZONE 'Asia/Seoul'))
  AND s.summary_at < (%s::date::timestamp AT TIME ZONE 'Asia/Seoul')
"""


LATE_EVENTS_ON_PRUNED_DAYS_SQL = """
SELECT COUNT(*)
FROM public.vital_event_logs AS l
JOIN public.daily_summaries AS ds
    ON ds.subject_id = l.subject_id
   AND ds.summary_date = (l.logged_at AT TIME ZONE 'Asia/Seoul')::date
   AND ds.calc_version = %s
   AND ds.detail_pruned_at IS NOT NULL
WHERE (%s::bigint IS NULL OR l.subject_id = %s::bigint)
  AND (%s::date IS NULL OR l.logged_at >= (%s::date::timestamp AT TIME ZONE 'Asia/Seoul'))
  AND l.logged_at < (%s::date::timestamp AT TIME ZONE 'Asia/Seoul')
"""


LIVE_14D_SQL = """
WITH report_days AS (
    SELECT %s::date - days_ago AS report_day
    FROM generate_series(1, 14) AS days(days_ago)
),
bounds AS (
    SELECT (%s::date - 14)::timestamp AT TIME ZONE 'Asia/Seoul' AS start_ts,
           %s::date::timestamp AT TIME ZONE 'Asia/Seoul' AS end_ts
),
summary_daily AS (
    SELECT
        (s.summary_at AT TIME ZONE 'Asia/Seoul')::date AS summary_date,
        COUNT(*)::int AS summary_count,
        LEAST(86400, COALESCE(SUM(s.period_seconds) FILTER (WHERE s.avg_breath_rate IS NOT NULL), 0))::int AS measured_seconds,
        COALESCE(SUM(s.valid_sample_count) FILTER (WHERE s.avg_breath_rate IS NOT NULL AND s.valid_sample_count > 0), 0)::int AS valid_sample_count,
        COUNT(*) FILTER (WHERE s.avg_breath_rate IS NOT NULL AND s.valid_sample_count > 0)::int AS sample_weighted_rows,
        COUNT(*) FILTER (WHERE s.avg_breath_rate IS NOT NULL AND (s.valid_sample_count IS NULL OR s.valid_sample_count = 0))::int AS period_weighted_rows,
        SUM(
            s.avg_breath_rate * CASE
                WHEN s.valid_sample_count IS NOT NULL AND s.valid_sample_count > 0
                THEN s.valid_sample_count::numeric
                ELSE s.period_seconds::numeric
            END
        ) FILTER (WHERE s.avg_breath_rate IS NOT NULL) AS breath_rate_weighted_sum,
        COALESCE(
            SUM(
                CASE
                    WHEN s.valid_sample_count IS NOT NULL AND s.valid_sample_count > 0
                    THEN s.valid_sample_count::numeric
                    ELSE s.period_seconds::numeric
                END
            ) FILTER (WHERE s.avg_breath_rate IS NOT NULL),
            0::numeric
        ) AS breath_rate_weight,
        MIN(COALESCE(s.min_breath_rate, s.avg_breath_rate))
            FILTER (WHERE s.avg_breath_rate IS NOT NULL OR s.min_breath_rate IS NOT NULL) AS min_breath_rate,
        MAX(COALESCE(s.max_breath_rate, s.avg_breath_rate))
            FILTER (WHERE s.avg_breath_rate IS NOT NULL OR s.max_breath_rate IS NOT NULL) AS max_breath_rate,
        SUM(s.active_time_sec) FILTER (WHERE s.active_time_sec IS NOT NULL) AS active_time_sec
    FROM public.vital_summaries AS s CROSS JOIN bounds AS b
    WHERE s.subject_id = %s
      AND s.summary_at >= b.start_ts
      AND s.summary_at < b.end_ts
    GROUP BY (s.summary_at AT TIME ZONE 'Asia/Seoul')::date
),
event_daily AS (
    SELECT
        (l.logged_at AT TIME ZONE 'Asia/Seoul')::date AS summary_date,
        COUNT(*) FILTER (WHERE UPPER(TRIM(l.status_level)) = 'WARNING')::int AS warning_log_count,
        COUNT(*) FILTER (WHERE UPPER(TRIM(l.status_level)) = 'EMERGENCY')::int AS emergency_log_count,
        COUNT(*) FILTER (
            WHERE UPPER(TRIM(l.status_level)) = 'WARNING'
              AND EXTRACT(HOUR FROM l.logged_at AT TIME ZONE 'Asia/Seoul') < 6
        )::int AS warning_dawn_count,
        COUNT(*) FILTER (
            WHERE UPPER(TRIM(l.status_level)) = 'WARNING'
              AND EXTRACT(HOUR FROM l.logged_at AT TIME ZONE 'Asia/Seoul') >= 6
              AND EXTRACT(HOUR FROM l.logged_at AT TIME ZONE 'Asia/Seoul') < 18
        )::int AS warning_day_count,
        COUNT(*) FILTER (
            WHERE UPPER(TRIM(l.status_level)) = 'WARNING'
              AND EXTRACT(HOUR FROM l.logged_at AT TIME ZONE 'Asia/Seoul') >= 18
        )::int AS warning_night_count
    FROM public.vital_event_logs AS l CROSS JOIN bounds AS b
    WHERE l.subject_id = %s
      AND l.logged_at >= b.start_ts
      AND l.logged_at < b.end_ts
      AND UPPER(TRIM(l.status_level)) IN ('WARNING', 'EMERGENCY')
    GROUP BY (l.logged_at AT TIME ZONE 'Asia/Seoul')::date
)
SELECT
    d.report_day,
    COALESCE(s.summary_count, 0),
    COALESCE(s.measured_seconds, 0),
    GREATEST(0, 86400 - COALESCE(s.measured_seconds, 0))::int,
    COALESCE(s.valid_sample_count, 0),
    s.breath_rate_weighted_sum,
    COALESCE(s.breath_rate_weight, 0::numeric),
    CASE
        WHEN COALESCE(s.sample_weighted_rows, 0) > 0 AND COALESCE(s.period_weighted_rows, 0) > 0 THEN 'mixed'
        WHEN COALESCE(s.sample_weighted_rows, 0) > 0 THEN 'valid_sample_count'
        WHEN COALESCE(s.period_weighted_rows, 0) > 0 THEN 'period_seconds'
        ELSE 'none'
    END,
    s.min_breath_rate,
    s.max_breath_rate,
    s.active_time_sec,
    COALESCE(e.warning_log_count, 0),
    COALESCE(e.emergency_log_count, 0),
    COALESCE(e.warning_dawn_count, 0),
    COALESCE(e.warning_day_count, 0),
    COALESCE(e.warning_night_count, 0),
    CASE
        WHEN COALESCE(s.summary_count, 0) > 0
          OR COALESCE(e.warning_log_count, 0) > 0
          OR COALESCE(e.emergency_log_count, 0) > 0
        THEN 'live'
        ELSE 'none'
    END
FROM report_days AS d
LEFT JOIN summary_daily AS s ON s.summary_date = d.report_day
LEFT JOIN event_daily AS e ON e.summary_date = d.report_day
ORDER BY d.report_day
"""


ARCHIVED_14D_SQL = """
WITH report_days AS (
    SELECT %s::date - days_ago AS report_day
    FROM generate_series(1, 14) AS days(days_ago)
)
SELECT
    d.report_day,
    COALESCE(a.summary_count, 0),
    COALESCE(a.measured_seconds, 0),
    COALESCE(a.unmeasured_seconds, 86400),
    COALESCE(a.valid_sample_count, 0),
    a.breath_rate_weighted_sum,
    COALESCE(a.breath_rate_weight, 0::numeric),
    COALESCE(a.weighting_method, 'none'),
    a.min_breath_rate,
    a.max_breath_rate,
    a.active_time_sec,
    COALESCE(a.warning_log_count, 0),
    COALESCE(a.emergency_log_count, 0),
    COALESCE(a.warning_dawn_count, 0),
    COALESCE(a.warning_day_count, 0),
    COALESCE(a.warning_night_count, 0),
    CASE
        WHEN a.daily_summary_id IS NOT NULL AND a.detail_pruned_at IS NOT NULL THEN 'daily_summaries_finalized'
        WHEN a.daily_summary_id IS NOT NULL THEN 'daily_summaries'
        ELSE 'none'
    END
FROM report_days AS d
LEFT JOIN public.daily_summaries AS a
    ON a.subject_id = %s
   AND a.summary_date = d.report_day
   AND a.calc_version = %s
ORDER BY d.report_day
"""


SAVE_WEEKLY_REPORT_SQL = """
INSERT INTO public.weekly_reports (
    subject_id, report_date, prev_start_date, prev_end_date,
    recent_start_date, recent_end_date, calc_version, comparison_status,
    prev_avg_breath_rate, recent_avg_breath_rate, breath_rate_change_bpm,
    prev_avg_daily_activity_sec, recent_avg_daily_activity_sec,
    prev_warning_count, recent_warning_count,
    prev_emergency_count, recent_emergency_count,
    report_payload, generated_at
)
VALUES (
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s::jsonb, CURRENT_TIMESTAMP
)
ON CONFLICT (subject_id, report_date, calc_version) DO UPDATE SET
    prev_start_date = EXCLUDED.prev_start_date,
    prev_end_date = EXCLUDED.prev_end_date,
    recent_start_date = EXCLUDED.recent_start_date,
    recent_end_date = EXCLUDED.recent_end_date,
    comparison_status = EXCLUDED.comparison_status,
    prev_avg_breath_rate = EXCLUDED.prev_avg_breath_rate,
    recent_avg_breath_rate = EXCLUDED.recent_avg_breath_rate,
    breath_rate_change_bpm = EXCLUDED.breath_rate_change_bpm,
    prev_avg_daily_activity_sec = EXCLUDED.prev_avg_daily_activity_sec,
    recent_avg_daily_activity_sec = EXCLUDED.recent_avg_daily_activity_sec,
    prev_warning_count = EXCLUDED.prev_warning_count,
    recent_warning_count = EXCLUDED.recent_warning_count,
    prev_emergency_count = EXCLUDED.prev_emergency_count,
    recent_emergency_count = EXCLUDED.recent_emergency_count,
    report_payload = EXCLUDED.report_payload,
    generated_at = CURRENT_TIMESTAMP
RETURNING report_id
"""


def json_number(value: Decimal | None, digits: int = 2) -> float | None:
    return float(round(value, digits)) if value is not None else None


def _combine_weighting_methods(methods: list[str]) -> str:
    active = {method for method in methods if method != "none"}
    if not active:
        return "none"
    if len(active) == 1:
        return next(iter(active))
    return "mixed"


def count_late_packets_on_pruned_days(
    connection: psycopg.Connection,
    *,
    before_date: date,
    start_date: date | None = None,
    subject_id: int | None = None,
    calc_version: str = DEFAULT_CALC_VERSION,
) -> dict[str, int]:
    """Count raw packets that arrived for dates whose detail records were already pruned."""
    params = (calc_version, subject_id, subject_id, start_date, start_date, before_date)
    summaries = connection.execute(LATE_SUMMARIES_ON_PRUNED_DAYS_SQL, params).fetchone()[0]
    events = connection.execute(LATE_EVENTS_ON_PRUNED_DAYS_SQL, params).fetchone()[0]
    return {"vital_summaries": summaries, "vital_event_logs": events}


def aggregate_daily_summaries(
    connection: psycopg.Connection,
    *,
    before_date: date,
    start_date: date | None = None,
    subject_id: int | None = None,
    calc_version: str = DEFAULT_CALC_VERSION,
) -> list[AggregatedDay]:
    """Roll up raw vital_summaries and vital_event_logs into public.daily_summaries.

    Returns the exact summary_id and log_id rows folded into each updated day so callers can
    scope subsequent retention pruning strictly to rows included in this rollup.
    Rows whose detail records were already pruned (detail_pruned_at IS NOT NULL) are never
    overwritten by late-arriving packets.
    """
    params = (
        subject_id, subject_id, start_date, start_date, before_date,
        subject_id, subject_id, start_date, start_date, before_date,
        calc_version,
    )
    rows = connection.execute(AGGREGATE_COMPLETED_DAYS_SQL, params).fetchall()
    return [
        AggregatedDay(
            subject_id=subject_id_val,
            summary_date=summary_date_val,
            summary_ids=tuple(summary_ids or ()),
            log_ids=tuple(log_ids or ()),
        )
        for subject_id_val, summary_date_val, summary_ids, log_ids in rows
    ]


def load_daily_summary_rows(
    connection: psycopg.Connection,
    subject_id: int,
    report_date: date,
    *,
    source: ReportSource = "auto",
    calc_version: str = DEFAULT_CALC_VERSION,
) -> list[DailySummaryRow]:
    """Load 14 completed KST days before report_date from live tables and/or daily_summaries."""
    if source == "live":
        rows = connection.execute(
            LIVE_14D_SQL, (report_date, report_date, report_date, subject_id, subject_id)
        ).fetchall()
        return [DailySummaryRow(*row) for row in rows]

    if source == "daily_summaries":
        rows = connection.execute(
            ARCHIVED_14D_SQL, (report_date, subject_id, calc_version)
        ).fetchall()
        return [DailySummaryRow(*row) for row in rows]

    # source == "auto": prefer live raw tables when present, fall back to archived daily_summaries.
    live_rows = [
        DailySummaryRow(*row)
        for row in connection.execute(
            LIVE_14D_SQL, (report_date, report_date, report_date, subject_id, subject_id)
        ).fetchall()
    ]
    has_archive = connection.execute("SELECT to_regclass('public.daily_summaries')").fetchone()[0] is not None
    if not has_archive:
        return live_rows

    archived_rows = [
        DailySummaryRow(*row)
        for row in connection.execute(
            ARCHIVED_14D_SQL, (report_date, subject_id, calc_version)
        ).fetchall()
    ]
    merged: list[DailySummaryRow] = []
    for live_day, archived_day in zip(live_rows, archived_rows):
        if archived_day.row_source == "daily_summaries_finalized":
            # Raw records for this date were already pruned; never let stray late packets shadow the archive.
            merged.append(archived_day)
        elif live_day.row_source != "none":
            merged.append(live_day)
        elif archived_day.row_source != "none":
            merged.append(archived_day)
        else:
            merged.append(live_day)
    return merged


def summarize_week(days: list[DailySummaryRow]) -> WeeklyMetrics:
    """Compute exact weighted weekly breathing average (not an unweighted average of daily averages)."""
    measured_days = sum(1 for day in days if day.breath_rate_weight > 0 and day.breath_rate_weighted_sum is not None)
    total_weight = sum((day.breath_rate_weight for day in days), Decimal(0))
    weighted_sums = [
        day.breath_rate_weighted_sum
        for day in days
        if day.breath_rate_weight > 0 and day.breath_rate_weighted_sum is not None
    ]
    total_weighted_sum = sum(weighted_sums, Decimal(0)) if weighted_sums else None
    average_bpm = (
        total_weighted_sum / total_weight
        if total_weighted_sum is not None and total_weight > 0
        else None
    )

    mins = [day.min_breath_rate for day in days if day.min_breath_rate is not None]
    maxs = [day.max_breath_rate for day in days if day.max_breath_rate is not None]
    activities = [day.active_time_sec for day in days if day.active_time_sec is not None]
    total_activity = sum(activities, Decimal(0)) if activities else None
    avg_daily_activity = total_activity / len(activities) if total_activity is not None else None

    return WeeklyMetrics(
        start_date=days[0].summary_date,
        end_date=days[-1].summary_date,
        summary_count=sum(day.summary_count for day in days),
        measured_days=measured_days,
        measured_seconds=sum(day.measured_seconds for day in days),
        unmeasured_seconds=sum(day.unmeasured_seconds for day in days),
        valid_sample_count=sum(day.valid_sample_count for day in days),
        breath_rate_weighted_sum=total_weighted_sum,
        breath_rate_weight=total_weight,
        weighting_method=_combine_weighting_methods([day.weighting_method for day in days]),
        average_bpm=average_bpm,
        min_bpm=min(mins) if mins else None,
        max_bpm=max(maxs) if maxs else None,
        activity_days=len(activities),
        total_activity_seconds=total_activity,
        average_daily_activity_seconds=avg_daily_activity,
        warning_log_count=sum(day.warning_log_count for day in days),
        emergency_log_count=sum(day.emergency_log_count for day in days),
        warning_logs_by_time={
            "dawn": sum(day.warning_dawn_count for day in days),
            "day": sum(day.warning_day_count for day in days),
            "night": sum(day.warning_night_count for day in days),
        },
    )


def _percent_change(previous: Decimal | None, recent: Decimal | None) -> Decimal | None:
    if previous is None or recent is None or previous == 0:
        return None
    return ((recent - previous) / previous) * Decimal(100)


def _week_payload(week: WeeklyMetrics) -> dict:
    return {
        "start_date": week.start_date.isoformat(),
        "end_date": week.end_date.isoformat(),
        "summary_count": week.summary_count,
        "measured_days": week.measured_days,
        "measured_seconds": week.measured_seconds,
        "unmeasured_seconds": week.unmeasured_seconds,
        "valid_sample_count": week.valid_sample_count,
        "weighting_method": week.weighting_method,
        "average_bpm": json_number(week.average_bpm),
        "min_bpm": json_number(week.min_bpm),
        "max_bpm": json_number(week.max_bpm),
        "activity_days": week.activity_days,
        "total_activity_seconds": json_number(week.total_activity_seconds, 1),
        "average_daily_activity_seconds": json_number(week.average_daily_activity_seconds, 1),
        "warning_log_count": week.warning_log_count,
        "emergency_log_count": week.emergency_log_count,
        "warning_logs_by_time": week.warning_logs_by_time,
    }


def _build_findings(
    previous: WeeklyMetrics,
    recent: WeeklyMetrics,
    *,
    bpm_change: Decimal | None,
    activity_change_pct: Decimal | None,
    min_measured_days: int,
) -> list[str]:
    findings: list[str] = []
    if bpm_change is None:
        findings.append(
            f"호흡수 비교는 주당 최소 {min_measured_days}일 이상 유효 관측이 필요합니다 "
            f"(직전 7일 {previous.measured_days}/7일, 최근 7일 {recent.measured_days}/7일)."
        )
    else:
        direction = "상승" if bpm_change > 0 else "하락" if bpm_change < 0 else "동일"
        findings.append(
            f"최근 7일 가중 평균 호흡수는 {recent.average_bpm:.2f}회/분으로 "
            f"직전 7일({previous.average_bpm:.2f}회/분) 대비 {bpm_change:+.2f}회/분({direction})입니다."
        )
        if previous.measured_days < 7 or recent.measured_days < 7:
            findings.append(
                f"일부 날짜에 측정 누락이 있어 관측일을 함께 확인해야 합니다 "
                f"(직전 {previous.measured_days}/7일, 최근 {recent.measured_days}/7일)."
            )

    if activity_change_pct is not None and recent.average_daily_activity_seconds is not None:
        findings.append(
            f"일평균 활동 시간은 직전 7일 대비 {activity_change_pct:+.1f}% 변화했습니다 "
            f"(최근 활동 기록일 {recent.activity_days}/7일)."
        )

    if recent.warning_log_count > 0:
        slot_labels = {"dawn": "새벽(00~06시)", "day": "낮(06~18시)", "night": "밤(18~24시)"}
        peak_slot = max(recent.warning_logs_by_time, key=recent.warning_logs_by_time.get)
        findings.append(
            f"최근 7일 주의 로그는 {recent.warning_log_count}건(직전 {previous.warning_log_count}건)이며 "
            f"'{slot_labels[peak_slot]}'에 가장 많이 기록되었습니다."
        )

    if recent.emergency_log_count > 0:
        findings.append(
            f"최근 7일 응급 로그가 {recent.emergency_log_count}건 기록되어 상세 이력 확인이 필요합니다."
        )

    return findings


def build_summary_report(
    subject_id: int,
    report_date: date,
    days: list[DailySummaryRow],
    *,
    source: ReportSource = "auto",
    calc_version: str = DEFAULT_CALC_VERSION,
    min_measured_days: int = DEFAULT_MIN_MEASURED_DAYS,
) -> dict:
    """Build a deterministic 14-day comparison report from weighted daily rows."""
    if len(days) != 14:
        raise ValueError("Expected exactly 14 daily rows for a 14-day summary report.")

    previous = summarize_week(days[:7])
    recent = summarize_week(days[7:])
    comparable = (
        previous.measured_days >= min_measured_days
        and recent.measured_days >= min_measured_days
        and previous.average_bpm is not None
        and recent.average_bpm is not None
    )
    bpm_change = (recent.average_bpm - previous.average_bpm) if comparable else None
    bpm_change_pct = _percent_change(previous.average_bpm, recent.average_bpm) if comparable else None
    activity_change_pct = _percent_change(
        previous.average_daily_activity_seconds, recent.average_daily_activity_seconds
    )

    daily_payload = []
    for day in days:
        avg_bpm = (
            day.breath_rate_weighted_sum / day.breath_rate_weight
            if day.breath_rate_weight > 0 and day.breath_rate_weighted_sum is not None
            else None
        )
        if avg_bpm is not None:
            status = "measured"
        elif day.summary_count > 0:
            status = "no_valid_measurement"
        else:
            status = "no_record"
        daily_payload.append({
            "date": day.summary_date.isoformat(),
            "summary_count": day.summary_count,
            "measured_seconds": day.measured_seconds,
            "unmeasured_seconds": day.unmeasured_seconds,
            "valid_sample_count": day.valid_sample_count,
            "weighting_method": day.weighting_method,
            "average_bpm": json_number(avg_bpm),
            "min_bpm": json_number(day.min_breath_rate),
            "max_bpm": json_number(day.max_breath_rate),
            "active_time_sec": json_number(day.active_time_sec, 1),
            "warning_log_count": day.warning_log_count,
            "emergency_log_count": day.emergency_log_count,
            "status": status,
            "row_source": day.row_source,
        })

    return {
        "subject_id": subject_id,
        "report_date": report_date.isoformat(),
        "timezone": "Asia/Seoul",
        "source": source,
        "calc_version": calc_version,
        "min_measured_days": min_measured_days,
        "daily": daily_payload,
        "previous_week": _week_payload(previous),
        "recent_week": _week_payload(recent),
        "comparison": {
            "status": "ready" if comparable else "insufficient_observations",
            "is_complete_14_days": previous.measured_days == 7 and recent.measured_days == 7,
            "average_change_bpm": json_number(bpm_change),
            "average_change_pct": json_number(bpm_change_pct, 1),
            "activity_change_pct": json_number(activity_change_pct, 1),
            "warning_log_diff": recent.warning_log_count - previous.warning_log_count,
            "emergency_log_diff": recent.emergency_log_count - previous.emergency_log_count,
        },
        "summary_findings": _build_findings(
            previous,
            recent,
            bpm_change=bpm_change,
            activity_change_pct=activity_change_pct,
            min_measured_days=min_measured_days,
        ),
        "disclaimer": DISCLAIMER,
    }


def save_weekly_report(connection: psycopg.Connection, report: dict) -> int:
    """Persist a generated 14-day report into public.weekly_reports (idempotent UPSERT)."""
    prev = report["previous_week"]
    recent = report["recent_week"]
    comp = report["comparison"]
    params = (
        report["subject_id"],
        date.fromisoformat(report["report_date"]),
        date.fromisoformat(prev["start_date"]),
        date.fromisoformat(prev["end_date"]),
        date.fromisoformat(recent["start_date"]),
        date.fromisoformat(recent["end_date"]),
        report["calc_version"],
        comp["status"],
        prev["average_bpm"],
        recent["average_bpm"],
        comp["average_change_bpm"],
        prev["average_daily_activity_seconds"],
        recent["average_daily_activity_seconds"],
        prev["warning_log_count"],
        recent["warning_log_count"],
        prev["emergency_log_count"],
        recent["emergency_log_count"],
        json.dumps(report, ensure_ascii=False),
    )
    return connection.execute(SAVE_WEEKLY_REPORT_SQL, params).fetchone()[0]


def load_summary_report(
    connection: psycopg.Connection,
    subject_id: int,
    report_date: date,
    *,
    source: ReportSource = "auto",
    calc_version: str = DEFAULT_CALC_VERSION,
    min_measured_days: int = DEFAULT_MIN_MEASURED_DAYS,
) -> dict:
    days = load_daily_summary_rows(
        connection, subject_id, report_date, source=source, calc_version=calc_version
    )
    return build_summary_report(
        subject_id,
        report_date,
        days,
        source=source,
        calc_version=calc_version,
        min_measured_days=min_measured_days,
    )


def load_saved_weekly_reports(
    connection: psycopg.Connection, subject_id: int, limit: int = 12
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT report_id, report_date, calc_version, comparison_status, generated_at, report_payload
        FROM public.weekly_reports
        WHERE subject_id = %s
        ORDER BY report_date DESC, report_id DESC
        LIMIT %s
        """,
        (subject_id, limit),
    ).fetchall()
    return [
        {
            "report_id": str(report_id),
            "report_date": report_date.isoformat(),
            "calc_version": calc_version,
            "comparison_status": status,
            "generated_at": generated_at.isoformat(),
            "report": payload,
        }
        for report_id, report_date, calc_version, status, generated_at, payload in rows
    ]


def format_summary_report(report: dict) -> str:
    prev = report["previous_week"]
    recent = report["recent_week"]
    comp = report["comparison"]
    prev_bpm = f"{prev['average_bpm']:.2f}회/분" if prev["average_bpm"] is not None else "계산 불가"
    recent_bpm = f"{recent['average_bpm']:.2f}회/분" if recent["average_bpm"] is not None else "계산 불가"
    lines = [
        f"실측 14일 주간 건강 리포트 | 대상자 {report['subject_id']} | 기준일 {report['report_date']} (KST)",
        f"직전 7일 ({prev['start_date']} ~ {prev['end_date']}) 평균 {prev_bpm} | 측정일 {prev['measured_days']}/7일 | 샘플 {prev['valid_sample_count']}개 ({prev['weighting_method']})",
        f"최근 7일 ({recent['start_date']} ~ {recent['end_date']}) 평균 {recent_bpm} | 측정일 {recent['measured_days']}/7일 | 샘플 {recent['valid_sample_count']}개 ({recent['weighting_method']})",
    ]
    if comp["status"] == "ready":
        lines.append(f"평균 변화: {comp['average_change_bpm']:+.2f}회/분")
    else:
        lines.append(f"평균 변화: 비교 불가 (관측 부족, 최소 {report['min_measured_days']}일 필요)")
    for finding in report["summary_findings"]:
        lines.append(f"  - {finding}")
    lines.append(f"※ {report['disclaimer']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject-id", type=int, default=1)
    parser.add_argument(
        "--report-date",
        type=date.fromisoformat,
        default=datetime.now(KST).date(),
        help="Comparison reference date in KST (YYYY-MM-DD); compares the 14 completed days before this date",
    )
    parser.add_argument("--source", choices=("auto", "live", "daily_summaries"), default="auto")
    parser.add_argument("--min-measured-days", type=int, default=DEFAULT_MIN_MEASURED_DAYS)
    parser.add_argument("--aggregate", action="store_true", help="UPSERT daily_summaries for the 14-day window first")
    parser.add_argument("--save-report", action="store_true", help="UPSERT the generated report into public.weekly_reports")
    parser.add_argument("--json", action="store_true", help="Print report as JSON")
    args = parser.parse_args(argv)
    if args.subject_id <= 0:
        parser.error("--subject-id must be positive")
    if not 1 <= args.min_measured_days <= 7:
        parser.error("--min-measured-days must be between 1 and 7")

    if "PGPASSWORD" not in os.environ:
        os.environ["PGPASSWORD"] = getpass.getpass("PostgreSQL password (hidden): ")

    try:
        with connect_db() as connection:
            if args.aggregate:
                aggregate_daily_summaries(
                    connection,
                    subject_id=args.subject_id,
                    start_date=args.report_date - timedelta(days=14),
                    before_date=args.report_date,
                )
            report = load_summary_report(
                connection,
                args.subject_id,
                args.report_date,
                source=args.source,
                min_measured_days=args.min_measured_days,
            )
            if args.save_report:
                report_id = save_weekly_report(connection, report)
                report["saved_report_id"] = report_id
    except psycopg.Error as exc:
        print(f"PostgreSQL 처리 실패 ({type(exc).__name__}). 접속 설정과 테이블(003 마이그레이션)을 확인하세요.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_summary_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
