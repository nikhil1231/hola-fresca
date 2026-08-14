"""products.base_price / base_unit_price: the shelf price behind a promotion

One price column was answering two questions, and the two shops answered it in
opposite directions: Sainsbury's ``retail_price`` is already the promotional
figure (Nectar half-price included), while Ocado's ``price`` is the shelf price
with ``promoPrice`` stated separately and, until now, ignored. So a quarter of
the Sainsbury's catalogue was ranked on a discount that mostly expires within the
month, and the same offer at Ocado counted for nothing.

Splitting them lets each question have its own answer: ``price`` is what the
trolley charges today, and ``base_price`` is what the shelf charges without the
offer, NULL when there is no offer to strip. The mapping sorts on the base — a
rank is written into ``ingredient_mapping_products`` and outlives the fortnight a
promotion runs for — and the basket spends the live one.

Nullable with no backfill here. The values are recoverable from each product's
stored ``raw_json`` without re-fetching anything, which is
``python -m app.scraper.products backfill-prices``'s job; doing it in a migration
would mean parsing two retailers' payload shapes from inside alembic.

Revision ID: 0009_product_base_price
Revises: 0008_planner_cache_revisions
Create Date: 2026-08-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_product_base_price"
down_revision = "0008_planner_cache_revisions"
branch_labels = None
depends_on = None

_TABLE = "products"
_COLUMNS = ("base_price", "base_unit_price")


def _existing() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        # Nothing to add to; create_all builds it from the model at head.
        return set(_COLUMNS)
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    existing = _existing()
    for column in _COLUMNS:
        if column not in existing:
            op.add_column(_TABLE, sa.Column(column, sa.Float(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return
    existing = {column["name"] for column in inspector.get_columns(_TABLE)}
    for column in _COLUMNS:
        if column in existing:
            op.drop_column(_TABLE, column)
