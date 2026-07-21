"""Shared fixtures for the mapping tests: a temp DB seeded with candidates."""
from __future__ import annotations

import pytest

from app.db.models import Product, ProductSearchHit
from app.db.session import init_db, make_engine, make_session_factory


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(tmp_path / "mapping.db")
    init_db(engine)
    return make_session_factory(engine)


def seed_candidates(session, ingredient_key, name, products, *, line_count=100):
    """Insert Product + ProductSearchHit rows for one ingredient.

    ``products`` is a list of dicts with at least ``sku`` and ``name``.
    """
    for rank, p in enumerate(products, start=1):
        product = Product(
            retailer="ocado",
            sku=p["sku"],
            name=p["name"],
            brand=p.get("brand"),
            pack_size_raw=p.get("pack_raw"),
            pack_size_value=p.get("pack_value"),
            pack_size_unit=p.get("pack_unit"),
            price=p.get("price"),
            unit_price=p.get("unit_price"),
            unit_price_basis=p.get("unit_basis"),
            avg_rating=p.get("rating"),
            ratings_count=p.get("count"),
            url=p.get("url"),
        )
        session.add(product)
        session.flush()
        session.add(
            ProductSearchHit(
                product_id=product.id,
                retailer="ocado",
                ingredient_key=ingredient_key,
                search_term=name,
                term_rank=1,
                line_count=line_count,
                sku=p["sku"],
                result_rank=rank,
            )
        )
    session.commit()
