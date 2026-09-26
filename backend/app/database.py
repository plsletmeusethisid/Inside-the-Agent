"""Explicit, transactional schema initialization and versioned migrations."""

from pathlib import Path

import psycopg

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def apply_schema(connection: psycopg.Connection) -> None:
    # Serialize startup across API workers; the caller commits all DDL together.
    connection.execute("SELECT pg_advisory_xact_lock(71023401)")
    connection.execute((SQL_DIR / "init.sql").read_text(encoding="utf-8"))
    connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    for migration in sorted((SQL_DIR / "migrations").glob("*.sql")):
        if connection.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (migration.name,)).fetchone():
            continue
        connection.execute(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (migration.name,))
