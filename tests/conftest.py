"""Shared fixtures for the mapping tests: a temp DB seeded with candidates."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import config
from app.db.models import Product, ProductSearchHit, User
from app.db.session import init_db, make_engine, make_session_factory

_REAL_DB_PATH = config.DB_PATH.resolve()


@pytest.fixture(autouse=True)
def isolated_legacy_retailer_config(monkeypatch, tmp_path):
    """Keep real account config and the real database out of every test."""
    from app.api import deps
    from app.ocado import session as ocado_session

    account = config.OcadoAccountConfig(id="default", label="Ocado")
    real_frequency_csv = config.DATA_DIR / "ingredient_frequency.csv"
    test_data_dir = tmp_path / "data"
    test_data_dir.mkdir()
    if real_frequency_csv.exists():
        (test_data_dir / "ingredient_frequency.csv").symlink_to(real_frequency_csv)
    real_db_url = config.db_url
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "default.db")
    monkeypatch.setattr(config, "DATA_DIR", test_data_dir)

    def isolated_db_url(path=None):
        resolved = (path or config.DB_PATH).resolve()
        if resolved == _REAL_DB_PATH:
            raise RuntimeError("a test attempted to open the real application database")
        return real_db_url(resolved)

    monkeypatch.setattr(config, "db_url", isolated_db_url)
    monkeypatch.setattr(config, "OCADO_ACCOUNTS", (account,))
    monkeypatch.setattr(config, "OCADO_ACCOUNT_IDS", (account.id,))
    monkeypatch.setattr(config, "DEFAULT_OCADO_ACCOUNT_ID", account.id)
    monkeypatch.setattr(config, "OCADO_EMAIL", None)
    monkeypatch.setattr(config, "OCADO_PASSWORD", None)
    deps._session_factory.cache_clear()
    ocado_session._default_account_factory.cache_clear()
    ocado_session._RUNTIMES.clear()
    yield
    cache_clear = getattr(deps._session_factory, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()
    ocado_session._default_account_factory.cache_clear()
    ocado_session._RUNTIMES.clear()


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
