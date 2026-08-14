"""Share ingredient aliases across retailer mapping rows.

Product acceptance is retailer-specific, but an alias is a fact about the recipe
ingredient: ``basil pesto`` and ``pesto`` do not stop being synonyms when the
shop changes.  Earlier rows stored ``alias_of`` per retailer and the first
Sainsbury's mapping pass consequently created its aliases as ordinary proposals.

Replicate every existing alias to every retailer already represented in the
mapping table and materialise missing alias rows.  The application write path
keeps these copies synchronized for retailers added after this migration.

Revision ID: 0006_share_ingredient_aliases
Revises: 0005_mapping_product_llm_rank
Create Date: 2026-08-14
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0006_share_ingredient_aliases"
down_revision = "0005_mapping_product_llm_rank"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Very old databases relied on runtime column patching and may predate the
    # alias column itself (the pre-accounts migration fixture has this shape).
    # The data repair below must be safe on that supported upgrade path too.
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("ingredient_mappings")
    }
    if "alias_of" not in columns:
        op.add_column(
            "ingredient_mappings", sa.Column("alias_of", sa.Text(), nullable=True)
        )

    # Reflect and copy complete source rows rather than spelling out columns.
    # Historical databases differ in which client-defaulted NOT NULL columns
    # they carry; cloning the row satisfies all of them without fabricating
    # schema-specific defaults.
    bind = op.get_bind()
    table = sa.Table("ingredient_mappings", sa.MetaData(), autoload_with=bind)
    rows = list(
        bind.execute(
            sa.select(table)
            .where(table.c.alias_of.is_not(None))
            .order_by(table.c.id)
        ).mappings()
    )
    aliases = {}
    for row in rows:
        aliases.setdefault(row["ingredient_key"], row)
    retailers = set(bind.execute(sa.select(table.c.retailer).distinct()).scalars())
    existing = set(
        bind.execute(sa.select(table.c.retailer, table.c.ingredient_key)).all()
    )
    now = datetime.now()
    for retailer in retailers:
        for key, source in aliases.items():
            if (retailer, key) in existing:
                continue
            values = {
                column.name: source[column.name]
                for column in table.columns
                if column.name != "id"
            }
            values.update(
                retailer=retailer,
                status="alias",
                alias_of=source["alias_of"],
                updated_at=now,
            )
            if "decided_by" in table.c:
                values["decided_by"] = "human"
            bind.execute(table.insert().values(**values))

    # Existing Sainsbury's proposals for those keys become aliases too.
    for key, source in aliases.items():
        values = {
            "alias_of": source["alias_of"],
            "status": "alias",
            "updated_at": now,
        }
        if "decided_by" in table.c:
            values["decided_by"] = "human"
        bind.execute(
            table.update().where(table.c.ingredient_key == key).values(**values)
        )


def downgrade() -> None:
    # This is a data-consistency repair. Removing shared aliases on downgrade
    # would discard valid curation, so there is intentionally no reverse write.
    pass
