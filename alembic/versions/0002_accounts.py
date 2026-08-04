"""accounts: give every personal row an owner, and move the plan into the database

Three things happen here, in the order they have to happen in:

1. A ``users`` table with one row, which everything personal is then attributed
   to. The app has no login yet — this row is resolved unconditionally — but the
   column has to exist first, because retrofitting a primary key under SQLite
   means rebuilding the table and that is much cheaper with two rows in it than
   with two years of them.
2. The tables that used to be ``localStorage``: the week's recipes, the per-week
   pack/snap/owned decisions on the basket page. Those were per-device by
   accident of where they were stored, and are the reason for this migration.
3. ``ingredient_mappings.preferred_sku`` moves to ``user_pack_preferences``. The
   mapping row is shared catalogue; which pack you always buy is not.

Revision ID: 0002_accounts
Revises: 0001_baseline
Create Date: 2026-08-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_accounts"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

#: Everything already in the database belongs to whoever has been running the
#: app, and this is their row. Hard-coded rather than looked up because a
#: migration must produce the same result on every database it is ever run
#: against, including a restored backup.
BOOTSTRAP_USER_ID = 1


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return any(col["name"] == column for col in inspector.get_columns(table))


def _unique_constraint_names(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {
        uq["name"]
        for uq in inspector.get_unique_constraints(table)
        if uq.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()

    # ---- 1. the account itself -------------------------------------------
    if not _has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("name", sa.Text(), nullable=True),
            sa.Column("is_admin", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("email", name="uq_user_email"),
        )

    # The one existing user is by definition the person who has been curating the
    # library, so they are the admin.
    existing = bind.execute(sa.text("SELECT COUNT(*) FROM users")).scalar_one()
    if not existing:
        bind.execute(
            sa.text(
                "INSERT INTO users (id, email, name, is_admin, created_at) "
                "VALUES (:id, NULL, NULL, 1, CURRENT_TIMESTAMP)"
            ),
            {"id": BOOTSTRAP_USER_ID},
        )

    # ---- 2. new personal tables ------------------------------------------
    if not _has_table("user_recipe_hides"):
        op.create_table(
            "user_recipe_hides",
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("recipe_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("user_id", "recipe_id"),
        )

    if not _has_table("plan_selections"):
        op.create_table(
            "plan_selections",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("week_start", sa.String(length=16), nullable=False),
            sa.Column("recipe_id", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("portions", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("protein_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "week_start", "recipe_id",
                name="uq_plan_selection_user_week_recipe",
            ),
        )
        op.create_index("ix_plan_selections_user_id", "plan_selections", ["user_id"])
        op.create_index("ix_plan_selections_week_start", "plan_selections", ["week_start"])
        op.create_index("ix_plan_selections_recipe_id", "plan_selections", ["recipe_id"])
        op.create_index(
            "ix_plan_selection_user_week", "plan_selections", ["user_id", "week_start"]
        )

    if not _has_table("plan_week_items"):
        op.create_table(
            "plan_week_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("week_start", sa.String(length=16), nullable=False),
            sa.Column("ingredient_key", sa.String(length=255), nullable=False),
            sa.Column("pack_sku", sa.String(length=128), nullable=True),
            sa.Column("snapped", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("owned", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "week_start", "ingredient_key",
                name="uq_plan_week_item_user_week_key",
            ),
        )
        op.create_index("ix_plan_week_items_user_id", "plan_week_items", ["user_id"])
        op.create_index("ix_plan_week_items_week_start", "plan_week_items", ["week_start"])
        op.create_index(
            "ix_plan_week_items_ingredient_key", "plan_week_items", ["ingredient_key"]
        )
        op.create_index(
            "ix_plan_week_item_user_week", "plan_week_items", ["user_id", "week_start"]
        )

    if not _has_table("user_pack_preferences"):
        op.create_table(
            "user_pack_preferences",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("retailer", sa.String(length=64), nullable=False, server_default="ocado"),
            sa.Column("ingredient_key", sa.String(length=255), nullable=False),
            sa.Column("sku", sa.String(length=128), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "retailer", "ingredient_key", name="uq_user_pack_pref_user_key"
            ),
        )
        op.create_index("ix_user_pack_preferences_user_id", "user_pack_preferences", ["user_id"])
        op.create_index(
            "ix_user_pack_preferences_retailer", "user_pack_preferences", ["retailer"]
        )
        op.create_index(
            "ix_user_pack_preferences_ingredient_key",
            "user_pack_preferences",
            ["ingredient_key"],
        )

    # ---- 3. give the existing personal rows an owner ----------------------
    # These two gain a *primary key* column, which SQLite cannot do in place, so
    # they are rebuilt rather than altered. Small tables; the copy is instant.
    _rebuild_with_user_pk(
        "personal_recipe_ratings",
        """
        CREATE TABLE personal_recipe_ratings_new (
            user_id INTEGER NOT NULL,
            recipe_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (user_id, recipe_id),
            CONSTRAINT ck_personal_rating_range CHECK (rating >= 1 AND rating <= 5),
            FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY(recipe_id) REFERENCES recipes (id)
        )
        """,
        """
        INSERT INTO personal_recipe_ratings_new
            (user_id, recipe_id, rating, created_at, updated_at)
        SELECT :user_id, recipe_id, rating, created_at, updated_at
        FROM personal_recipe_ratings
        """,
    )
    _rebuild_with_user_pk(
        "personal_recipe_wishlist",
        """
        CREATE TABLE personal_recipe_wishlist_new (
            user_id INTEGER NOT NULL,
            recipe_id INTEGER NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (user_id, recipe_id),
            FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY(recipe_id) REFERENCES recipes (id)
        )
        """,
        """
        INSERT INTO personal_recipe_wishlist_new (user_id, recipe_id, created_at)
        SELECT :user_id, recipe_id, created_at FROM personal_recipe_wishlist
        """,
    )

    # ---- 4. plan_settings: one row becomes one row per user ---------------
    if _has_table("plan_settings") and not _has_column("plan_settings", "user_id"):
        op.add_column("plan_settings", sa.Column("user_id", sa.Integer(), nullable=True))
        bind.execute(
            sa.text("UPDATE plan_settings SET user_id = :user_id WHERE user_id IS NULL"),
            {"user_id": BOOTSTRAP_USER_ID},
        )
        # More than one settings row could only come from a bug, but if it did,
        # keep the first and drop the rest: they would now collide on the unique
        # constraint below and fail the whole migration.
        bind.execute(
            sa.text(
                "DELETE FROM plan_settings WHERE id NOT IN "
                "(SELECT MIN(id) FROM plan_settings)"
            )
        )
        with op.batch_alter_table("plan_settings") as batch:
            batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
            batch.create_unique_constraint("uq_plan_settings_user", ["user_id"])
        op.create_index("ix_plan_settings_user_id", "plan_settings", ["user_id"])

    # ---- 5. plan_weeks: a skip belongs to whoever skipped it --------------
    if _has_table("plan_weeks") and not _has_column("plan_weeks", "user_id"):
        existing_uniques = _unique_constraint_names("plan_weeks")
        op.add_column("plan_weeks", sa.Column("user_id", sa.Integer(), nullable=True))
        bind.execute(
            sa.text("UPDATE plan_weeks SET user_id = :user_id WHERE user_id IS NULL"),
            {"user_id": BOOTSTRAP_USER_ID},
        )
        with op.batch_alter_table("plan_weeks") as batch:
            batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
            if "uq_plan_week_start" in existing_uniques:
                batch.drop_constraint("uq_plan_week_start", type_="unique")
            batch.create_unique_constraint(
                "uq_plan_week_user_start", ["user_id", "week_start"]
            )
        op.create_index("ix_plan_weeks_user_id", "plan_weeks", ["user_id"])

    # ---- 6. the standing pack choice stops being catalogue ----------------
    if _has_column("ingredient_mappings", "preferred_sku"):
        bind.execute(
            sa.text(
                """
                INSERT INTO user_pack_preferences
                    (user_id, retailer, ingredient_key, sku, updated_at)
                SELECT :user_id, retailer, ingredient_key, preferred_sku, CURRENT_TIMESTAMP
                FROM ingredient_mappings
                WHERE preferred_sku IS NOT NULL AND preferred_sku != ''
                """
            ),
            {"user_id": BOOTSTRAP_USER_ID},
        )
        with op.batch_alter_table("ingredient_mappings") as batch:
            batch.drop_column("preferred_sku")


def _rebuild_with_user_pk(table: str, create_sql: str, copy_sql: str) -> None:
    """Rebuild ``table`` with ``user_id`` in its primary key, keeping the rows."""
    if not _has_table(table) or _has_column(table, "user_id"):
        return
    bind = op.get_bind()
    bind.execute(sa.text(create_sql))
    bind.execute(sa.text(copy_sql), {"user_id": BOOTSTRAP_USER_ID})
    bind.execute(sa.text(f"DROP TABLE {table}"))
    bind.execute(sa.text(f"ALTER TABLE {table}_new RENAME TO {table}"))


def downgrade() -> None:
    raise NotImplementedError(
        "0002_accounts is one-way: downgrading would have to decide which user's "
        "plan survives. Restore a snapshot instead — see deploy/README.md."
    )
