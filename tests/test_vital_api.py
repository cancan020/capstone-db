import copy
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

from fastapi.testclient import TestClient

from postgres_db import psycopg
from vital_api import app

EVENT = {
    "packet_type": "DETAILED_EVENT_LOG", "timestamp": "2026-11-07 16:48:33",
    "status_level": "WARNING", "event_type": "BRADYPNEXIA_LOW",
    "duration_sec": 10.1, "breath_rate": 9.7, "heart_rate": 61.6,
    "breath_deviation": 0.15828,
    "location": {"x": -0.03, "y": 1.19, "z": -0.17},
}
SUMMARY = {
    "packet_type": "PERIODIC_SUMMARY_STATS", "timestamp": "2026-11-07 16:48:59",
    "status_level": "NORMAL", "period_seconds": 60, "avg_breath_rate": 12.2,
    "min_breath_rate": 10.0, "max_breath_rate": 16.7,
    "avg_heart_rate": 66.7, "active_time_sec": 3.5,
}


class FakeConnection:
    def __init__(self, commit_error=None, rows=None):
        self.query = None
        self.parameters = None
        self.committed = False
        self.commit_error = commit_error
        self.rows = iter(rows) if rows is not None else None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.commit_error is not None:
            raise self.commit_error
        self.committed = exc_type is None
        return False

    def execute(self, query, parameters):
        self.query = query
        self.parameters = parameters
        return self

    def fetchone(self):
        return next(self.rows) if self.rows is not None else (101,)


class VitalAPITests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"VITAL_DEFAULT_SUBJECT_ID": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_event_maps_all_fields_and_commits(self):
        connection = FakeConnection()
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.post("/api/v1/vital-events", json=EVENT)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"saved": True, "record_id": 101, "subject_id": 1})
        self.assertTrue(connection.committed)
        self.assertIn("public.vital_event_logs", connection.query)
        self.assertEqual(connection.parameters, (
            1, datetime(2026, 11, 7, 7, 48, 33, tzinfo=timezone.utc),
            "WARNING", "BRADYPNEXIA_LOW", Decimal("10.1"), Decimal("9.7"),
            Decimal("61.6"), Decimal("0.15828"), Decimal("-0.03"),
            Decimal("1.19"), Decimal("-0.17"),
        ))

    def test_summary_maps_all_fields_and_commits(self):
        connection = FakeConnection()
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.post("/api/v1/vital-summary", json=SUMMARY)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(connection.committed)
        self.assertIn("public.vital_summaries", connection.query)
        self.assertEqual(connection.parameters, (
            1, datetime(2026, 11, 7, 7, 48, 59, tzinfo=timezone.utc),
            "NORMAL", 60, Decimal("12.2"), Decimal("10.0"), Decimal("16.7"),
            Decimal("66.7"), Decimal("3.5"), None,
        ))

    def test_radar_summary_with_valid_sample_count_maps_and_commits(self):
        packet = {**SUMMARY, "valid_sample_count": 520}
        connection = FakeConnection()
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.post("/api/v1/vital-summary", json=packet)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(connection.committed)
        self.assertIn("valid_sample_count", connection.query)
        self.assertEqual(connection.parameters[-1], 520)

    def test_summary_allows_zero_valid_samples_when_breathing_stats_are_null(self):
        packet = {
            **SUMMARY,
            "avg_breath_rate": None,
            "min_breath_rate": None,
            "max_breath_rate": None,
            "valid_sample_count": 0,
            "avg_heart_rate": None,
        }
        connection = FakeConnection()
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.post("/api/v1/vital-summary", json=packet)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(connection.parameters[4:10], (None, None, None, None, Decimal("3.5"), 0))

    def test_offset_timestamp_preserves_instant(self):
        packet = {**EVENT, "timestamp": "2026-11-07T07:48:33Z"}
        connection = FakeConnection()
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.post("/api/v1/vital-events", json=packet)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(connection.parameters[1].hour, 7)

    def test_explicit_subject_overrides_demo_mapping(self):
        connection = FakeConnection()
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.post("/api/v1/vital-events", json={**EVENT, "subject_id": 42})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(connection.parameters[0], 42)

    def test_no_implicit_subject_without_demo_setting(self):
        os.environ.pop("VITAL_DEFAULT_SUBJECT_ID", None)
        with patch("vital_api.connect_db") as connect:
            response = self.client.post("/api/v1/vital-events", json=EVENT)
        self.assertEqual(response.status_code, 422)
        connect.assert_not_called()

    def test_invalid_event_never_connects_to_database(self):
        changes = [
            {"breath_rate": -1}, {"breath_rate": "NaN"},
            {"timestamp": 123456}, {"timestamp": "invalid"},
            {"packet_type": "PERIODIC_SUMMARY_STATS"},
            {"subject_id": True}, {"unknown_field": 1},
        ]
        for change in changes:
            with self.subTest(change=change), patch("vital_api.connect_db") as connect:
                response = self.client.post("/api/v1/vital-events", json={**EVENT, **change})
                self.assertEqual(response.status_code, 422)
                connect.assert_not_called()

    def test_invalid_summary_never_connects_to_database(self):
        changes = [
            {"period_seconds": 0}, {"period_seconds": 60.5},
            {"active_time_sec": 61}, {"min_breath_rate": 20},
            {"avg_breath_rate": 30}, {"packet_type": "DETAILED_EVENT_LOG"},
            {"valid_sample_count": -1}, {"valid_sample_count": 12.5},
            {"valid_sample_count": True}, {"valid_sample_count": 0},
        ]
        for change in changes:
            with self.subTest(change=change), patch("vital_api.connect_db") as connect:
                response = self.client.post("/api/v1/vital-summary", json={**SUMMARY, **change})
                self.assertEqual(response.status_code, 422)
                connect.assert_not_called()

    def test_null_measurement_and_zero_remain_different(self):
        packet = {**EVENT, "breath_rate": None, "heart_rate": 0, "location": None}
        connection = FakeConnection()
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.post("/api/v1/vital-events", json=packet)
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(connection.parameters[5])
        self.assertEqual(connection.parameters[6], Decimal(0))
        self.assertEqual(connection.parameters[8:], (None, None, None))

    def test_unknown_subject_reports_validation_error(self):
        with patch("vital_api.connect_db", side_effect=psycopg.errors.ForeignKeyViolation()):
            response = self.client.post("/api/v1/vital-events", json=EVENT)
        self.assertEqual(response.status_code, 422)

    def test_database_outage_does_not_report_saved(self):
        with patch("vital_api.connect_db", side_effect=psycopg.OperationalError()):
            response = self.client.post("/api/v1/vital-events", json=EVENT)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("saved", response.json())

    def test_commit_failure_does_not_report_saved(self):
        connection = FakeConnection(commit_error=psycopg.OperationalError())
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.post("/api/v1/vital-summary", json=SUMMARY)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("saved", response.json())

    def test_event_type_is_bound_as_a_parameter(self):
        packet = copy.deepcopy(EVENT)
        packet["event_type"] = "quote'; SELECT 1; --"
        connection = FakeConnection()
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.post("/api/v1/vital-events", json=packet)
        self.assertEqual(response.status_code, 201)
        self.assertNotIn(packet["event_type"], connection.query)
        self.assertEqual(connection.parameters[3], packet["event_type"])

    def test_identified_replay_returns_original_record_on_each_route(self):
        for path, payload in [("vital-events", EVENT), ("vital-summary", SUMMARY)]:
            with self.subTest(path=path):
                packet = {**payload, "source_id": "demo-radar", "message_id": "m-001"}
                first = FakeConnection()
                with patch("vital_api.connect_db", return_value=first):
                    created = self.client.post(f"/api/v1/{path}", json=packet)
                self.assertEqual(created.status_code, 201)
                self.assertIn("ON CONFLICT (source_id, message_id) DO NOTHING", first.query)
                self.assertEqual(first.parameters[-2:], ("demo-radar", "m-001"))

                replay = FakeConnection(rows=[None, (101, *first.parameters[:-2])])
                with patch("vital_api.connect_db", return_value=replay):
                    repeated = self.client.post(f"/api/v1/{path}", json=packet)
                self.assertEqual(repeated.status_code, 201)
                self.assertEqual(repeated.json(), created.json())
                self.assertTrue(replay.committed)

    def test_reused_identifier_with_changed_packet_is_rejected(self):
        packet = {**EVENT, "source_id": "demo-radar", "message_id": "m-001"}
        first = FakeConnection()
        with patch("vital_api.connect_db", return_value=first):
            self.client.post("/api/v1/vital-events", json=packet)
        replay = FakeConnection(rows=[None, (101, *first.parameters[:-2])])
        with patch("vital_api.connect_db", return_value=replay):
            response = self.client.post("/api/v1/vital-events", json={**packet, "breath_rate": 16})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(replay.committed)

    def test_reused_summary_identifier_with_changed_valid_sample_count_is_rejected(self):
        packet = {**SUMMARY, "valid_sample_count": 500, "source_id": "demo-radar", "message_id": "s-001"}
        first = FakeConnection()
        with patch("vital_api.connect_db", return_value=first):
            self.client.post("/api/v1/vital-summary", json=packet)
        replay = FakeConnection(rows=[None, (101, *first.parameters[:-2])])
        with patch("vital_api.connect_db", return_value=replay):
            response = self.client.post("/api/v1/vital-summary", json={**packet, "valid_sample_count": 501})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(replay.committed)

    def test_incomplete_identifier_pair_is_rejected_before_storage(self):
        for fields in [{"source_id": "demo-radar"}, {"message_id": "m-001"}]:
            with self.subTest(fields=fields), patch("vital_api.connect_db") as connect:
                response = self.client.post("/api/v1/vital-events", json={**EVENT, **fields})
                self.assertEqual(response.status_code, 422)
                connect.assert_not_called()

    def test_breathing_report_returns_the_demo_comparison(self):
        start = date(2026, 10, 26)
        rows = [
            (start + timedelta(days=i), 1, 1, Decimal(15 if i < 7 else 17))
            for i in range(14)
        ]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.side_effect = [Mock(fetchone=Mock(return_value=(1,))), Mock(fetchall=Mock(return_value=rows))]
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.get("/api/v1/breathing-report")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["subject_id"], 1)
        self.assertEqual(payload["report_date"], "2026-11-09")
        self.assertEqual(len(payload["daily"]), 14)
        self.assertEqual(payload["previous_week"]["average_bpm"], 15.0)
        self.assertEqual(payload["recent_week"]["average_bpm"], 17.0)
        self.assertEqual(payload["comparison"], {"status": "ready", "average_change_bpm": 2.0})
        self.assertEqual(connection.execute.call_args.args[1], (date(2026, 11, 9), 1))

    def test_breathing_report_preserves_missing_days_for_requested_subject_and_date(self):
        report_date = date(2026, 11, 10)
        rows = [(report_date - timedelta(days=i), 0, 0, None) for i in range(14, 0, -1)]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.side_effect = [Mock(fetchone=Mock(return_value=(1,))), Mock(fetchall=Mock(return_value=rows))]
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.get("/api/v1/breathing-report", params={"subject_id": 2, "report_date": "2026-11-10"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["daily"]), 14)
        self.assertEqual(payload["daily"][0]["date"], "2026-10-27")
        self.assertEqual(payload["daily"][0]["status"], "no_record")
        self.assertIsNone(payload["daily"][0]["average_bpm"])
        self.assertEqual(payload["comparison"], {"status": "insufficient_observations", "average_change_bpm": None})
        self.assertEqual(connection.execute.call_args.args[1], (report_date, 2))

    def test_breathing_report_returns_404_for_unregistered_subject(self):
        connection = FakeConnection(rows=[None])
        with patch("vital_api.connect_db", return_value=connection):
            response = self.client.get("/api/v1/breathing-report", params={"subject_id": 999})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
