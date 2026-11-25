"""Read the care dashboard's weekly comparison and recent event logs."""

from datetime import date, timedelta

from breathing_report import json_number, load_daily_readings, report_payload
from postgres_db import psycopg


ACTIVITY_SQL = """
WITH bounds AS (
    SELECT %s::date AS start_day, %s::date AS split_day, %s::date AS end_day
)
SELECT
    (s.summary_at AT TIME ZONE 'Asia/Seoul')::date >= b.split_day AS recent,
    COUNT(DISTINCT (s.summary_at AT TIME ZONE 'Asia/Seoul')::date),
    SUM(s.active_time_sec)
FROM public.vital_summaries AS s CROSS JOIN bounds AS b
WHERE s.subject_id = %s
  AND s.summary_at >= (b.start_day::timestamp AT TIME ZONE 'Asia/Seoul')
  AND s.summary_at < (b.end_day::timestamp AT TIME ZONE 'Asia/Seoul')
  AND s.active_time_sec IS NOT NULL
GROUP BY recent
"""

LOG_COUNTS_SQL = """
WITH bounds AS (
    SELECT %s::date AS start_day, %s::date AS split_day, %s::date AS end_day
)
SELECT
    (l.logged_at AT TIME ZONE 'Asia/Seoul')::date >= b.split_day AS recent,
    UPPER(TRIM(l.status_level)) AS level,
    EXTRACT(HOUR FROM l.logged_at AT TIME ZONE 'Asia/Seoul')::int AS local_hour,
    COUNT(*)
FROM public.vital_event_logs AS l CROSS JOIN bounds AS b
WHERE l.subject_id = %s
  AND l.logged_at >= (b.start_day::timestamp AT TIME ZONE 'Asia/Seoul')
  AND l.logged_at < (b.end_day::timestamp AT TIME ZONE 'Asia/Seoul')
  AND UPPER(TRIM(l.status_level)) IN ('WARNING', 'EMERGENCY')
GROUP BY recent, level, local_hour
"""

HISTORY_SQL = """
SELECT log_id, logged_at, status_level, event_type, duration_sec, breath_rate
FROM public.vital_event_logs
WHERE subject_id = %s
ORDER BY logged_at DESC, log_id DESC
LIMIT 50
"""


def load_care_report(connection: psycopg.Connection, subject_id: int, report_date: date) -> dict:
    report = report_payload(
        subject_id, report_date, load_daily_readings(connection, subject_id, report_date)
    )
    weeks = {False: report["previous_week"], True: report["recent_week"]}
    for week in weeks.values():
        week.update(
            activity_days=0,
            total_activity_seconds=None,
            average_daily_activity_seconds=None,
            warning_log_count=0,
            emergency_log_count=0,
            warning_logs_by_time={"dawn": 0, "day": 0, "night": 0},
        )

    bounds = (report_date - timedelta(days=14), report_date - timedelta(days=7),
              report_date, subject_id)
    for recent, measured_days, total_seconds in connection.execute(ACTIVITY_SQL, bounds):
        weeks[recent]["activity_days"] = measured_days
        weeks[recent]["total_activity_seconds"] = json_number(total_seconds)
        weeks[recent]["average_daily_activity_seconds"] = json_number(total_seconds / measured_days)

    for recent, level, hour, count in connection.execute(LOG_COUNTS_SQL, bounds):
        week = weeks[recent]
        if level == "WARNING":
            bucket = "dawn" if hour < 6 else "day" if hour < 18 else "night"
            week["warning_log_count"] += count
            week["warning_logs_by_time"][bucket] += count
        else:
            week["emergency_log_count"] += count

    report["event_logs"] = [
        {
            "log_id": str(log_id),
            "logged_at": logged_at.isoformat(),
            "status_level": status_level,
            "event_type": event_type,
            "duration_sec": json_number(duration),
            "breath_rate": json_number(breath_rate),
        }
        for log_id, logged_at, status_level, event_type, duration, breath_rate
        in connection.execute(HISTORY_SQL, (subject_id,))
    ]
    return report
