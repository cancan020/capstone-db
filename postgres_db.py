"""Shared PostgreSQL connection settings for the local capstone tools."""

import os
from pathlib import Path


def _configure_windows_libpq() -> None:
    """Use the PostgreSQL 18 client's libpq when it is installed locally."""
    if os.name != "nt" or os.getenv("PSYCOPG_IMPL", "python") != "python":
        return
    program_files = Path(os.getenv("ProgramFiles", r"C:\Program Files"))
    pg_bin = program_files / "PostgreSQL" / "18" / "bin"
    if (pg_bin / "libpq.dll").is_file():
        # Psycopg's Windows Python implementation discovers libpq through PATH.
        os.environ["PATH"] = str(pg_bin) + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("PSYCOPG_IMPL", "python")


_configure_windows_libpq()

# Psycopg selects its implementation during import.
import psycopg


def connect_db() -> psycopg.Connection:
    """Use libpq environment variables; never put credentials in source code."""
    return psycopg.connect(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "capstone"),
        user=os.getenv("PGUSER", "postgres"),
        connect_timeout=5,
        # PGPASSWORD or a local pgpass file supplies the password.
    )
