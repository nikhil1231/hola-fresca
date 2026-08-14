"""plan_settings.retailer: which shop a user's weeks are priced and bought at

The catalogue has always been able to hold more than one retailer — ``products``,
``product_search_hits``, ``ingredient_mappings`` and ``user_pack_preferences``
are all keyed by it — but there was nowhere to record *which* one a given person
shops at, because there was only ever one. This adds that.

It goes on ``plan_settings`` rather than into a table of its own because it is
the same kind of fact as the cadence and the cutoff: a standing choice about how
this person shops, one row per user, changed rarely.

``server_default='ocado'`` is what makes this a pure addition. Every row that
exists was written when Ocado was the only shop, and every retailer-scoped row
those users own already carries ``retailer='ocado'``, so the default is not a
guess — it is the value the data already implies. Nothing needs backfilling.

Revision ID: 0004_plan_settings_retailer
Revises: 0003_ocado_auth_events
Create Date: 2026-08-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_plan_settings_retailer"
down_revision = "0003_ocado_auth_events"
branch_labels = None
depends_on = None

_TABLE = "plan_settings"
_COLUMN = "retailer"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return True  # nothing to add to; create_all will build it from the model
    return any(column["name"] == _COLUMN for column in inspector.get_columns(_TABLE))


def upgrade() -> None:
    if _has_column():
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=64), nullable=False, server_default="ocado"),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return
    if any(column["name"] == _COLUMN for column in inspector.get_columns(_TABLE)):
        op.drop_column(_TABLE, _COLUMN)
