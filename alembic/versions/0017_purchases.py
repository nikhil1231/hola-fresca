"""what the household bought, and what each bought product looks like

"Not the usual protein bread, the other one" is answered by the order history:
the usual one is the loaf bought thirteen times, the other one the loaf bought
three. That history was read once by hand to seed the mappings; it is kept now,
one row per line of each delivered order, so the matcher and Noodle's shopping
resolver can ask it. Product descriptions hold a line about the packaging, read
from the product image, for "the one in the green bag".

Revision ID: 0017_purchases
Revises: 0016_pantry_use_by
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_purchases"
down_revision = "0016_pantry_use_by"
branch_labels = None
depends_on = None


def _has(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _has("purchases"):
        op.create_table(
            "purchases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("retailer", sa.String(length=64), nullable=False),
            sa.Column("account_key", sa.String(length=64), nullable=False),
            sa.Column("order_id", sa.String(length=64), nullable=False),
            sa.Column("sku", sa.String(length=128), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("delivered_on", sa.String(length=10), nullable=False),
            sa.UniqueConstraint("retailer", "order_id", "sku", name="uq_purchase_order_sku"),
        )
        for column in ("user_id", "retailer", "order_id", "sku", "delivered_on"):
            op.create_index(f"ix_purchases_{column}", "purchases", [column])
    if not _has("product_descriptions"):
        op.create_table(
            "product_descriptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("retailer", sa.String(length=64), nullable=False),
            sa.Column("sku", sa.String(length=128), nullable=False),
            sa.Column("packaging", sa.Text(), nullable=False),
            sa.Column("model", sa.String(length=64), nullable=True),
            sa.Column("described_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("retailer", "sku", name="uq_product_description_sku"),
        )
        for column in ("retailer", "sku"):
            op.create_index(f"ix_product_descriptions_{column}", "product_descriptions", [column])


def downgrade() -> None:
    if _has("product_descriptions"):
        op.drop_table("product_descriptions")
    if _has("purchases"):
        op.drop_table("purchases")
