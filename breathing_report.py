"""Generate the 14-day breathing report for the noon-observation demo."""

import argparse
import getpass
import json
import os
import sys
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from postgres_db import connect_db, psycopg


DEMO_REPORT_DATE = date(2026, 11, 9)
DAILY_SQL = """
WITH report_days AS (
    SELECT %s::date - days_ago AS report_day
    FROM generate_series(1, 14) AS days(days_ago)
)
SELECT
    d.report_day,
    COUNT(o.observation_id),
    COUNT(o.breathing_rate_bpm),
    SUM(o.breathing_rate_bpm)
FROM report_days AS d
LEFT JOIN public.observations AS o
    ON o.subject_id = %s
   AND o.observed_at = (
       (d.report_day + TIME '12:00') AT TIME ZONE 'Asia/Seoul'
   )
GROUP BY d.report_day
ORDER BY d.report_day
"""


class DailyReading(NamedTuple):
    day: date
    record_count: int
    valid_count: int
    rate_sum: Decimal | None


def load_daily_readings(
    connection: psycopg.Connection, subject_id: int, report_date: date
) -> list[DailyReading]:
    result = connection.execute(DAILY_SQL, (report_date, subject_id))
    return [DailyReading(*row) for row in result.fetchall()]


class WeeklySummary(NamedTuple):
    start_date: date
    end_date: date
    record_count: int
    valid_count: int
    measured_days: int
    average_bpm: Decimal | None


def summarize_week(days: list[DailyReading]) -> WeeklySummary:
    valid_count = sum(day.valid_count for day in days)
    rate_sum = sum(day.rate_sum for day in days if day.rate_sum is not None)
    return WeeklySummary(
        start_date=days[0].day,
        end_date=days[-1].day,
        record_count=sum(day.record_count for day in days),
        valid_count=valid_count,
        measured_days=sum(day.valid_count > 0 for day in days),
        average_bpm=rate_sum / valid_count if valid_count else None,
    )


def average_change(previous: WeeklySummary, recent: WeeklySummary) -> Decimal | None:
    if previous.measured_days != 7 or recent.measured_days != 7:
        return None
    assert previous.average_bpm is not None and recent.average_bpm is not None
    return recent.average_bpm - previous.average_bpm


def format_week(label: str, week: WeeklySummary) -> str:
    mean = f"{week.average_bpm:.2f}회/분" if week.average_bpm is not None else "계산 불가"
    return (
        f"{label} ({week.start_date} ~ {week.end_date})  "
        f"평균 {mean} | 유효 {week.valid_count}/{week.record_count}건 | 측정일 {week.measured_days}/7일"
    )


def format_report(subject_id: int, days: list[DailyReading]) -> str:
    previous, recent = summarize_week(days[:7]), summarize_week(days[7:])
    change = average_change(previous, recent)
    lines = [
        f"14일 호흡수 비교 리포트 | 대상자 {subject_id} | 한국 시간 정오 시연 데이터",
        format_week("직전 7일", previous),
        format_week("최근 7일", recent),
    ]

    if change is None:
        lines.append("평균 변화: 비교 불가 (관측 부족)")
        for day in days:
            if day.valid_count:
                continue
            reason = "유효 측정 없음" if day.record_count else "기록 없음"
            lines.append(f"  {day.day}  {reason}")
    else:
        lines.append(f"평균 변화: {change:+.2f}회/분")
    return "\n".join(lines)


def json_number(value: Decimal | None) -> float | None:
    return float(round(value, 2)) if value is not None else None


def week_payload(week: WeeklySummary) -> dict:
    return {
        "start_date": week.start_date.isoformat(),
        "end_date": week.end_date.isoformat(),
        "record_count": week.record_count,
        "valid_count": week.valid_count,
        "measured_days": week.measured_days,
        "average_bpm": json_number(week.average_bpm),
    }


def report_payload(subject_id: int, report_date: date, days: list[DailyReading]) -> dict:
    previous, recent = summarize_week(days[:7]), summarize_week(days[7:])
    change = average_change(previous, recent)
    daily = []
    for day in days:
        average = day.rate_sum / day.valid_count if day.rate_sum is not None and day.valid_count else None
        if day.valid_count:
            status = "measured"
        elif day.record_count:
            status = "no_valid_measurement"
        else:
            status = "no_record"
        daily.append({
            "date": day.day.isoformat(),
            "record_count": day.record_count,
            "valid_count": day.valid_count,
            "average_bpm": json_number(average),
            "status": status,
        })
    return {
        "subject_id": subject_id,
        "report_date": report_date.isoformat(),
        "timezone": "Asia/Seoul",
        "source": "noon_demo",
        "daily": daily,
        "previous_week": week_payload(previous),
        "recent_week": week_payload(recent),
        "comparison": {
            "status": "ready" if change is not None else "insufficient_observations",
            "average_change_bpm": json_number(change),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-date", type=date.fromisoformat, default=DEMO_REPORT_DATE)
    parser.add_argument("--subject-id", type=int, default=1)
    parser.add_argument("--json", action="store_true", help="Print the web handoff payload as JSON")
    args = parser.parse_args()
    if args.subject_id <= 0:
        parser.error("--subject-id must be positive")

    if "PGPASSWORD" not in os.environ:
        os.environ["PGPASSWORD"] = getpass.getpass("PostgreSQL password (hidden): ")

    try:
        with connect_db() as connection:
            days = load_daily_readings(connection, args.subject_id, args.report_date)
    except psycopg.Error as exc:
        print(f"PostgreSQL 조회 실패 ({type(exc).__name__}). 접속 설정과 테이블을 확인해줘.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report_payload(args.subject_id, args.report_date, days), indent=2))
    else:
        print(format_report(args.subject_id, days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
