"""Shared fixtures for the mapping tests: a temp DB seeded with candidates."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Product, ProductSearchHit, User
from app.db.session import init_db, make_engine, make_session_factory


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(tmp_path / "mapping.db")
    init_db(engine)
    return make_session_factory(engine)


def user_id(session) -> int:
    """The bootstrap account's id, which owns everything personal in a test DB.

    ``init_db`` creates it, so this is always available. Looked up rather than
    written as ``1`` so a test does not quietly depend on the autoincrement.
    """
    return session.scalar(select(User.id).order_by(User.id).limit(1))


def seed_candidates(
    session, ingredient_key, name, products, *, line_count=100, retailer="ocado"
):
    """Insert Product + ProductSearchHit rows for one ingredient.

    ``products`` is a list of dicts with at least ``sku`` and ``name``.
    ``retailer`` is which shop's catalogue they belong to — the same ingredient
    can be seeded twice, once per shop, which is what a multi-retailer test needs.
    """
    for rank, p in enumerate(products, start=1):
        # The same product can be a candidate for several ingredients (which is
        # exactly the alias case), so reuse an existing row rather than
        # re-inserting and tripping the retailer+sku unique constraint.
        product = session.scalar(
            select(Product).where(Product.retailer == retailer, Product.sku == p["sku"])
        )
        if product is not None:
            session.add(
                ProductSearchHit(
                    product_id=product.id,
                    retailer=retailer,
                    ingredient_key=ingredient_key,
                    search_term=name,
                    term_rank=1,
                    line_count=line_count,
                    sku=p["sku"],
                    result_rank=rank,
                )
            )
            continue
        product = Product(
            retailer=retailer,
            sku=p["sku"],
            name=p["name"],
            brand=p.get("brand"),
            pack_size_raw=p.get("pack_raw"),
            pack_size_value=p.get("pack_value"),
            pack_size_unit=p.get("pack_unit"),
            price=p.get("price"),
            base_price=p.get("base_price"),
            unit_price=p.get("unit_price"),
            base_unit_price=p.get("base_unit_price"),
            unit_price_basis=p.get("unit_basis"),
            category=p.get("category"),
            is_frozen=p.get("is_frozen", False),
            avg_rating=p.get("rating"),
            ratings_count=p.get("count"),
            url=p.get("url"),
        )
        session.add(product)
        session.flush()
        session.add(
            ProductSearchHit(
                product_id=product.id,
                retailer=retailer,
                ingredient_key=ingredient_key,
                search_term=name,
                term_rank=1,
                line_count=line_count,
                sku=p["sku"],
                result_rank=rank,
            )
        )
    session.commit()
