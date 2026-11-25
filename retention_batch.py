"""Run daily summary rollup, weekly report archiving, and 14d/365d retention pruning.

Policy (from 캡스톤 작품요약서):
- Detailed radar tables (vital_summaries, vital_event_logs): retained for 14 days after the KST recording day ends.
  Rows are ONLY deleted when:
    1) They were explicitly included in this batch's daily_summaries rollup (exact PK list),
    2) Both a covering un-pruned daily_summaries row AND a covering weekly_reports row exist, and
    3) The batch runs inside a REPEATABLE READ snapshot when opened on an idle connection.
  Once detail rows for a date are deleted, daily_summaries.detail_pruned_at is stamped so late-arriving
  packets cannot overwrite the archived daily statistics.
- Aggregated tables (daily_summaries, weekly_reports): retained for 365 days.

Defaults to dry-run mode (no writes or deletes). Pass --apply to execute and commit.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone

from postgres_db import connect_db, psycopg
from summary_report import (
    DEFAULT_CALC_VERSION,
    DEFAULT_MIN_MEASURED_DAYS,
    KST,
    aggregate_daily_summaries,
    count_late_packets_on_pruned_days,
    load_summary_report,
    save_weekly_report,
)

DETAIL_RETENTION_DAYS = 14
LONG_TERM_RETENTION_DAYS = 365


def retention_cutoffs(reference_date: date) -> dict[str, date | datetime]:
    """Compute exact KST-boundary cutoffs (stored/compared in UTC) for 14d and 365d retention."""
    detail_cutoff_date = reference_date - timedelta(days=DETAIL_RETENTION_DAYS)
    long_term_cutoff_date = reference_date - timedelta(days=LONG_TERM_RETENTION_DAYS)
    detail_cutoff_ts = datetime.combine(detail_cutoff_date, time(0), tzinfo=KST).astimezone(timezone.utc)
    weekly_report_cutoff_ts = datetime.combine(long_term_cutoff_date, time(0), tzinfo=KST).astimezone(timezone.utc)
    return {
        "reference_date": reference_date,
        "detail_cutoff_date": detail_cutoff_date,
        "detail_cutoff_ts": detail_cutoff_ts,
        "daily_summary_cutoff_date": long_term_cutoff_date,
        "weekly_report_cutoff_ts": weekly_report_cutoff_ts,
    }


def _ensure_repeatable_read(connection: psycopg.Connection) -> None:
    """Pin the batch transaction to a single MVCC snapshot when the connection is still idle."""
    info = getattr(connection, "info", None)
    status = getattr(info, "transaction_status", None)
    if status == psycopg.pq.TransactionStatus.IDLE:
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ


def _detail_eligibility_predicate(ts_column: str) -> str:
    return (
        f"t.{ts_column} < %s"
        " AND EXISTS ("
        "     SELECT 1 FROM public.weekly_reports AS wr"
        "     WHERE wr.subject_id = t.subject_id"
        "       AND wr.calc_version = %s"
        f"       AND (t.{ts_column} AT TIME ZONE 'Asia/Seoul')::date BETWEEN wr.prev_start_date AND wr.recent_end_date"
        " )"
        " AND EXISTS ("
        "     SELECT 1 FROM public.daily_summaries AS ds"
        "     WHERE ds.subject_id = t.subject_id"
        "       AND ds.calc_version = %s"
        f"       AND ds.summary_date = (t.{ts_column} AT TIME ZONE 'Asia/Seoul')::date"
        "       AND ds.detail_pruned_at IS NULL"
        " )"
    )


def _prune_detail_table(
    connection: psycopg.Connection,
    *,
    table: str,
    id_column: str,
    ts_column: str,
    cutoff_ts: datetime,
    calc_version: str,
    dry_run: bool,
    included_ids: list[int] | None = None,
) -> tuple[int, int, set[tuple[int, date]]]:
    """Return (eligible_or_deleted_count, held_back_count, pruned_subject_dates) for a 14-day detail table.

    In apply mode, only rows whose primary key is in included_ids (folded into daily_summaries
    during this batch) can be deleted.
    """
    eligible_where = _detail_eligibility_predicate(ts_column)
    if dry_run:
        eligible = connection.execute(
            f"SELECT COUNT(*) FROM {table} AS t WHERE {eligible_where}",
            (cutoff_ts, calc_version, calc_version),
        ).fetchone()[0]
        total_expired = connection.execute(
            f"SELECT COUNT(*) FROM {table} AS t WHERE t.{ts_column} < %s",
            (cutoff_ts,),
        ).fetchone()[0]
        return eligible, max(0, total_expired - eligible), set()

    pruned_days: set[tuple[int, date]] = set()
    deleted_count = 0
    if included_ids:
        deleted_rows = connection.execute(
            f"""
            DELETE FROM {table} AS t
            WHERE t.{id_column} = ANY(%s::bigint[])
              AND {eligible_where}
            RETURNING t.subject_id, (t.{ts_column} AT TIME ZONE 'Asia/Seoul')::date
            """,
            (included_ids, cutoff_ts, calc_version, calc_version),
        ).fetchall()
        deleted_count = len(deleted_rows)
        pruned_days = {(subject_id, summary_date) for subject_id, summary_date in deleted_rows}

    held_back = connection.execute(
        f"SELECT COUNT(*) FROM {table} AS t WHERE t.{ts_column} < %s",
        (cutoff_ts,),
    ).fetchone()[0]
    return deleted_count, held_back, pruned_days


def _mark_pruned_daily_summaries(
    connection: psycopg.Connection,
    pruned_days: set[tuple[int, date]],
    *,
    calc_version: str,
) -> int:
    if not pruned_days:
        return 0
    ordered = sorted(pruned_days)
    subject_ids = [subject_id for subject_id, _ in ordered]
    summary_dates = [summary_date for _, summary_date in ordered]
    return connection.execute(
        """
        WITH target AS (
            SELECT UNNEST(%s::bigint[]) AS subject_id,
                   UNNEST(%s::date[]) AS summary_date
        ),
        marked AS (
            UPDATE public.daily_summaries AS ds
            SET detail_pruned_at = COALESCE(ds.detail_pruned_at, CURRENT_TIMESTAMP)
            FROM target AS t
            WHERE ds.subject_id = t.subject_id
              AND ds.summary_date = t.summary_date
              AND ds.calc_version = %s
            RETURNING 1
        )
        SELECT COUNT(*) FROM marked
        """,
        (subject_ids, summary_dates, calc_version),
    ).fetchone()[0]


def _count_or_delete_long_term(
    connection: psycopg.Connection,
    *,
    table: str,
    where_clause: str,
    param,
    dry_run: bool,
) -> int:
    if dry_run:
        return connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where_clause}",
            (param,),
        ).fetchone()[0]
    row = connection.execute(
        f"WITH deleted AS (DELETE FROM {table} WHERE {where_clause} RETURNING 1) SELECT COUNT(*) FROM deleted",
        (param,),
    ).fetchone()
    return row[0]


def run_retention_batch(
    connection: psycopg.Connection,
    reference_date: date,
    *,
    dry_run: bool = True,
    aggregate_before_delete: bool = True,
    generate_weekly: bool = False,
    calc_version: str = DEFAULT_CALC_VERSION,
    min_measured_days: int = DEFAULT_MIN_MEASURED_DAYS,
) -> dict:
    """Execute rollup + optional weekly archive + PK-scoped, guarded 14d/365d pruning in one transaction."""
    _ensure_repeatable_read(connection)
    cutoffs = retention_cutoffs(reference_date)
    aggregated_days = 0
    generated_reports = 0
    included_summary_ids: list[int] = []
    included_log_ids: list[int] = []

    late_packets = count_late_packets_on_pruned_days(
        connection,
        before_date=reference_date,
        calc_version=calc_version,
    )

    if not dry_run:
        if aggregate_before_delete:
            rows = aggregate_daily_summaries(
                connection,
                before_date=reference_date,
                calc_version=calc_version,
            )
            aggregated_days = len(rows)
            included_summary_ids = [sid for row in rows for sid in row.summary_ids]
            included_log_ids = [lid for row in rows for lid in row.log_ids]

        if generate_weekly:
            subject_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT subject_id FROM public.subjects ORDER BY subject_id"
                ).fetchall()
            ]
            for subject_id in subject_ids:
                report = load_summary_report(
                    connection,
                    subject_id,
                    reference_date,
                    source="auto",
                    calc_version=calc_version,
                    min_measured_days=min_measured_days,
                )
                save_weekly_report(connection, report)
                generated_reports += 1

    summaries_count, summaries_held_back, pruned_summary_days = _prune_detail_table(
        connection,
        table="public.vital_summaries",
        id_column="summary_id",
        ts_column="summary_at",
        cutoff_ts=cutoffs["detail_cutoff_ts"],
        calc_version=calc_version,
        dry_run=dry_run,
        included_ids=included_summary_ids,
    )
    events_count, events_held_back, pruned_event_days = _prune_detail_table(
        connection,
        table="public.vital_event_logs",
        id_column="log_id",
        ts_column="logged_at",
        cutoff_ts=cutoffs["detail_cutoff_ts"],
        calc_version=calc_version,
        dry_run=dry_run,
        included_ids=included_log_ids,
    )

    finalized_daily_rows = 0
    if not dry_run:
        finalized_daily_rows = _mark_pruned_daily_summaries(
            connection,
            pruned_summary_days | pruned_event_days,
            calc_version=calc_version,
        )

    counts = {
        "vital_summaries": summaries_count,
        "vital_event_logs": events_count,
        "daily_summaries": _count_or_delete_long_term(
            connection,
            table="public.daily_summaries",
            where_clause="summary_date < %s",
            param=cutoffs["daily_summary_cutoff_date"],
            dry_run=dry_run,
        ),
        "weekly_reports": _count_or_delete_long_term(
            connection,
            table="public.weekly_reports",
            where_clause="generated_at < %s",
            param=cutoffs["weekly_report_cutoff_ts"],
            dry_run=dry_run,
        ),
    }
    held_back = {
        "vital_summaries": summaries_held_back,
        "vital_event_logs": events_held_back,
    }

    return {
        "mode": "dry_run" if dry_run else "applied",
        "reference_date_kst": reference_date.isoformat(),
        "calc_version": calc_version,
        "cutoffs": {
            "detail_retention_days": DETAIL_RETENTION_DAYS,
            "long_term_retention_days": LONG_TERM_RETENTION_DAYS,
            "detail_before_kst_date": cutoffs["detail_cutoff_date"].isoformat(),
            "detail_before_utc": cutoffs["detail_cutoff_ts"].isoformat(),
            "daily_summary_before_kst_date": cutoffs["daily_summary_cutoff_date"].isoformat(),
            "weekly_report_before_utc": cutoffs["weekly_report_cutoff_ts"].isoformat(),
        },
        "aggregated_daily_rows": aggregated_days,
        "generated_weekly_reports": generated_reports,
        "finalized_daily_rows": finalized_daily_rows,
        "late_packets_on_pruned_days": late_packets,
        "held_back_counts": held_back,
        "expired_counts" if dry_run else "deleted_counts": counts,
    }


def format_batch_result(result: dict) -> str:
    dry_run = result["mode"] == "dry_run"
    counts = result["expired_counts"] if dry_run else result["deleted_counts"]
    held = result["held_back_counts"]
    late = result["late_packets_on_pruned_days"]
    action = "삭제 예정" if dry_run else "삭제 완료"
    cutoffs = result["cutoffs"]
    lines = [
        f"보관·삭제 배치 결과 ({'DRY RUN - 실제 변경 없음' if dry_run else 'APPLIED - DB 반영 완료'})",
        f"- 기준일(KST): {result['reference_date_kst']} (계산 버전: {result['calc_version']})",
        f"- 상세 데이터 14일 기준: {cutoffs['detail_before_kst_date']} 00:00 KST 이전 ({cutoffs['detail_before_utc']})",
        f"- 장기 보관 365일 기준: {cutoffs['daily_summary_before_kst_date']} 이전",
    ]
    if not dry_run:
        lines.append(f"- 사전 일일 집계(daily_summaries) 반영: {result['aggregated_daily_rows']}건")
        lines.append(f"- 주간 리포트(weekly_reports) 생성·갱신: {result['generated_weekly_reports']}건")
        lines.append(f"- 상세 삭제 마감 잠금(detail_pruned_at): {result['finalized_daily_rows']}건")
    lines.extend([
        f"- vital_summaries (14일): {counts['vital_summaries']}건 {action} (보류 {held['vital_summaries']}건)",
        f"- vital_event_logs (14일): {counts['vital_event_logs']}건 {action} (보류 {held['vital_event_logs']}건)",
        f"- daily_summaries (365일): {counts['daily_summaries']}건 {action}",
        f"- weekly_reports (365일): {counts['weekly_reports']}건 {action}",
    ])
    if held["vital_summaries"] > 0 or held["vital_event_logs"] > 0:
        lines.append(
            "⚠ 일부 만료 상세 기록은 주간 리포트/일일 집계 미비 또는 이번 배치 집계 미포함(동시/지연 유입)으로 삭제가 보류되었습니다."
        )
    if late["vital_summaries"] > 0 or late["vital_event_logs"] > 0:
        lines.append(
            f"⚠ 이미 마감·삭제된 날짜로 지연 도착한 패킷이 감지되었습니다 "
            f"(요약 {late['vital_summaries']}건, 이벤트 {late['vital_event_logs']}건). 기존 daily_summaries는 덮어쓰지 않고 보존했습니다."
        )
    if dry_run:
        lines.append("※ 실제 집계 및 삭제를 적용하려면 --apply 옵션을 추가해 실행하세요.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference-date",
        type=date.fromisoformat,
        default=datetime.now(KST).date(),
        help="Batch reference date in KST (YYYY-MM-DD); defaults to today in KST",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually run daily rollup, optional weekly archiving, and guarded retention deletes (default is dry-run)",
    )
    parser.add_argument(
        "--skip-aggregate",
        action="store_true",
        help="Skip rolling up completed days into daily_summaries (also prevents detail deletion for safety)",
    )
    parser.add_argument(
        "--generate-weekly",
        action="store_true",
        help="Generate and save 14-day weekly_reports for all registered subjects before pruning",
    )
    parser.add_argument(
        "--min-measured-days",
        type=int,
        default=DEFAULT_MIN_MEASURED_DAYS,
        help="Minimum measured days per week required for comparison (1..7)",
    )
    parser.add_argument("--json", action="store_true", help="Print result as JSON")
    args = parser.parse_args(argv)
    if not 1 <= args.min_measured_days <= 7:
        parser.error("--min-measured-days must be between 1 and 7")

    if "PGPASSWORD" not in os.environ:
        os.environ["PGPASSWORD"] = getpass.getpass("PostgreSQL password (hidden): ")

    try:
        with connect_db() as connection:
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            result = run_retention_batch(
                connection,
                args.reference_date,
                dry_run=not args.apply,
                aggregate_before_delete=not args.skip_aggregate,
                generate_weekly=args.generate_weekly,
                min_measured_days=args.min_measured_days,
            )
    except psycopg.Error as exc:
        print(
            f"PostgreSQL 배치 실행 실패 ({type(exc).__name__}). "
            "sql/003_daily_weekly_retention.sql 및 sql/004_daily_summary_pruned_lock.sql 적용 여부와 DB 접속 설정을 확인하세요.",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_batch_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
