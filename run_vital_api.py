"""Start the local demo after checking the existing PostgreSQL connection."""

import getpass
import os

import uvicorn

from postgres_db import connect_db, psycopg


def main():
    # Explicit demo mapping for the user's registered test subject.
    os.environ.setdefault("VITAL_DEFAULT_SUBJECT_ID", "1")
    print("DB: {}:{} / {} / user={}".format(
        os.getenv("PGHOST", "127.0.0.1"), os.getenv("PGPORT", "5432"),
        os.getenv("PGDATABASE", "capstone"), os.getenv("PGUSER", "postgres")))
    print("Demo subject_id:", os.environ["VITAL_DEFAULT_SUBJECT_ID"])
    print("Timestamps without an offset are interpreted as Korean time (+09:00).")
    if "PGPASSWORD" not in os.environ:
        os.environ["PGPASSWORD"] = getpass.getpass("PostgreSQL password (hidden): ")
    try:
        subject_id = int(os.environ["VITAL_DEFAULT_SUBJECT_ID"])
        with connect_db() as connection:
            row = connection.execute(
                "SELECT display_name FROM public.subjects WHERE subject_id = %s",
                (subject_id,),
            ).fetchone()
            if row is None:
                print("The demo subject_id is not registered in subjects.")
                return 1
            for table in ("public.vital_event_logs", "public.vital_summaries"):
                if connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
                    print("Required table is missing:", table)
                    return 1
            required_columns = [
                ("vital_event_logs", "source_id", "sql/001_message_dedup.sql"),
                ("vital_event_logs", "message_id", "sql/001_message_dedup.sql"),
                ("vital_summaries", "source_id", "sql/001_message_dedup.sql"),
                ("vital_summaries", "message_id", "sql/001_message_dedup.sql"),
                ("vital_summaries", "valid_sample_count", "sql/002_summary_valid_sample_count.sql"),
            ]
            for table_name, column_name, migration_file in required_columns:
                exists = connection.execute(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
                    """,
                    (table_name, column_name),
                ).fetchone()
                if exists is None:
                    print(f"Required column is missing: public.{table_name}.{column_name}")
                    print(f"Apply {migration_file} in pgAdmin and restart.")
                    return 1
            for optional_table in ("public.daily_summaries", "public.weekly_reports"):
                if connection.execute("SELECT to_regclass(%s)", (optional_table,)).fetchone()[0] is None:
                    print(f"Notice: {optional_table} is not created yet. Apply sql/003_daily_weekly_retention.sql for daily rollup & retention.")
            if connection.execute("SELECT to_regclass('public.daily_summaries')").fetchone()[0] is not None:
                pruned_col = connection.execute(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'daily_summaries' AND column_name = 'detail_pruned_at'
                    """
                ).fetchone()
                if pruned_col is None:
                    print("Notice: public.daily_summaries.detail_pruned_at is missing. Apply sql/004_daily_summary_pruned_lock.sql.")
    except ValueError:
        print("VITAL_DEFAULT_SUBJECT_ID must be a positive integer.")
        return 1
    except psycopg.Error as exc:
        # Do not print connection strings or passwords.
        print("Database check failed:", type(exc).__name__)
        print("Check the password and PGHOST/PGPORT/PGDATABASE/PGUSER settings.")
        return 1
    print("Database check passed. Open http://127.0.0.1:8000/docs")
    uvicorn.run("vital_api:app", host="127.0.0.1", port=8000)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
