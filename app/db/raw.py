"""Plain-SQL access for the tools that read the database without the ORM (backup exports, analysis).

They were written against ``sqlite3``. On Postgres (DATABASE_URL, since 2026-11-18) they get a connection
from ``app.sqlpg``, which keeps sqlite3's shape (``?`` placeholders, rows by name), in a read-only
transaction. Given a SQLite ``db_path`` they still open that file read-only, as before.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app import config, sqlpg


def using_postgres(db_path: Path | None = None) -> bool:
    return db_path is None and bool(os.environ.get("DATABASE_URL"))


@contextmanager
def connect_readonly(db_path: Path | None = None) -> Iterator:
    if using_postgres(db_path):
        with sqlpg.connect(os.environ["DATABASE_URL"], max_size=2) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            yield conn
        return
    db = db_path or config.DB_PATH
    if not Path(db).exists():
        raise FileNotFoundError(f"no database at {db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def columns(conn, table: str) -> list[str]:
    """A table's column names, in order: PRAGMA table_info on SQLite, the catalogue on Postgres."""
    if isinstance(conn, sqlite3.Connection):
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    return sqlpg.table_columns(conn, table)
