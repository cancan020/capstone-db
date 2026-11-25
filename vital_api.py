"""Receive processed radar packets and store them in the existing PostgreSQL tables."""

import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from breathing_report import DEMO_REPORT_DATE, load_daily_readings, report_payload
from care_report import load_care_report
from postgres_db import connect_db, psycopg
from summary_report import (
    DEFAULT_MIN_MEASURED_DAYS,
    ReportSource,
    load_saved_weekly_reports,
    load_summary_report,
)

KST = timezone(timedelta(hours=9))
Measurement = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Coordinate = Annotated[Decimal, Field(allow_inf_nan=False)]
SampleCount = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]
PacketIdentifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
logger = logging.getLogger(__name__)

app = FastAPI(title="Capstone Vital API", version="0.1.0")


class Packet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    source_id: PacketIdentifier | None = None
    message_id: PacketIdentifier | None = None
    timestamp: datetime
    status_level: Annotated[str, Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def require_identifier_pair(self):
        if (self.source_id is None) != (self.message_id is None):
            raise ValueError("source_id and message_id must be provided together")
        return self

    @field_validator("timestamp", mode="before")
    @classmethod
    def require_datetime_text(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("timestamp must be date/time text")
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        # Prototype contract: the supplied timezone-free samples use Korean time.
        if value.tzinfo is None:
            value = value.replace(tzinfo=KST)
        return value.astimezone(timezone.utc)


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: Coordinate
    y: Coordinate
    z: Coordinate


class VitalEvent(Packet):
    packet_type: Literal["DETAILED_EVENT_LOG"]
    event_type: Annotated[str, Field(min_length=1, max_length=128)]
    duration_sec: Measurement | None = None
    breath_rate: Measurement | None = None
    heart_rate: Measurement | None = None
    # The producer has not specified the sign or unit of this metric yet.
    breath_deviation: Coordinate | None = None
    location: Location | None = None


class VitalSummary(Packet):
    packet_type: Literal["PERIODIC_SUMMARY_STATS"]
    period_seconds: Annotated[int, Field(strict=True, gt=0)]
    avg_breath_rate: Measurement | None = None
    min_breath_rate: Measurement | None = None
    max_breath_rate: Measurement | None = None
    valid_sample_count: SampleCount | None = None
    avg_heart_rate: Measurement | None = None
    active_time_sec: Measurement | None = None

    @model_validator(mode="after")
    def validate_summary(self):
        if self.active_time_sec is not None and self.active_time_sec > self.period_seconds:
            raise ValueError("active_time_sec must not exceed period_seconds")
        low, avg, high = self.min_breath_rate, self.avg_breath_rate, self.max_breath_rate
        if self.valid_sample_count == 0 and any(v is not None for v in (low, avg, high)):
            raise ValueError("breath rate stats must be null when valid_sample_count is 0")
        if low is not None and high is not None and low > high:
            raise ValueError("min_breath_rate must not exceed max_breath_rate")
        if avg is not None and low is not None and avg < low:
            raise ValueError("avg_breath_rate must not be below min_breath_rate")
        if avg is not None and high is not None and avg > high:
            raise ValueError("avg_breath_rate must not exceed max_breath_rate")
        return self


class StoredPacket(BaseModel):
    saved: bool = True
    record_id: int
    subject_id: int


def resolve_subject(packet: Packet) -> int:
    if packet.subject_id is not None:
        return packet.subject_id
    fallback = os.getenv("VITAL_DEFAULT_SUBJECT_ID", "")
    try:
        subject_id = int(fallback)
        if subject_id <= 0:
            raise ValueError
        return subject_id
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Send subject_id or configure VITAL_DEFAULT_SUBJECT_ID for a single-subject demo.",
        ) from None


def insert_record(table: str, id_column: str, columns: str, parameters: tuple, packet: Packet) -> int:
    # Table and column names are fixed by the endpoints. Packet values are bound separately.
    values = parameters
    insert_columns = columns
    conflict = ""
    if packet.source_id is not None:
        insert_columns += ", source_id, message_id"
        values += (packet.source_id, packet.message_id)
        conflict = " ON CONFLICT (source_id, message_id) DO NOTHING"
    placeholders = ", ".join(["%s"] * len(values))
    query = (
        f"INSERT INTO {table} ({insert_columns}) VALUES ({placeholders})"
        f"{conflict} RETURNING {id_column}"
    )
    try:
        # Commit finishes before a success response is returned; exceptions roll back.
        with connect_db() as connection:
            if conflict:
                # A new statement can see a concurrent insert committed while ON CONFLICT waited.
                connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
            row = connection.execute(query, values).fetchone()
            if row is None and conflict:
                row = connection.execute(
                    f"SELECT {id_column}, {columns} FROM {table} WHERE source_id = %s AND message_id = %s",
                    (packet.source_id, packet.message_id),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=503, detail="Existing record changed; retry the same packet.")
                if tuple(row[1:]) != parameters:
                    raise HTTPException(status_code=409, detail="This source_id and message_id already identify a different packet.")
            return row[0]
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(status_code=422, detail="subject_id is not registered.") from None
    except psycopg.errors.CheckViolation:
        raise HTTPException(status_code=422, detail="Values violate a database constraint.") from None
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise HTTPException(status_code=503, detail="PostgreSQL is unavailable; check server settings.") from None
    except psycopg.Error as exc:
        logger.error("PostgreSQL write failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not store packet; check the database schema.") from None


@app.post("/api/v1/vital-events", status_code=201, response_model=StoredPacket,
          responses={409: {"description": "Message ID reused with different content"}})
def receive_event(packet: VitalEvent):
    subject_id = resolve_subject(packet)
    location = packet.location
    record_id = insert_record(
        "public.vital_event_logs", "log_id",
        "subject_id, logged_at, status_level, event_type, duration_sec, "
        "breath_rate, heart_rate, breath_deviation, location_x, location_y, location_z",
        (subject_id, packet.timestamp, packet.status_level, packet.event_type,
         packet.duration_sec, packet.breath_rate, packet.heart_rate,
         packet.breath_deviation,
         location.x if location is not None else None,
         location.y if location is not None else None,
         location.z if location is not None else None),
        packet,
    )
    return StoredPacket(record_id=record_id, subject_id=subject_id)


@app.post("/api/v1/vital-summary", status_code=201, response_model=StoredPacket,
          responses={409: {"description": "Message ID reused with different content"}})
def receive_summary(packet: VitalSummary):
    subject_id = resolve_subject(packet)
    record_id = insert_record(
        "public.vital_summaries", "summary_id",
        "subject_id, summary_at, status_level, period_seconds, avg_breath_rate, "
        "min_breath_rate, max_breath_rate, avg_heart_rate, active_time_sec, valid_sample_count",
        (subject_id, packet.timestamp, packet.status_level, packet.period_seconds,
         packet.avg_breath_rate, packet.min_breath_rate, packet.max_breath_rate,
         packet.avg_heart_rate, packet.active_time_sec, packet.valid_sample_count),
        packet,
    )
    return StoredPacket(record_id=record_id, subject_id=subject_id)


@app.get(
    "/api/v1/breathing-report",
    responses={404: {"description": "Subject not registered"}},
)
def read_breathing_report(
    subject_id: Annotated[int, Query(gt=0, le=2**63 - 1)] = 1,
    report_date: Annotated[date, Query(ge=date.min + timedelta(days=14))] = DEMO_REPORT_DATE,
) -> dict:
    """Compare the 14 completed Korean dates before report_date using noon demo readings."""
    try:
        with connect_db() as connection:
            subject = connection.execute(
                "SELECT 1 FROM public.subjects WHERE subject_id = %s", (subject_id,)
            ).fetchone()
            if subject is None:
                raise HTTPException(status_code=404, detail="subject_id is not registered.")
            days = load_daily_readings(connection, subject_id, report_date)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise HTTPException(status_code=503, detail="PostgreSQL is unavailable; check server settings.") from None
    except psycopg.Error as exc:
        logger.error("PostgreSQL report query failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not read the breathing report.") from None

    return report_payload(subject_id, report_date, days)


@app.get("/api/v1/care-subjects")
def read_care_subjects() -> list[dict[str, str]]:
    """List subjects registered in the local demo database."""
    try:
        with connect_db() as connection:
            rows = connection.execute(
                "SELECT subject_id, display_name FROM public.subjects ORDER BY subject_id"
            ).fetchall()
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise HTTPException(status_code=503, detail="PostgreSQL is unavailable; check server settings.") from None
    except psycopg.Error as exc:
        logger.error("PostgreSQL subject query failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not read subjects.") from None
    return [{"subject_id": str(subject_id), "display_name": display_name} for subject_id, display_name in rows]


@app.get("/api/v1/care-report", responses={404: {"description": "Subject not registered"}})
def read_care_report(
    subject_id: Annotated[int, Query(gt=0, le=2**63 - 1)],
    report_date: Annotated[date, Query(ge=date.min + timedelta(days=14))] = DEMO_REPORT_DATE,
) -> dict:
    """Compare two weeks in Korean time and return the latest 50 received event logs."""
    if report_date.weekday() != 0:
        raise HTTPException(status_code=422, detail="report_date must be a Monday in Korean time.")
    try:
        with connect_db() as connection:
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            subject = connection.execute(
                "SELECT 1 FROM public.subjects WHERE subject_id = %s", (subject_id,)
            ).fetchone()
            if subject is None:
                raise HTTPException(status_code=404, detail="subject_id is not registered.")
            return load_care_report(connection, subject_id, report_date)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise HTTPException(status_code=503, detail="PostgreSQL is unavailable; check server settings.") from None
    except psycopg.Error as exc:
        logger.error("PostgreSQL care report query failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not read the care report.") from None


@app.get("/api/v1/summary-report", responses={404: {"description": "Subject not registered"}})
def read_summary_report(
    subject_id: Annotated[int, Query(gt=0, le=2**63 - 1)] = 1,
    report_date: Annotated[date, Query(ge=date.min + timedelta(days=14))] = DEMO_REPORT_DATE,
    source: ReportSource = "auto",
    min_measured_days: Annotated[int, Query(ge=1, le=7)] = DEFAULT_MIN_MEASURED_DAYS,
) -> dict:
    """Compare 14 completed KST days using vital_summaries (weighted by valid_sample_count) and archived daily_summaries."""
    try:
        with connect_db() as connection:
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            subject = connection.execute(
                "SELECT 1 FROM public.subjects WHERE subject_id = %s", (subject_id,)
            ).fetchone()
            if subject is None:
                raise HTTPException(status_code=404, detail="subject_id is not registered.")
            return load_summary_report(
                connection,
                subject_id,
                report_date,
                source=source,
                min_measured_days=min_measured_days,
            )
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise HTTPException(status_code=503, detail="PostgreSQL is unavailable; check server settings.") from None
    except psycopg.Error as exc:
        logger.error("PostgreSQL summary report query failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not read the summary report.") from None


@app.get("/api/v1/weekly-reports", responses={404: {"description": "Subject not registered"}})
def read_weekly_reports(
    subject_id: Annotated[int, Query(gt=0, le=2**63 - 1)] = 1,
    limit: Annotated[int, Query(ge=1, le=52)] = 12,
) -> list[dict]:
    """List persisted weekly health reports (365-day retention) for a subject."""
    try:
        with connect_db() as connection:
            subject = connection.execute(
                "SELECT 1 FROM public.subjects WHERE subject_id = %s", (subject_id,)
            ).fetchone()
            if subject is None:
                raise HTTPException(status_code=404, detail="subject_id is not registered.")
            return load_saved_weekly_reports(connection, subject_id, limit=limit)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise HTTPException(status_code=503, detail="PostgreSQL is unavailable; check server settings.") from None
    except psycopg.Error as exc:
        logger.error("PostgreSQL weekly reports query failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not read weekly reports.") from None
