"""Engine and session management.

Phase 1 creates the schema directly with ``Base.metadata.create_all``. The
database is a disposable derivative of the raw payload store — it can be dropped
and rebuilt by re-running the normalize stage — so migrations are deferred until
the schema stabilises with the planner/pantry work.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.db.base import Base
from app.db import models  # noqa: F401  (register models on Base.metadata)


def make_engine(db_path: Path | None = None) -> Engine:
    engine = create_engine(config.db_url(db_path), future=True)
    return engine


# Columns declared after a database was first created. ``create_all`` only makes
# whole tables, so anything added to an existing table has to be listed here —
# these run on every startup because the API reads them, so waiting for the next
# enrich pass would break it in the meantime.
_RUNTIME_COLUMNS: dict[str, dict[str, str]] = {
    "recipe_ingredients": {"position": "INTEGER"},
    "recipe_steps": {"image_path": "TEXT"},
    "ingredient_mappings": {"unit_kind": "TEXT DEFAULT 'mass'", "preferred_sku": "VARCHAR(128)"},
    "products": {"stock_checked_at": "DATETIME"},
    "ocado_cart_sync": {"account_id": "VARCHAR(64)"},
    "ocado_cart_ledger": {"account_id": "VARCHAR(64)"},
    "recipes": {
        "flagged_suspicious": "INTEGER DEFAULT 0",
        "audited_at": "DATETIME",
        "aggregate_rating": "REAL",
        "aggregate_ratings_count": "INTEGER",
        "effective_rating": "REAL",
        "effective_ratings_count": "INTEGER",
        "unique_recipe_code": "VARCHAR(64)",
        "family_code": "VARCHAR(64)",
        "cloned_from": "VARCHAR(64)",
        "source_active": "INTEGER DEFAULT 0",
        "source_published": "INTEGER DEFAULT 0",
        "course": "VARCHAR(16) DEFAULT 'main'",
        "manually_excluded": "INTEGER DEFAULT 0",
    },
}


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, columns in _RUNTIME_COLUMNS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))
        default_account = config.DEFAULT_OCADO_ACCOUNT_ID
        conn.execute(
            text("UPDATE ocado_cart_sync SET account_id = :account_id WHERE account_id IS NULL OR account_id = ''"),
            {"account_id": default_account},
        )
        conn.execute(
            text("UPDATE ocado_cart_ledger SET account_id = :account_id WHERE account_id IS NULL OR account_id = ''"),
            {"account_id": default_account},
        )
        _rebuild_old_ocado_ledger_table(conn, default_account)


def _rebuild_old_ocado_ledger_table(conn, default_account: str) -> None:
    """Drop the old global SKU uniqueness so ledgers can be per account."""
    indexes = conn.execute(text("PRAGMA index_list(ocado_cart_ledger)")).all()
    has_global_sku_unique = False
    for index in indexes:
        if not index[2]:
            continue
        columns = [
            row[2]
            for row in conn.execute(text(f"PRAGMA index_info({index[1]})")).all()
        ]
        if columns == ["sku"]:
            has_global_sku_unique = True
            break
    if not has_global_sku_unique:
        return

    conn.execute(
        text(
            """
            CREATE TABLE ocado_cart_ledger_new (
                id INTEGER NOT NULL,
                account_id VARCHAR(64) NOT NULL,
                sku VARCHAR(128) NOT NULL,
                quantity INTEGER NOT NULL,
                name TEXT,
                ingredient_key VARCHAR(255),
                ingredient_name TEXT,
                week_start VARCHAR(16),
                synced_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_ocado_cart_ledger_account_sku UNIQUE (account_id, sku)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO ocado_cart_ledger_new (
                id, account_id, sku, quantity, name, ingredient_key,
                ingredient_name, week_start, synced_at
            )
            SELECT
                id,
                COALESCE(NULLIF(account_id, ''), :account_id),
                sku,
                quantity,
                name,
                ingredient_key,
                ingredient_name,
                week_start,
                synced_at
            FROM ocado_cart_ledger
            """
        ),
        {"account_id": default_account},
    )
    conn.execute(text("DROP TABLE ocado_cart_ledger"))
    conn.execute(text("ALTER TABLE ocado_cart_ledger_new RENAME TO ocado_cart_ledger"))
    conn.execute(text("CREATE INDEX ix_ocado_cart_ledger_account_id ON ocado_cart_ledger (account_id)"))
    conn.execute(text("CREATE INDEX ix_ocado_cart_ledger_sku ON ocado_cart_ledger (sku)"))


def ensure_columns(session: Session, table: str, columns: dict[str, str]) -> None:
    """Add any missing columns to an existing table, in place.

    ``create_all`` only creates whole tables, so an already-populated database
    never gains a newly declared column. Maps column name -> SQLite declaration.
    """
    existing = {row[1] for row in session.execute(text(f"PRAGMA table_info({table})"))}
    for name, decl in columns.items():
        if name not in existing:
            session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))
    session.commit()


def ensure_runtime_schema(engine: Engine) -> None:
    """Keep existing local SQLite DBs compatible with newly declared columns."""
    init_db(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
