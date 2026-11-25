"""Register the four fictional care dashboard subjects in the local PostgreSQL DB."""

import getpass
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from postgres_db import connect_db, psycopg


DEMO_FILE = Path(__file__).resolve().parent / "demo" / "care_monitor_subjects.json"
KST = timezone(timedelta(hours=9))
DAY_SECONDS = 86400
SOURCE_ID = "capstone-care-demo-v1"
DEMO_KEYS = {"stable", "dawn-warning", "fall-history", "missing"}


class DemoDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    breathing_rate_bpm: Annotated[Decimal, Field(gt=0, max_digits=5, decimal_places=2)] | None
    active_time_sec: Annotated[int, Field(strict=True, ge=0, le=DAY_SECONDS)] | None


class DemoEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    status_level: Literal["WARNING", "EMERGENCY"]
    event_type: Annotated[str, Field(min_length=1, max_length=128)]
    duration_sec: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None


class DemoSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Literal["stable", "dawn-warning", "fall-history", "missing"]
    display_name: Annotated[str, Field(pattern=r"^\[시연\] .+$", max_length=50)]
    daily: Annotated[list[DemoDay], Field(min_length=14, max_length=14)]
    events: list[DemoEvent]


class DemoData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_date: date
    subjects: Annotated[list[DemoSubject], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def validate_period(self):
        if self.report_date.weekday() != 0:
            raise ValueError("The report date must be a Monday.")
        if {subject.key for subject in self.subjects} != DEMO_KEYS:
            raise ValueError("Include each of the four demo scenarios once.")
        if len({subject.display_name for subject in self.subjects}) != len(self.subjects):
            raise ValueError("Demo names must be distinct.")
        days = [self.report_date - timedelta(days=offset) for offset in range(14, 0, -1)]
        for subject in self.subjects:
            if [entry.date for entry in subject.daily] != days:
                raise ValueError(f"{subject.key}: expected 14 consecutive dates before report_date.")
            if all(entry.active_time_sec is None for entry in subject.daily):
                raise ValueError(f"{subject.key}: at least one activity record is required.")
            times = [event.timestamp for event in subject.events]
            if len(set(times)) != len(times) or any(
                when.utcoffset() != KST.utcoffset(None) or not days[0] <= when.date() < self.report_date
                for when in times
            ):
                raise ValueError(f"{subject.key}: event times must be distinct, in range, and use +09:00.")
        return self


def insert_demo(connection: psycopg.Connection, demo: DemoData) -> list[tuple[int, str, bool]]:
    """Import each scenario once. The caller commits or rolls back the whole batch."""
    connection.execute("SELECT pg_advisory_xact_lock(1861903850, 1)")
    results = []
    with connection.cursor() as cursor:
        for subject in demo.subjects:
            source = f"{SOURCE_ID}.{subject.key}"
            registered = cursor.execute(
                "SELECT DISTINCT subject_id FROM public.vital_summaries WHERE source_id = %s",
                (source,),
            ).fetchall()
            matching = cursor.execute(
                "SELECT subject_id FROM public.subjects WHERE display_name = %s ORDER BY subject_id",
                (subject.display_name,),
            ).fetchall()
            if len(registered) == 1 and registered == matching and registered[0][0] != 1:
                results.append((registered[0][0], subject.display_name, False))
                continue
            if registered or matching:
                raise ValueError(f"시연 대상자 정보가 기존 기록과 겹칩니다: {subject.display_name}")

            subject_id = cursor.execute(
                "INSERT INTO public.subjects (display_name) VALUES (%s) RETURNING subject_id",
                (subject.display_name,),
            ).fetchone()[0]
            observations = []
            summaries = []
            for day in subject.daily:
                stamp = datetime.combine(day.date, time(12), tzinfo=KST)
                if day.breathing_rate_bpm is not None:
                    observations.append((subject_id, stamp, day.breathing_rate_bpm))
                if day.active_time_sec is not None:
                    summaries.append((subject_id, stamp, DAY_SECONDS, day.active_time_sec,
                                      source, day.date.isoformat()))

            cursor.executemany(
                """INSERT INTO public.observations (subject_id, observed_at, breathing_rate_bpm)
                   VALUES (%s, %s, %s)""",
                observations,
            )
            cursor.executemany(
                """INSERT INTO public.vital_summaries
                   (subject_id, summary_at, period_seconds, active_time_sec, status_level, source_id, message_id)
                   VALUES (%s, %s, %s, %s, 'NORMAL', %s, %s)""",
                summaries,
            )
            cursor.executemany(
                """INSERT INTO public.vital_event_logs
                   (subject_id, logged_at, status_level, event_type, duration_sec, source_id, message_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                [(subject_id, event.timestamp, event.status_level, event.event_type, event.duration_sec,
                  source, f"event-{index}") for index, event in enumerate(subject.events, start=1)],
            )
            results.append((subject_id, subject.display_name, True))
    return results


def main() -> int:
    try:
        demo = DemoData.model_validate_json(DEMO_FILE.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        print(f"시연 JSON을 읽을 수 없습니다. ({type(exc).__name__})")
        return 1

    try:
        if "PGPASSWORD" not in os.environ:
            os.environ["PGPASSWORD"] = getpass.getpass("PostgreSQL password (hidden): ")
        with connect_db() as connection:
            if not connection.execute("SELECT 1 FROM public.subjects WHERE subject_id = 1").fetchone():
                raise ValueError("기존 시연 대상자 1번이 없습니다. capstone DB 연결 설정을 확인하세요.")
            results = insert_demo(connection, demo)
    except (EOFError, KeyboardInterrupt):
        print("\n등록을 취소했습니다.")
        return 1
    except ValueError as exc:
        print(exc)
        return 1
    except psycopg.Error as exc:
        print(f"시연 데이터 등록 실패 ({type(exc).__name__}). PostgreSQL 연결 설정과 테이블을 확인하세요.")
        return 1

    for subject_id, name, created in results:
        print(f"{subject_id}: {name} ({'신규 등록' if created else '이미 등록됨'})")
    print(f"화면을 새로고침하고 비교 기준일을 {demo.report_date}로 선택하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
