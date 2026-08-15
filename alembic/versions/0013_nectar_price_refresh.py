"""Nectar price identity and per-user retailer refresh cadence

Revision ID: 0013_nectar_price_refresh
Revises: 0012_retailer_accounts
Create Date: 2026-08-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_nectar_price_refresh"
down_revision = "0012_retailer_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    product_columns = {column["name"] for column in inspector.get_columns("products")}
    if "is_nectar_price" not in product_columns:
        op.add_column(
            "products",
            sa.Column(
                "is_nectar_price", sa.Integer(), nullable=False, server_default="0"
            ),
        )

    if not inspector.has_table("user_retailer_price_refreshes"):
        op.create_table(
            "user_retailer_price_refreshes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("retailer", sa.String(length=64), nullable=False),
            sa.Column("last_refreshed_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id", "retailer", name="uq_user_retailer_price_refresh"
            ),
        )
        op.create_index(
            "ix_user_retailer_price_refreshes_user_id",
            "user_retailer_price_refreshes",
            ["user_id"],
        )
        op.create_index(
            "ix_user_retailer_price_refreshes_retailer",
            "user_retailer_price_refreshes",
            ["retailer"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_user_retailer_price_refreshes_retailer",
        table_name="user_retailer_price_refreshes",
    )
    op.drop_index(
        "ix_user_retailer_price_refreshes_user_id",
        table_name="user_retailer_price_refreshes",
    )
    op.drop_table("user_retailer_price_refreshes")
    op.drop_column("products", "is_nectar_price")
