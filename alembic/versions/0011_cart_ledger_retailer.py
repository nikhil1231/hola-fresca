"""cart ledger and sync: which shop the claim is against

The cart ledger records what the last sync put in the retailer's cart, so that
the next one can tell its own contributions from yours. It was keyed by account
alone, which was enough while Ocado was the only shop a basket could be pushed
to. Sainsbury's is now shoppable too, and one account id ('default') would have
had the two shops' claims landing on the same rows — a Sainsbury's push reading
Ocado's ledger and "restoring" products into the wrong trolley.

``server_default='ocado'`` is not a guess. Every row that exists was written by
the only pushing code there was, and that code pushed to Ocado. Nothing needs
backfilling.

The unique constraints have to be rebuilt rather than added to, which on SQLite
means a table copy — hence ``batch_alter_table``. Both tables are small (one row
per product in one week's shop), so the copy is not worth optimising around.

Revision ID: 0011_cart_ledger_retailer
Revises: 0010_product_frozen_form
Create Date: 2026-08-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_cart_ledger_retailer"
down_revision = "0010_product_frozen_form"
branch_labels = None
depends_on = None

_COLUMN = "retailer"

#: table -> (old constraint name, new constraint name, columns of the new one)
_TABLES = {
    "ocado_cart_sync": (
        "uq_ocado_cart_sync_account",
        "uq_ocado_cart_sync_retailer_account",
        ["retailer", "account_id"],
    ),
    "ocado_cart_ledger": (
        "uq_ocado_cart_ledger_account_sku",
        "uq_ocado_cart_ledger_retailer_account_sku",
        ["retailer", "account_id", "sku"],
    ),
}


def _tables_missing_column() -> list[str]:
    inspector = sa.inspect(op.get_bind())
    missing = []
    for table in _TABLES:
        if not inspector.has_table(table):
            # Nothing to alter; create_all builds it from the model already.
            continue
        if not any(column["name"] == _COLUMN for column in inspector.get_columns(table)):
            missing.append(table)
    return missing


def _constraint_names(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table)
        if constraint.get("name")
    }


def upgrade() -> None:
    for table in _tables_missing_column():
        old_name, new_name, columns = _TABLES[table]
        existing = _constraint_names(table)
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column(_COLUMN, sa.String(length=64), nullable=False, server_default="ocado")
            )
            if old_name in existing:
                batch.drop_constraint(old_name, type_="unique")
            batch.create_unique_constraint(new_name, columns)
        op.create_index(f"ix_{table}_{_COLUMN}", table, [_COLUMN])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, (old_name, new_name, _) in _TABLES.items():
        if not inspector.has_table(table):
            continue
        if not any(column["name"] == _COLUMN for column in inspector.get_columns(table)):
            continue
        # Claims against any other shop have nowhere to go in the old shape, and
        # keeping them would make them look like Ocado's. Dropping them only
        # costs the next sync its head start.
        op.execute(sa.text(f"DELETE FROM {table} WHERE {_COLUMN} <> 'ocado'"))
        op.drop_index(f"ix_{table}_{_COLUMN}", table_name=table)
        existing = _constraint_names(table)
        with op.batch_alter_table(table) as batch:
            if new_name in existing:
                batch.drop_constraint(new_name, type_="unique")
            batch.drop_column(_COLUMN)
            batch.create_unique_constraint(
                old_name,
                ["account_id"] if table == "ocado_cart_sync" else ["account_id", "sku"],
            )
