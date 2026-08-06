"""Upgrading a database that predates accounts.

The real library is the one thing in this project that cannot be rebuilt from
source data — a plan, a rating and a standing pack choice exist nowhere else — so
the migration that gives them an owner is worth testing against a database shaped
the way that one actually was, rather than against the models.

The schema below is deliberately hand-written and minimal: it is a fixture of
history, and regenerating it from the current models would test nothing.
"""
from __future__ import annotations

from sqlalchemy import text

from app.db.session import init_db, make_engine

#: The pre-accounts schema, cut down to the tables migration 0002 touches plus
#: the ones app.db.session patches columns onto on the way past.
_OLD_SCHEMA = [
    """
    CREATE TABLE recipes (
        id INTEGER NOT NULL PRIMARY KEY,
        source VARCHAR(64), source_id VARCHAR(64), url TEXT, name TEXT,
        curated INTEGER DEFAULT 0, manually_excluded INTEGER DEFAULT 0
    )
    """,
    "CREATE TABLE recipe_ingredients (id INTEGER NOT NULL PRIMARY KEY, recipe_id INTEGER)",
    "CREATE TABLE recipe_steps (id INTEGER NOT NULL PRIMARY KEY, recipe_id INTEGER)",
    "CREATE TABLE products (id INTEGER NOT NULL PRIMARY KEY, retailer VARCHAR(64), sku VARCHAR(128))",
    """
    CREATE TABLE ingredient_mappings (
        id INTEGER NOT NULL PRIMARY KEY,
        retailer VARCHAR(64) NOT NULL,
        ingredient_key TEXT NOT NULL,
        name TEXT NOT NULL,
        line_count INTEGER DEFAULT 0,
        status VARCHAR(32) DEFAULT 'proposed',
        pantry_staple INTEGER DEFAULT 0,
        preferred_sku VARCHAR(128),
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE personal_recipe_ratings (
        recipe_id INTEGER NOT NULL PRIMARY KEY,
        rating INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE personal_recipe_wishlist (
        recipe_id INTEGER NOT NULL PRIMARY KEY,
        created_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE plan_settings (
        id INTEGER NOT NULL PRIMARY KEY,
        cadence_weeks INTEGER DEFAULT 1,
        anchor_week_start VARCHAR(16) NOT NULL,
        cutoff_days_before INTEGER DEFAULT 2,
        cutoff_time VARCHAR(5) DEFAULT '18:00',
        paused INTEGER DEFAULT 0,
        horizon_weeks INTEGER DEFAULT 6,
        recipes_per_week INTEGER DEFAULT 5,
        default_portions INTEGER DEFAULT 4,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE plan_weeks (
        id INTEGER NOT NULL PRIMARY KEY,
        week_start VARCHAR(16) NOT NULL,
        skipped INTEGER DEFAULT 0,
        note TEXT,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        CONSTRAINT uq_plan_week_start UNIQUE (week_start)
    )
    """,
    """
    CREATE TABLE ocado_cart_sync (
        id INTEGER NOT NULL PRIMARY KEY, week_start VARCHAR(16), synced_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE ocado_cart_ledger (
        id INTEGER NOT NULL PRIMARY KEY, sku VARCHAR(128) NOT NULL, quantity INTEGER NOT NULL,
        name TEXT, ingredient_key VARCHAR(255), ingredient_name TEXT,
        week_start VARCHAR(16), synced_at DATETIME NOT NULL
    )
    """,
]

_OLD_ROWS = [
    "INSERT INTO recipes (id, name, curated) VALUES (7, 'Chicken Curry', 1)",
    "INSERT INTO recipes (id, name, curated) VALUES (8, 'Pork Noodles', 1)",
    "INSERT INTO personal_recipe_ratings (recipe_id, rating, created_at, updated_at) "
    "VALUES (7, 5, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    "INSERT INTO personal_recipe_wishlist (recipe_id, created_at) VALUES (8, CURRENT_TIMESTAMP)",
    "INSERT INTO plan_settings (id, cadence_weeks, anchor_week_start, updated_at) "
    "VALUES (1, 2, '2026-08-03', CURRENT_TIMESTAMP)",
    "INSERT INTO plan_weeks (id, week_start, skipped, created_at, updated_at) "
    "VALUES (1, '2026-08-10', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    "INSERT INTO ingredient_mappings "
    "(id, retailer, ingredient_key, name, status, preferred_sku, created_at, updated_at) "
    "VALUES (1, 'ocado', 'name:rice', 'Rice', 'approved', 'rice-1kg', "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    "INSERT INTO ingredient_mappings "
    "(id, retailer, ingredient_key, name, status, preferred_sku, created_at, updated_at) "
    "VALUES (2, 'ocado', 'name:salt', 'Salt', 'approved', NULL, "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
]


def _old_database(path):
    engine = make_engine(path)
    with engine.begin() as conn:
        for statement in _OLD_SCHEMA + _OLD_ROWS:
            conn.execute(text(statement))
    return engine


def test_an_old_database_is_migrated_rather_than_rebuilt(tmp_path):
    engine = _old_database(tmp_path / "old.db")

    init_db(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0004_planner_cache_revisions"
        # Nothing was dropped on the way through.
        assert conn.execute(text("SELECT COUNT(*) FROM recipes")).scalar() == 2
        assert conn.execute(
            text("SELECT COUNT(*) FROM recipe_cook_maps")
        ).scalar() == 0
        assert conn.execute(
            text("SELECT recipe_revision, ingredient_revision FROM planner_cache_state")
        ).one() == (1, 1)
        trigger_names = set(
            conn.scalars(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        )
        assert "trg_planner_cache_recipes_update" in trigger_names
        assert "trg_planner_cache_products_update" in trigger_names


def test_everything_personal_is_handed_to_the_existing_user(tmp_path):
    """There is one person's data in there and one account to give it to."""
    engine = _old_database(tmp_path / "old.db")

    init_db(engine)

    with engine.connect() as conn:
        (user_id,) = conn.execute(text("SELECT id FROM users")).one()
        assert conn.execute(
            text("SELECT recipe_id, user_id, rating FROM personal_recipe_ratings")
        ).all() == [(7, user_id, 5)]
        assert conn.execute(
            text("SELECT recipe_id, user_id FROM personal_recipe_wishlist")
        ).all() == [(8, user_id)]
        assert conn.execute(
            text("SELECT user_id, cadence_weeks, anchor_week_start FROM plan_settings")
        ).all() == [(user_id, 2, "2026-08-03")]
        assert conn.execute(
            text("SELECT user_id, week_start, skipped FROM plan_weeks")
        ).all() == [(user_id, "2026-08-10", 1)]


def test_the_existing_user_is_an_admin(tmp_path):
    """Whoever has been curating the library keeps being allowed to."""
    engine = _old_database(tmp_path / "old.db")

    init_db(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT is_admin FROM users")).scalar() == 1


def test_standing_pack_choices_move_off_the_shared_mapping(tmp_path):
    engine = _old_database(tmp_path / "old.db")

    init_db(engine)

    with engine.connect() as conn:
        (user_id,) = conn.execute(text("SELECT id FROM users")).one()
        assert conn.execute(
            text("SELECT user_id, retailer, ingredient_key, sku FROM user_pack_preferences")
        ).all() == [(user_id, "ocado", "name:rice", "rice-1kg")]
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(ingredient_mappings)"))}
        assert "preferred_sku" not in columns, "the column must not come back"


def test_migrating_twice_changes_nothing(tmp_path):
    """Every start-up runs this; it has to be safe to run on an up-to-date file."""
    engine = _old_database(tmp_path / "old.db")
    init_db(engine)

    init_db(engine)
    init_db(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM user_pack_preferences")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM personal_recipe_ratings")).scalar() == 1


def test_a_fresh_database_is_stamped_at_head(tmp_path):
    """Otherwise the next start-up would replay migrations onto a current schema."""
    engine = make_engine(tmp_path / "fresh.db")

    init_db(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0004_planner_cache_revisions"
        assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar() == 1
        assert conn.execute(
            text("SELECT COUNT(*) FROM recipe_cook_maps")
        ).scalar() == 0
