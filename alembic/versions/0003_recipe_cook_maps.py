"""cache generated recipe cook maps

Revision ID: 0003_recipe_cook_maps
Revises: 0002_accounts
Create Date: 2026-08-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_recipe_cook_maps"
down_revision = "0002_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("recipe_cook_maps"):
        return
    op.create_table(
        "recipe_cook_maps",
        sa.Column("recipe_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="processing"),
        sa.Column("graph_json", sa.Text(), nullable=True),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("prompt_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generation_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("recipe_id"),
    )
    op.create_index("ix_recipe_cook_maps_status", "recipe_cook_maps", ["status"])


def downgrade() -> None:
    op.drop_index("ix_recipe_cook_maps_status", table_name="recipe_cook_maps")
    op.drop_table("recipe_cook_maps")
