"""Engine, session management and schema upkeep.

The schema is built two different ways, on purpose. A **fresh** database — every
test, and a rebuild from the raw payload store — is created by
``Base.metadata.create_all`` and stamped at alembic head: it is a derivative that
can be thrown away, and replaying two years of migrations to reach a schema the
models already describe would only be slower and less faithful. An **existing**
database is migrated by alembic, because it holds the one thing that is not
derivable — what its user planned, rated and chose.

Before accounts, existing databases were kept up to date by ``_RUNTIME_COLUMNS``
below, which can only bolt a nullable column onto a table. That was enough while
every change was additive and stopped being enough with user ownership, which
changes primary keys and drops columns. Those columns stay listed because
databases predating alembic still need them; anything new belongs in a migration.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.db.base import Base
from app.db import models  # noqa: F401  (register models on Base.metadata)
from app.db.retailer_accounts import seed_legacy_ocado_accounts
from app.db.models import User

log = logging.getLogger(__name__)

#: Where alembic's scripts live, found from this file so the CLI's working
#: directory does not decide whether migrations can be run.
ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "alembic"

#: The revision an existing pre-accounts database is stamped with before it is
#: upgraded. See alembic/versions/0001_baseline.py.
BASELINE_REVISION = "0001_baseline"

#: Held for the whole of :func:`init_db`. FastAPI runs sync dependencies in a
#: threadpool, so the first few requests after a cold start all call this at
#: once — and alembic's ``EnvironmentContext`` publishes itself through module
#: globals, one set per process. Two overlapping runs leave the second one
#: tearing down a proxy the first already removed (``KeyError: 'script'``), which
#: surfaces as a 500 before the endpoint is even entered. Serialising the whole
#: function also keeps ``create_all`` and the ALTER TABLEs below from racing.
_INIT_LOCK = threading.Lock()


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
    # ``preferred_sku`` was here until 0002_accounts moved it to
    # user_pack_preferences. It must not come back: this dict runs after the
    # migrations, so listing it would re-add the column the migration dropped.
    "ingredient_mappings": {"unit_kind": "TEXT DEFAULT 'mass'"},
    "products": {"stock_checked_at": "DATETIME"},
    "plan_settings": {"pack_shortfall_tolerance_pct": "REAL DEFAULT 10"},
    # ``retailer`` is added by 0011 with the unique constraint that goes with it;
    # it is listed here for the same reason ``account_id`` is, so a database that
    # was stamped rather than migrated still gets the column the API reads.
    "ocado_cart_sync": {"account_id": "VARCHAR(64)", "retailer": "VARCHAR(64) DEFAULT 'ocado'"},
    "ocado_cart_ledger": {"account_id": "VARCHAR(64)", "retailer": "VARCHAR(64) DEFAULT 'ocado'"},
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
        "manually_included": "INTEGER DEFAULT 0",
    },
}


def _alembic_config(connection: Connection):
    """An alembic Config bound to an open connection.

    Imported lazily: the scraper and analysis CLIs open the database too, and
    they should not pay for alembic's import to do it.
    """
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_DIR.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.attributes["connection"] = connection
    # The app configures logging in main.py; alembic's fileConfig would replace
    # those handlers and silence everything the app logs after startup.
    cfg.attributes["configure_logger"] = False
    return cfg


def _is_fresh(conn: Connection) -> bool:
    """True when nothing has been built here yet.

    ``recipes`` is the marker rather than ``alembic_version``: a database created
    before alembic existed has no version table either, and the two cases need
    opposite treatment.
    """
    from sqlalchemy import inspect

    return not inspect(conn).has_table("recipes")


def _run_migrations(conn: Connection) -> None:
    from alembic import command
    from alembic.runtime.migration import MigrationContext

    cfg = _alembic_config(conn)
    current = MigrationContext.configure(conn).get_current_revision()
    if current is None:
        # An existing database from before alembic: its schema is the baseline by
        # definition, so record that rather than trying to replay it.
        log.info("database predates migrations; stamping %s", BASELINE_REVISION)
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")


def ensure_bootstrap_user(engine: Engine) -> int:
    """Make sure an account exists, and return the id of the first one.

    There is no sign-up yet, so the account is created by the app the first time
    it opens a database rather than by anyone asking for it. Once Google sign-in
    lands this stays as the fallback for an empty ``users`` table — the first
    person through the door — rather than the only way a user is ever made.
    """
    with Session(engine) as session:
        user_id = session.scalar(select(User.id).order_by(User.id).limit(1))
        if user_id is not None:
            return user_id
        user = User(is_admin=1)
        session.add(user)
        session.commit()
        return user.id


def init_db(engine: Engine) -> None:
    """Bring the database at ``engine`` up to the current schema.

    A fresh database is built from the models; an existing one is migrated. Note
    what that means for anything added from here on: ``create_all`` never touches
    a database that already has a ``recipes`` table, so a new model without a
    migration will exist in tests and be missing in production. Write the
    migration.

    One caller at a time — see ``_INIT_LOCK``.
    """
    with _INIT_LOCK:
        _init_db(engine)


def _init_db(engine: Engine) -> None:
    with engine.begin() as conn:
        fresh = _is_fresh(conn)

    if fresh:
        Base.metadata.create_all(engine)

    with engine.begin() as conn:
        if fresh:
            # Nothing to migrate — the schema was just built at head — but the
            # version has to be recorded, or the next startup would try to apply
            # every migration to a database that already has their effect.
            from alembic import command

            command.stamp(_alembic_config(conn), "head")
        else:
            _run_migrations(conn)

    ensure_bootstrap_user(engine)

    with engine.begin() as conn:
        # Fresh databases are stamped at head, so migration 0012 does not run on
        # them.  Seed here too; the helper is idempotent for existing databases
        # where the migration already inserted the rows.
        seed_legacy_ocado_accounts(conn, config.OCADO_ACCOUNTS)
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
        # Every row written before the ledger became per-retailer was an Ocado
        # claim, because Ocado was the only shop that could be pushed to.
        for table in ("ocado_cart_sync", "ocado_cart_ledger"):
            conn.execute(
                text(f"UPDATE {table} SET retailer = 'ocado' WHERE retailer IS NULL OR retailer = ''")
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
