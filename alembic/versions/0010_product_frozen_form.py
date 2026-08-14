"""persist frozen storage form and correct existing mapping tiers

Ocado exposes frozen storage as an explicit product chip (``iconAttributes``),
but the normalized catalogue previously discarded it. Existing category paths
retain the same fact, so they can backfill the new column without a re-scrape.

Revision ID: 0010_product_frozen_form
Revises: 0009_product_base_price
Create Date: 2026-08-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_product_frozen_form"
down_revision = "0009_product_base_price"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    product_columns = _columns("products")
    if not product_columns:
        return
    if "is_frozen" not in product_columns:
        op.add_column(
            "products",
            sa.Column("is_frozen", sa.Integer(), nullable=False, server_default="0"),
        )
        product_columns.add("is_frozen")

    # The historical compatibility fixture has a deliberately minimal products
    # table. Real catalogues have category/raw_json; use whichever facts exist.
    frozen_tests: list[str] = []
    if "category" in product_columns:
        frozen_tests.append("LOWER(COALESCE(category, '')) LIKE '%frozen%'")
    if "raw_json" in product_columns:
        frozen_tests.extend(
            (
                "LOWER(COALESCE(raw_json, '')) LIKE '%\"label\"%\"frozen\"%'",
                "LOWER(COALESCE(raw_json, '')) LIKE '%\"file\"%\"frozen\"%'",
            )
        )
    if frozen_tests:
        op.execute(
            "UPDATE products SET is_frozen = CASE WHEN "
            + " OR ".join(frozen_tests)
            + " THEN 1 ELSE 0 END"
        )

    mapping_product_columns = _columns("ingredient_mapping_products")
    mapping_columns = _columns("ingredient_mappings")
    if not {
        "product_id",
        "mapping_id",
        "match_type",
    }.issubset(mapping_product_columns) or not {"id", "name"}.issubset(mapping_columns):
        return

    # Re-tier existing choices as well as future proposals. An ingredient that
    # explicitly says "frozen" keeps exact matches; otherwise the stored form
    # change makes a fresh option win by default and leaves frozen as a labelled
    # choice in the basket.
    op.execute(
        """
        UPDATE ingredient_mapping_products
        SET match_type = 'form_differs'
        WHERE match_type = 'exact'
          AND EXISTS (
              SELECT 1
              FROM products p
              JOIN ingredient_mappings m
                ON m.id = ingredient_mapping_products.mapping_id
              WHERE p.id = ingredient_mapping_products.product_id
                AND p.is_frozen = 1
                AND LOWER(COALESCE(m.name, '')) NOT LIKE '%frozen%'
          )
        """
    )


def downgrade() -> None:
    # The match-type correction is intentionally retained: once identified as a
    # form difference there is no reliable way to distinguish it from a human
    # decision that predated this migration.
    if "is_frozen" in _columns("products"):
        op.drop_column("products", "is_frozen")
