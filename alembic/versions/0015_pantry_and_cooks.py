"""carry cupboard leftovers between shops, and record what got cooked

The planner prices every week from zero: it works out how much of each pack will
be left over, values the remainder, and then forgets it. Next week buys the rice
again. Carrying the remainder forward needs two things the schema could not say.

The first is what was *consumed*, which nothing recorded — ``plan_selections``
says what was planned, and no row said it happened. ``plan_cook_marks`` holds
only the departures from the assumption that a recipe still in the plan when its
week ended was cooked; ``plan_week_pushes`` holds the evidence that the week was
shopped for at all, which is what makes that assumption safe to make.

The second is the cupboard itself. ``pantry_lots`` carries one row per ingredient
per shop, holding what was available after it and what the week's recipes were
going to take out of it, so what remains can be derived rather than stored and an
untick puts the grams back without a second write.

Revision ID: 0015_pantry_and_cooks
Revises: 0014_recipe_manually_included
Create Date: 2026-08-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_pantry_and_cooks"
down_revision = "0014_recipe_manually_included"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("plan_week_pushes"):
        op.create_table(
            "plan_week_pushes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("retailer", sa.String(length=64), nullable=False, server_default="ocado"),
            sa.Column("week_start", sa.String(length=16), nullable=False),
            sa.Column("pushed_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "retailer", "week_start",
                name="uq_plan_week_push_user_retailer_week",
            ),
        )
        op.create_index("ix_plan_week_pushes_user_id", "plan_week_pushes", ["user_id"])
        op.create_index("ix_plan_week_pushes_retailer", "plan_week_pushes", ["retailer"])
        op.create_index("ix_plan_week_pushes_week_start", "plan_week_pushes", ["week_start"])
        op.create_index(
            "ix_plan_week_push_user_week", "plan_week_pushes", ["user_id", "week_start"]
        )

    if not inspector.has_table("plan_cook_marks"):
        op.create_table(
            "plan_cook_marks",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("week_start", sa.String(length=16), nullable=False),
            sa.Column("recipe_id", sa.Integer(), nullable=False),
            sa.Column("cooked", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("marked_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "week_start", "recipe_id",
                name="uq_plan_cook_mark_user_week_recipe",
            ),
        )
        op.create_index("ix_plan_cook_marks_user_id", "plan_cook_marks", ["user_id"])
        op.create_index("ix_plan_cook_marks_week_start", "plan_cook_marks", ["week_start"])
        op.create_index("ix_plan_cook_marks_recipe_id", "plan_cook_marks", ["recipe_id"])
        op.create_index(
            "ix_plan_cook_mark_user_week", "plan_cook_marks", ["user_id", "week_start"]
        )

    if not inspector.has_table("pantry_lots"):
        op.create_table(
            "pantry_lots",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("retailer", sa.String(length=64), nullable=False, server_default="ocado"),
            sa.Column("ingredient_key", sa.String(length=255), nullable=False),
            sa.Column("week_start", sa.String(length=16), nullable=False),
            sa.Column("ingredient_name", sa.Text(), nullable=True),
            sa.Column("available_g", sa.Float(), nullable=False, server_default="0"),
            sa.Column("available_qty", sa.Float(), nullable=True),
            sa.Column("unit_kind", sa.String(length=16), nullable=False, server_default="mass"),
            sa.Column("salvage", sa.Float(), nullable=False, server_default="0"),
            sa.Column("contributions_json", sa.Text(), nullable=True),
            sa.Column("superseded_at", sa.DateTime(), nullable=True),
            sa.Column("emptied_at", sa.DateTime(), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("confirmed_week_start", sa.String(length=16), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "retailer", "ingredient_key", "week_start",
                name="uq_pantry_lot_user_retailer_key_week",
            ),
        )
        op.create_index("ix_pantry_lots_user_id", "pantry_lots", ["user_id"])
        op.create_index("ix_pantry_lots_retailer", "pantry_lots", ["retailer"])
        op.create_index("ix_pantry_lots_ingredient_key", "pantry_lots", ["ingredient_key"])
        op.create_index("ix_pantry_lots_week_start", "pantry_lots", ["week_start"])
        # The read every basket build does: this user's cupboard at this shop.
        op.create_index("ix_pantry_lot_user_retailer", "pantry_lots", ["user_id", "retailer"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("pantry_lots", "plan_cook_marks", "plan_week_pushes"):
        if inspector.has_table(table):
            op.drop_table(table)
