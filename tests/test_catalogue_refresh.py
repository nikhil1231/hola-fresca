"""The retailer-neutral half of a live refresh: what gets written back.

How each shop is *asked* is its adapter's business and is covered per retailer.
What these pin down is the part a basket depends on whichever shop it is priced
at — that today's price and the shelf price behind it move together, and that a
shop with no live read says so instead of silently pricing from a stale scrape.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import catalogue
from app.db.models import Product
from app.scraper.products.base import ProductStatus

pytest_plugins = ["tests.conftest"]


def _seed(factory, **columns):
    with factory() as session:
        session.add(
            Product(
                retailer="sainsburys",
                sku="8190444",
                name="La Espanola Olive Oil 500ml",
                **columns,
            )
        )
        session.commit()


def _row(factory):
    with factory() as session:
        return session.scalar(select(Product).where(Product.sku == "8190444"))


def _refresh(factory, status):
    return catalogue.refresh_stock(
        factory,
        [status.sku],
        retailer="sainsburys",
        fetch=lambda skus: {status.sku: status},
    )


def test_a_promotion_writes_both_what_you_pay_and_the_shelf_price(factory):
    _seed(factory, price=7.0, unit_price=14.0, unit_price_basis="l", in_stock=1)

    result = _refresh(
        factory,
        ProductStatus(
            sku="8190444",
            available=True,
            price=3.5,
            base_price=7.0,
            unit_price=7.0,
            unit_price_basis="l",
            base_unit_price=14.0,
        ),
    )

    row = _row(factory)
    assert (row.price, row.base_price) == (3.5, 7.0)
    assert (row.unit_price, row.base_unit_price) == (7.0, 14.0)
    assert result.repriced == ["8190444"]
    assert row.stock_checked_at is not None


def test_a_promotion_that_has_ended_clears_the_shelf_price_it_left_behind(factory):
    # The shop simply stops mentioning an expired offer. Leaving the old base
    # behind would keep advertising a discount off a price nobody is charging —
    # and the review page would keep colouring its pills from it.
    _seed(factory, price=3.5, base_price=7.0, unit_price=7.0, base_unit_price=14.0,
          unit_price_basis="l", in_stock=1)

    _refresh(
        factory,
        ProductStatus(
            sku="8190444",
            available=True,
            price=7.0,
            base_price=None,
            unit_price=14.0,
            unit_price_basis="l",
            base_unit_price=None,
        ),
    )

    row = _row(factory)
    assert (row.price, row.base_price) == (7.0, None)
    assert (row.unit_price, row.base_unit_price) == (14.0, None)


def test_a_shop_that_states_no_price_leaves_the_cached_one_alone(factory):
    # "Did not say" is not "is free". The stock answer is still worth keeping.
    _seed(factory, price=3.5, base_price=7.0, unit_price=7.0, base_unit_price=14.0,
          unit_price_basis="l", in_stock=1)

    result = _refresh(factory, ProductStatus(sku="8190444", available=False))

    row = _row(factory)
    assert (row.price, row.base_price) == (3.5, 7.0)
    assert (row.unit_price, row.base_unit_price) == (7.0, 14.0)
    assert (row.in_stock, result.sold_out) == (0, ["8190444"])


def test_every_catalogued_retailer_can_be_refreshed():
    # A shop the planner can price but never re-read would quietly serve a
    # month-old price behind a basket total.
    from app.retailers import RETAILERS

    for retailer in RETAILERS:
        if retailer.catalogued:
            assert catalogue.supports_live_status(retailer.id), retailer.id


def test_an_unknown_shop_is_an_error_not_an_empty_answer(factory):
    with pytest.raises(KeyError):
        catalogue.refresh_stock(factory, ["x"], retailer="waitrose")
