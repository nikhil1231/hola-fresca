"""catalogue-aware planner cache revisions

Revision ID: 0004_planner_cache_revisions
Revises: 0003_recipe_cook_maps
Create Date: 2026-08-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_planner_cache_revisions"
down_revision = "0003_recipe_cook_maps"
branch_labels = None
depends_on = None

RECIPE_TABLES = ("recipes", "recipe_ingredients")
INGREDIENT_TABLES = ("ingredient_mappings", "ingredient_mapping_products", "products")


def _trigger_name(table: str, operation: str) -> str:
    return f"trg_planner_cache_{table}_{operation.lower()}"


def upgrade() -> None:
    op.create_table(
        "planner_cache_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recipe_revision", sa.Integer(), nullable=False),
        sa.Column("ingredient_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_planner_cache_state_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO planner_cache_state "
        "(id, recipe_revision, ingredient_revision) VALUES (1, 1, 1)"
    )
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    for column, tables in (
        ("recipe_revision", RECIPE_TABLES),
        ("ingredient_revision", INGREDIENT_TABLES),
    ):
        for table in tables:
            # The pre-Alembic compatibility fixture is intentionally a minimal
            # historical schema. A trigger cannot target a table that is absent;
            # real catalogue databases and fresh create_all databases have all
            # five tables and therefore receive the complete set.
            if table not in existing_tables:
                continue
            for operation in ("INSERT", "UPDATE", "DELETE"):
                op.execute(
                    f"""
                    CREATE TRIGGER {_trigger_name(table, operation)}
                    AFTER {operation} ON {table}
                    BEGIN
                        UPDATE planner_cache_state
                        SET {column} = {column} + 1
                        WHERE id = 1;
                    END
                    """
                )


def downgrade() -> None:
    for table in (*RECIPE_TABLES, *INGREDIENT_TABLES):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            op.execute(f"DROP TRIGGER IF EXISTS {_trigger_name(table, operation)}")
    op.drop_table("planner_cache_state")
