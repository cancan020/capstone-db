import json
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

from fastapi.testclient import TestClient

import retention_batch
import summary_report
from postgres_db import psycopg
from vital_api import app


def make_day(
    day: date,
    *,
    summary_count: int = 10,
    measured_seconds: int = 3600,
    valid_sample_count: int = 100,
    avg_bpm: Decimal | None = Decimal("15.0"),
    weight: Decimal | None = None,
    weighting_method: str = "valid_sample_count",
    min_bpm: Decimal | None = Decimal("13.0"),
    max_bpm: Decimal | None = Decimal("17.0"),
    active_time_sec: Decimal | None = Decimal("3600"),
    warning_dawn: int = 0,
    warning_day: int = 0,
    warning_night: int = 0,
    emergency: int = 0,
    row_source: str = "live",
) -> summary_report.DailySummaryRow:
    eff_weight = Decimal(valid_sample_count) if weight is None else weight
    if avg_bpm is None:
        weighted_sum = None
        eff_weight = Decimal(0)
        weighting_method = "none"
    else:
        weighted_sum = avg_bpm * eff_weight
    return summary_report.DailySummaryRow(
        summary_date=day,
        summary_count=summary_count,
        measured_seconds=measured_seconds if avg_bpm is not None else 0,
        unmeasured_seconds=86400 - (measured_seconds if avg_bpm is not None else 0),
        valid_sample_count=valid_sample_count if avg_bpm is not None else 0,
        breath_rate_weighted_sum=weighted_sum,
        breath_rate_weight=eff_weight,
        weighting_method=weighting_method,
        min_breath_rate=min_bpm if avg_bpm is not None else None,
        max_breath_rate=max_bpm if avg_bpm is not None else None,
        active_time_sec=active_time_sec,
        warning_log_count=warning_dawn + warning_day + warning_night,
        emergency_log_count=emergency,
        warning_dawn_count=warning_dawn,
        warning_day_count=warning_day,
        warning_night_count=warning_night,
        row_source=row_source,
    )


class SummaryReportUnitTests(unittest.TestCase):
    def test_weekly_average_uses_sample_weights_not_simple_daily_mean(self):
        report_date = date(2026, 11, 23)
        start = report_date - timedelta(days=14)
        days = []
        for i in range(7):
            day = start + timedelta(days=i)
            if i < 6:
                days.append(make_day(day, valid_sample_count=100, avg_bpm=Decimal("14.0")))
            else:
                days.append(make_day(day, valid_sample_count=600, avg_bpm=Decimal("18.0")))
        for i in range(7, 14):
            day = start + timedelta(days=i)
            days.append(
                make_day(
                    day,
                    valid_sample_count=200,
                    avg_bpm=Decimal("17.5"),
                    active_time_sec=Decimal("2700"),
                    warning_dawn=1 if i in (7, 9, 11) else 0,
                    emergency=1 if i == 12 else 0,
                )
            )

        report = summary_report.build_summary_report(1, report_date, days)
        self.assertEqual(report["previous_week"]["average_bpm"], 16.0)
        self.assertEqual(report["previous_week"]["valid_sample_count"], 1200)
        self.assertEqual(report["previous_week"]["weighting_method"], "valid_sample_count")
        self.assertEqual(report["recent_week"]["average_bpm"], 17.5)
        self.assertEqual(report["comparison"]["status"], "ready")
        self.assertTrue(report["comparison"]["is_complete_14_days"])
        self.assertEqual(report["comparison"]["average_change_bpm"], 1.5)
        self.assertEqual(report["comparison"]["activity_change_pct"], -25.0)
        self.assertEqual(report["recent_week"]["warning_logs_by_time"], {"dawn": 3, "day": 0, "night": 0})
        self.assertEqual(report["recent_week"]["emergency_log_count"], 1)

    def test_insufficient_observations_blocks_comparison_and_preserves_unmeasured_time(self):
        report_date = date(2026, 11, 23)
        start = report_date - timedelta(days=14)
        days = [
            make_day(
                start + timedelta(days=i),
                summary_count=0 if i < 4 else 5,
                avg_bpm=None if i < 4 else Decimal("16.0"),
                active_time_sec=None if i < 4 else Decimal("3600"),
                row_source="none" if i < 4 else "live",
            )
            for i in range(14)
        ]
        report = summary_report.build_summary_report(1, report_date, days)
        self.assertEqual(report["previous_week"]["measured_days"], 3)
        self.assertEqual(report["comparison"]["status"], "insufficient_observations")
        self.assertFalse(report["comparison"]["is_complete_14_days"])
        self.assertIsNone(report["comparison"]["average_change_bpm"])
        self.assertEqual(report["daily"][0]["status"], "no_record")
        self.assertEqual(report["daily"][0]["unmeasured_seconds"], 86400)

        partial = summary_report.build_summary_report(1, report_date, days, min_measured_days=3)
        self.assertEqual(partial["comparison"]["status"], "ready")
        self.assertFalse(partial["comparison"]["is_complete_14_days"])
        self.assertEqual(partial["comparison"]["average_change_bpm"], 0.0)

    def test_auto_source_falls_back_to_archived_daily_summaries_when_live_pruned(self):
        report_date = date(2026, 11, 23)
        start = report_date - timedelta(days=14)
        live_rows = [
            tuple(make_day(start + timedelta(days=i), summary_count=0, avg_bpm=None, active_time_sec=None, row_source="none"))
            if i < 7
            else tuple(make_day(start + timedelta(days=i), avg_bpm=Decimal("17.0"), row_source="live"))
            for i in range(14)
        ]
        archived_rows = [
            tuple(make_day(start + timedelta(days=i), avg_bpm=Decimal("15.0"), row_source="daily_summaries_finalized"))
            if i < 7
            else tuple(make_day(start + timedelta(days=i), summary_count=0, avg_bpm=None, active_time_sec=None, row_source="none"))
            for i in range(14)
        ]
        connection = MagicMock()
        connection.execute.side_effect = [
            Mock(fetchall=Mock(return_value=live_rows)),
            Mock(fetchone=Mock(return_value=("public.daily_summaries",))),
            Mock(fetchall=Mock(return_value=archived_rows)),
        ]
        merged = summary_report.load_daily_summary_rows(connection, 1, report_date, source="auto")
        self.assertEqual(merged[0].row_source, "daily_summaries_finalized")
        self.assertEqual(merged[7].row_source, "live")
        report = summary_report.build_summary_report(1, report_date, merged, source="auto")
        self.assertEqual(report["previous_week"]["average_bpm"], 15.0)
        self.assertEqual(report["recent_week"]["average_bpm"], 17.0)
        self.assertEqual(report["comparison"]["average_change_bpm"], 2.0)

    def test_auto_source_preserves_finalized_archive_even_when_late_packet_arrives(self):
        report_date = date(2026, 11, 23)
        start = report_date - timedelta(days=14)
        live_rows = [
            tuple(make_day(start, summary_count=1, valid_sample_count=5, avg_bpm=Decimal("24.0"), row_source="live"))
        ] + [
            tuple(make_day(start + timedelta(days=i), avg_bpm=Decimal("15.0"), row_source="live"))
            for i in range(1, 14)
        ]
        archived_rows = [
            tuple(make_day(start, summary_count=24, valid_sample_count=100, avg_bpm=Decimal("14.0"), row_source="daily_summaries_finalized"))
        ] + [
            tuple(make_day(start + timedelta(days=i), summary_count=0, avg_bpm=None, active_time_sec=None, row_source="none"))
            for i in range(1, 14)
        ]
        connection = MagicMock()
        connection.execute.side_effect = [
            Mock(fetchall=Mock(return_value=live_rows)),
            Mock(fetchone=Mock(return_value=("public.daily_summaries",))),
            Mock(fetchall=Mock(return_value=archived_rows)),
        ]
        merged = summary_report.load_daily_summary_rows(connection, 1, report_date, source="auto")
        self.assertEqual(merged[0].row_source, "daily_summaries_finalized")
        self.assertEqual(merged[0].valid_sample_count, 100)
        self.assertEqual(merged[0].breath_rate_weighted_sum / merged[0].breath_rate_weight, Decimal("14.0"))

    def test_aggregate_returns_exact_ids_and_locks_finalized_rows(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            (1, date(2026, 11, 22), [10, 11], [20]),
        ]
        rolled = summary_report.aggregate_daily_summaries(
            connection, before_date=date(2026, 11, 23), start_date=date(2026, 11, 9), subject_id=1
        )
        self.assertEqual(
            rolled,
            [summary_report.AggregatedDay(1, date(2026, 11, 22), (10, 11), (20,))],
        )
        agg_sql = connection.execute.call_args.args[0]
        self.assertIn("ARRAY_AGG(s.summary_id ORDER BY s.summary_id)", agg_sql)
        self.assertIn("ARRAY_AGG(l.log_id ORDER BY l.log_id)", agg_sql)
        self.assertIn("ON CONFLICT (subject_id, summary_date, calc_version) DO UPDATE", agg_sql)
        self.assertIn("WHERE ds.detail_pruned_at IS NULL", agg_sql)

        report_date = date(2026, 11, 23)
        start = report_date - timedelta(days=14)
        days = [make_day(start + timedelta(days=i)) for i in range(14)]
        report = summary_report.build_summary_report(1, report_date, days)
        connection.execute.reset_mock()
        connection.execute.return_value.fetchone.return_value = (42,)
        report_id = summary_report.save_weekly_report(connection, report)
        self.assertEqual(report_id, 42)
        save_sql, save_params = connection.execute.call_args.args
        self.assertIn("ON CONFLICT (subject_id, report_date, calc_version) DO UPDATE", save_sql)
        self.assertEqual(json.loads(save_params[-1])["subject_id"], 1)


class RetentionBatchTests(unittest.TestCase):
    def test_cutoffs_use_exact_kst_midnight_converted_to_utc(self):
        cutoffs = retention_batch.retention_cutoffs(date(2026, 11, 25))
        self.assertEqual(cutoffs["detail_cutoff_date"], date(2026, 11, 11))
        self.assertEqual(
            cutoffs["detail_cutoff_ts"],
            datetime(2026, 11, 10, 15, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(cutoffs["daily_summary_cutoff_date"], date(2025, 11, 25))
        self.assertEqual(
            cutoffs["weekly_report_cutoff_ts"],
            datetime(2025, 11, 24, 15, 0, 0, tzinfo=timezone.utc),
        )

    def test_idle_connection_is_pinned_to_repeatable_read(self):
        connection = MagicMock()
        connection.info.transaction_status = psycopg.pq.TransactionStatus.IDLE
        connection.execute.side_effect = [
            Mock(fetchone=Mock(return_value=(0,))),
            Mock(fetchone=Mock(return_value=(0,))),
            Mock(fetchone=Mock(return_value=(0,))),
            Mock(fetchone=Mock(return_value=(0,))),
            Mock(fetchone=Mock(return_value=(0,))),
            Mock(fetchone=Mock(return_value=(0,))),
            Mock(fetchone=Mock(return_value=(0,))),
            Mock(fetchone=Mock(return_value=(0,))),
        ]
        retention_batch.run_retention_batch(connection, date(2026, 11, 25), dry_run=True)
        self.assertEqual(connection.isolation_level, psycopg.IsolationLevel.REPEATABLE_READ)

    def test_dry_run_never_executes_write_and_separates_held_back_rows(self):
        connection = MagicMock()
        connection.execute.side_effect = [
            Mock(fetchone=Mock(return_value=(1,))),    # late vital_summaries on pruned days
            Mock(fetchone=Mock(return_value=(0,))),    # late vital_event_logs on pruned days
            Mock(fetchone=Mock(return_value=(100,))),  # eligible vital_summaries
            Mock(fetchone=Mock(return_value=(120,))),  # total expired vital_summaries -> 20 held back
            Mock(fetchone=Mock(return_value=(10,))),   # eligible vital_event_logs
            Mock(fetchone=Mock(return_value=(15,))),   # total expired vital_event_logs -> 5 held back
            Mock(fetchone=Mock(return_value=(3,))),    # expired daily_summaries
            Mock(fetchone=Mock(return_value=(1,))),    # expired weekly_reports
        ]
        result = retention_batch.run_retention_batch(
            connection, date(2026, 11, 25), dry_run=True, aggregate_before_delete=True, generate_weekly=True
        )
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(
            result["expired_counts"],
            {"vital_summaries": 100, "vital_event_logs": 10, "daily_summaries": 3, "weekly_reports": 1},
        )
        self.assertEqual(result["held_back_counts"], {"vital_summaries": 20, "vital_event_logs": 5})
        self.assertEqual(result["late_packets_on_pruned_days"], {"vital_summaries": 1, "vital_event_logs": 0})
        for call in connection.execute.call_args_list:
            sql = call.args[0].upper()
            self.assertNotIn("DELETE", sql)
            self.assertNotIn("INSERT", sql)
            self.assertNotIn("UPDATE", sql)

    def test_apply_scopes_detail_deletion_strictly_to_aggregated_ids_and_holds_back_concurrent_inserts(self):
        connection = MagicMock()
        delete_calls: list[tuple[str, tuple]] = []

        def fake_execute(sql, params=None):
            if "DELETE FROM public.vital_summaries" in sql:
                delete_calls.append((sql, params))
                # Only aggregated summary_id=101 is deleted; concurrently inserted id=102 stays behind.
                return Mock(fetchall=Mock(return_value=[(1, date(2026, 11, 10))]))
            if "SELECT COUNT(*) FROM public.vital_summaries" in sql:
                # The concurrently inserted 50-sample row remains intact -> held_back = 1
                return Mock(fetchone=Mock(return_value=(1,)))
            if "DELETE FROM public.vital_event_logs" in sql:
                delete_calls.append((sql, params))
                return Mock(fetchall=Mock(return_value=[(1, date(2026, 11, 10))]))
            if "SELECT COUNT(*) FROM public.vital_event_logs" in sql:
                return Mock(fetchone=Mock(return_value=(0,)))
            if "UPDATE public.daily_summaries AS ds" in sql:
                return Mock(fetchone=Mock(return_value=(1,)))
            if "DELETE FROM public.daily_summaries" in sql or "DELETE FROM public.weekly_reports" in sql:
                return Mock(fetchone=Mock(return_value=(0,)))
            raise AssertionError(f"Unexpected SQL: {sql}")

        connection.execute.side_effect = fake_execute
        with (
            patch.object(
                retention_batch,
                "count_late_packets_on_pruned_days",
                return_value={"vital_summaries": 0, "vital_event_logs": 0},
            ),
            patch.object(
                retention_batch,
                "aggregate_daily_summaries",
                return_value=[summary_report.AggregatedDay(1, date(2026, 11, 10), (101,), (201,))],
            ),
        ):
            result = retention_batch.run_retention_batch(
                connection, date(2026, 11, 25), dry_run=False, aggregate_before_delete=True, generate_weekly=False
            )

        self.assertEqual(len(delete_calls), 2)
        summary_sql, summary_params = delete_calls[0]
        event_sql, event_params = delete_calls[1]
        self.assertIn("WHERE t.summary_id = ANY(%s::bigint[])", summary_sql)
        self.assertIn("FROM public.weekly_reports AS wr", summary_sql)
        self.assertIn("AND ds.detail_pruned_at IS NULL", summary_sql)
        self.assertEqual(summary_params[0], [101])
        self.assertIn("WHERE t.log_id = ANY(%s::bigint[])", event_sql)
        self.assertEqual(event_params[0], [201])
        self.assertEqual(result["deleted_counts"]["vital_summaries"], 1)
        self.assertEqual(result["held_back_counts"]["vital_summaries"], 1)
        self.assertEqual(result["finalized_daily_rows"], 1)

    def test_skip_aggregate_never_deletes_detail_rows(self):
        connection = MagicMock()
        connection.execute.side_effect = [
            Mock(fetchone=Mock(return_value=(0,))),  # late summaries
            Mock(fetchone=Mock(return_value=(0,))),  # late events
            Mock(fetchone=Mock(return_value=(9,))),  # held-back vital_summaries
            Mock(fetchone=Mock(return_value=(2,))),  # held-back vital_event_logs
            Mock(fetchone=Mock(return_value=(0,))),  # 365d daily_summaries delete
            Mock(fetchone=Mock(return_value=(0,))),  # 365d weekly_reports delete
        ]
        result = retention_batch.run_retention_batch(
            connection, date(2026, 11, 25), dry_run=False, aggregate_before_delete=False, generate_weekly=False
        )
        self.assertEqual(result["deleted_counts"]["vital_summaries"], 0)
        self.assertEqual(result["deleted_counts"]["vital_event_logs"], 0)
        self.assertEqual(result["held_back_counts"], {"vital_summaries": 9, "vital_event_logs": 2})
        for call in connection.execute.call_args_list:
            self.assertNotIn("DELETE FROM public.vital_", call.args[0])

    def test_apply_rolls_up_daily_and_weekly_deletes_both_detail_tables_then_marks_pruned(self):
        connection = MagicMock()
        report_date = date(2026, 11, 23)
        start = report_date - timedelta(days=14)
        sample_days = [make_day(start + timedelta(days=i)) for i in range(14)]
        executed_steps: list[str] = []

        def fake_late_count(conn, **kwargs):
            executed_steps.append("check_late")
            return {"vital_summaries": 1, "vital_event_logs": 0}

        def fake_aggregate(conn, **kwargs):
            executed_steps.append("aggregate")
            return [summary_report.AggregatedDay(1, date(2026, 11, 8), (11, 12), (21,))]

        def fake_load_report(conn, subject_id, rep_date, **kwargs):
            executed_steps.append(f"load_report:{subject_id}")
            return summary_report.build_summary_report(subject_id, rep_date, sample_days)

        def fake_save_report(conn, report):
            executed_steps.append(f"save_report:{report['subject_id']}")
            return 7

        def fake_execute(sql, params=None):
            if "FROM public.subjects" in sql:
                return Mock(fetchall=Mock(return_value=[(1,)]))
            if "DELETE FROM public.vital_" in sql:
                table = sql.split("DELETE FROM ")[1].split(" AS t")[0]
                executed_steps.append("delete_pk_scoped:" + table)
                return Mock(fetchall=Mock(return_value=[(1, date(2026, 11, 8))]))
            if "SELECT COUNT(*) FROM public.vital_" in sql:
                return Mock(fetchone=Mock(return_value=(0,)))
            if "UPDATE public.daily_summaries AS ds" in sql:
                executed_steps.append("mark_pruned")
                return Mock(fetchone=Mock(return_value=(1,)))
            if "DELETE FROM public." in sql:
                table = sql.split("DELETE FROM ")[1].split(" WHERE")[0]
                executed_steps.append("delete:" + table)
                return Mock(fetchone=Mock(return_value=(2,)))
            raise AssertionError(f"Unexpected SQL: {sql}")

        connection.execute.side_effect = fake_execute
        with (
            patch.object(retention_batch, "count_late_packets_on_pruned_days", side_effect=fake_late_count),
            patch.object(retention_batch, "aggregate_daily_summaries", side_effect=fake_aggregate),
            patch.object(retention_batch, "load_summary_report", side_effect=fake_load_report),
            patch.object(retention_batch, "save_weekly_report", side_effect=fake_save_report),
        ):
            result = retention_batch.run_retention_batch(
                connection, report_date, dry_run=False, aggregate_before_delete=True, generate_weekly=True
            )

        self.assertEqual(result["mode"], "applied")
        self.assertEqual(result["aggregated_daily_rows"], 1)
        self.assertEqual(result["generated_weekly_reports"], 1)
        self.assertEqual(result["finalized_daily_rows"], 1)
        self.assertEqual(
            executed_steps,
            [
                "check_late",
                "aggregate",
                "load_report:1",
                "save_report:1",
                "delete_pk_scoped:public.vital_summaries",
                "delete_pk_scoped:public.vital_event_logs",
                "mark_pruned",
                "delete:public.daily_summaries",
                "delete:public.weekly_reports",
            ],
        )
        formatted = retention_batch.format_batch_result(result)
        self.assertIn("지연 도착한 패킷이 감지되었습니다", formatted)


class SummaryAndWeeklyEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_summary_report_endpoint_returns_weighted_comparison(self):
        report_date = date(2026, 11, 23)
        start = report_date - timedelta(days=14)
        rows = [
            tuple(make_day(start + timedelta(days=i), avg_bpm=Decimal("15.0") if i < 7 else Decimal("16.5")))
            for i in range(14)
        ]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.side_effect = [
            Mock(fetchone=Mock(return_value=(1,))),
            Mock(fetchall=Mock(return_value=rows)),
        ]
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.get(
                "/api/v1/summary-report",
                params={"subject_id": 1, "report_date": "2026-11-23", "source": "live"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["previous_week"]["average_bpm"], 15.0)
        self.assertEqual(payload["recent_week"]["average_bpm"], 16.5)
        self.assertEqual(payload["comparison"]["average_change_bpm"], 1.5)

    def test_summary_report_endpoint_404_and_validation(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = None
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.get("/api/v1/summary-report", params={"subject_id": 999})
        self.assertEqual(response.status_code, 404)

        bad = self.client.get("/api/v1/summary-report", params={"min_measured_days": 8})
        self.assertEqual(bad.status_code, 422)

    def test_weekly_reports_endpoint_lists_saved_reports_and_handles_outage(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        saved_at = datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)
        connection.execute.side_effect = [
            Mock(fetchone=Mock(return_value=(1,))),
            Mock(fetchall=Mock(return_value=[(9, date(2026, 11, 23), "v1", "ready", saved_at, {"subject_id": 1})])),
        ]
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.get("/api/v1/weekly-reports", params={"subject_id": 1, "limit": 4})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["report_id"], "9")
        self.assertEqual(body[0]["comparison_status"], "ready")

        with patch("vital_api.connect_db", side_effect=psycopg.OperationalError()):
            outage = self.client.get("/api/v1/weekly-reports", params={"subject_id": 1})
        self.assertEqual(outage.status_code, 503)


if __name__ == "__main__":
    unittest.main()
