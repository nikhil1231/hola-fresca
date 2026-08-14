"""ingredient_mapping_products.llm_rank: the model's own ordering, kept

``rank`` used to be whatever the LLM returned. It is now computed — match type
first, then a blend of unit price, confidence-adjusted rating and the model's
ordering (``app.mapping.ordering``) — and the input that cannot be recovered from
the catalogue is the model's ordering itself. Storing it means the balance can be
re-tuned by re-sorting what is already there (``python -m app.mapping reorder``)
instead of re-running the pass.

Nullable with no backfill, deliberately. Rows written before this have no
recorded model ordering, and inventing one from their ``rank`` would be a lie:
that rank *was* the model's answer for LLM rows, but on human rows it is the
reviewer's, and the two are not interchangeable. Absent, it simply contributes
nothing to the blend.

Revision ID: 0005_mapping_product_llm_rank
Revises: 0004_plan_settings_retailer
Create Date: 2026-08-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_mapping_product_llm_rank"
down_revision = "0004_plan_settings_retailer"
branch_labels = None
depends_on = None

_TABLE = "ingredient_mapping_products"
_COLUMN = "llm_rank"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return True  # nothing to add to; create_all will build it from the model
    return any(column["name"] == _COLUMN for column in inspector.get_columns(_TABLE))


def upgrade() -> None:
    if _has_column():
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return
    if any(column["name"] == _COLUMN for column in inspector.get_columns(_TABLE)):
        op.drop_column(_TABLE, _COLUMN)
