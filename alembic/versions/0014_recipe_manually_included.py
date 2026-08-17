"""admit hand-picked recipes the curation rules cut

Curation demands a rating count a new or niche dish may never earn, so most of a
complete scrape sits outside the library and cannot be searched or planned. This
column is the counterpart to ``manually_excluded``: one recipe, admitted by hand,
surviving the next re-curation because it records a decision rather than a
derivation.

Revision ID: 0014_recipe_manually_included
Revises: 0013_nectar_price_refresh
Create Date: 2026-08-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_recipe_manually_included"
down_revision = "0013_nectar_price_refresh"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    columns = _columns("recipes")
    if not columns or "manually_included" in columns:
        return
    op.add_column(
        "recipes",
        sa.Column("manually_included", sa.Integer(), nullable=False, server_default="0"),
    )
    # Indexed for the same reason ``curated`` is: it sits in the library
    # predicate, which is on the hot path of every browse query.
    op.create_index(
        "ix_recipes_manually_included", "recipes", ["manually_included"]
    )


def downgrade() -> None:
    if "manually_included" not in _columns("recipes"):
        return
    op.drop_index("ix_recipes_manually_included", table_name="recipes")
    op.drop_column("recipes", "manually_included")
