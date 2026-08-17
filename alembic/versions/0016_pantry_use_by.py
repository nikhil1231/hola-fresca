"""a date on the food, for the things a salvage curve describes badly

The cupboard ages everything on the same curve: a fraction of the remainder
survives each shop, tuned to the question "is this worth anything at the next
one". That is a fair guess about a bag of rice nobody has reported on, and it is
the wrong shape for anything fresh — chicken is not 15% chicken by Friday, it is
fine and then it is rubbish.

It also stopped fresh stock being carried at all. Stating eight sausages on the
Monday and pricing the following week's shop applied one cycle of the chilled
figure and offered 8 x 0.257 = 2.06 sausages against the basket: a fraction of a
thing nobody owns a fraction of, derived from a number the user had typed in
exactly. A date turns that into the answer it should always have been — all
eight if they last until the delivery, none if they do not.

Revision ID: 0016_pantry_use_by
Revises: 0015_pantry_and_cooks
Create Date: 2026-08-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_pantry_use_by"
down_revision = "0015_pantry_and_cooks"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    columns = _columns("pantry_lots")
    if not columns or "use_by" in columns:
        return
    op.add_column("pantry_lots", sa.Column("use_by", sa.String(length=16), nullable=True))


def downgrade() -> None:
    if "use_by" not in _columns("pantry_lots"):
        return
    op.drop_column("pantry_lots", "use_by")
