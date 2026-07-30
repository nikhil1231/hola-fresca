"""Live stock reads, and what they write back into the catalogue.

``products.in_stock`` is a scrape cache. These tests pin down what makes it safe
to plan against: that a fresh read overwrites it, that a read which never
happened is not mistaken for one saying "sold out", and that one dead product id
cannot take the whole check down with it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import Product
from app.ocado import availability as A

MITAKE = "d0f6968b-37c7-4f82-baff-f2851f35bec2"
SAITAKU = "53459fec-154f-4443-b781-a5fa055a5ed1"
RETIRED = "00000000-0000-0000-0000-000000000000"


def _uuid(n: int) -> str:
    return f"{n:08x}-0000-0000-0000-000000000000"


class FakeSession:
    """Stands in for OcadoSession, recording the batches it was asked for.

    ``poison`` reproduces the endpoint's real failure mode: a single id it does
    not recognise makes it answer 500 for the entire batch that id arrived in.
    """

    def __init__(self, products, *, poison=()):
        self.products = products
        self.batches: list[list[str]] = []
        self.poison = set(poison)

    def request(self, method, path, *, json=None, **kwargs):
        assert method == "PUT" and path == A.PRODUCTS_PATH
        self.batches.append(list(json))
        if self.poison & set(json):
            return FakeResponse(None, status=500)
        return FakeResponse(
            {"products": [p for p in self.products if p["productId"] in set(json)]}
        )


class FakeResponse:
    def __init__(self, payload, *, status=200):
        self.payload = payload
        self.status = status
        self.content = b"{}"

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self.payload


def _product(sku, *, available=True, price="1.30", name="Sesame Seeds"):
    return {
        "productId": sku,
        "name": name,
        "price": {"amount": price, "currency": "GBP"},
        "available": available,
    }


def _seed(factory, rows):
    with factory() as session:
        for sku, price, in_stock in rows:
            session.add(
                Product(retailer="ocado", sku=sku, name=sku, price=price, in_stock=in_stock)
            )
        session.commit()


def _stock(factory, sku):
    with factory() as session:
        row = session.scalar(select(Product).where(Product.sku == sku))
        return row.in_stock, row.price, row.stock_checked_at


# -- reading -------------------------------------------------------------


def test_statuses_come_back_keyed_by_sku():
    session = FakeSession([_product(MITAKE), _product(SAITAKU, available=False)])

    statuses = A.fetch_statuses([MITAKE, SAITAKU], session=session)

    assert statuses[MITAKE].available is True
    assert statuses[MITAKE].price == 1.30
    assert statuses[SAITAKU].available is False


def test_an_id_ocado_will_not_talk_about_counts_as_unavailable():
    """A delisted product reads exactly like a sold-out one from the basket's view."""
    session = FakeSession([_product(MITAKE)])

    statuses = A.fetch_statuses([MITAKE, RETIRED], session=session)

    assert statuses[RETIRED].available is False
    assert statuses[RETIRED].unlisted is True


def test_another_retailers_sku_is_never_sent_or_judged():
    """``manual:truffle-zest`` is not Ocado's to have an opinion about."""
    session = FakeSession([_product(MITAKE)])

    statuses = A.fetch_statuses([MITAKE, "manual:truffle-zest"], session=session)

    assert "manual:truffle-zest" not in statuses
    assert session.batches == [[MITAKE]]


def test_skus_are_asked_for_in_batches_and_only_once_each():
    skus = [_uuid(i) for i in range(120)]
    session = FakeSession([_product(sku) for sku in skus])

    A.fetch_statuses(skus + [skus[0]], session=session)

    assert [len(batch) for batch in session.batches] == [50, 50, 20]


def test_one_dead_id_does_not_cost_the_batch_its_answer():
    """Ocado 500s the whole batch over a single id it has retired."""
    skus = [_uuid(i) for i in range(1, 9)] + [RETIRED]
    session = FakeSession([_product(sku) for sku in skus[:-1]], poison={RETIRED})

    statuses = A.fetch_statuses(skus, session=session)

    assert all(statuses[sku].available for sku in skus[:-1])
    assert statuses[RETIRED].unlisted is True
    assert len(session.batches) < 12, "bisected, not retried one id at a time"


# -- writing back --------------------------------------------------------


def test_a_refresh_overwrites_stock_price_and_the_timestamp(factory):
    _seed(factory, [(MITAKE, 1.30, 1), (SAITAKU, 2.20, 0)])
    session = FakeSession(
        [
            _product(MITAKE, available=False, price="1.30"),
            _product(SAITAKU, available=True, price="2.50"),
        ]
    )

    result = A.refresh_stock(factory, [MITAKE, SAITAKU], session=session)

    assert result.sold_out == [MITAKE]
    assert result.restocked == [SAITAKU]
    assert result.repriced == [SAITAKU], "a stale price picks the wrong pack, too"
    assert _stock(factory, MITAKE)[0] == 0
    assert _stock(factory, SAITAKU)[:2] == (1, 2.50)
    assert _stock(factory, MITAKE)[2] is not None


def test_a_never_checked_product_is_not_reported_as_newly_sold_out(factory):
    """NULL means nobody has asked yet, which is not the same as "in stock"."""
    _seed(factory, [(MITAKE, 1.00, None)])
    session = FakeSession([_product(MITAKE, available=False)])

    result = A.refresh_stock(factory, [MITAKE], session=session)

    assert result.sold_out == [MITAKE]
    assert result.restocked == []


def test_a_push_refusal_is_believed_over_the_catalogue(factory):
    _seed(factory, [(MITAKE, 1.30, 1)])

    assert A.mark_unavailable(factory, [MITAKE, RETIRED]) == 1
    assert _stock(factory, MITAKE)[0] == 0


def test_a_shop_that_answers_nothing_is_an_outage_not_an_empty_warehouse():
    """Otherwise a dropped connection writes "sold out" across the catalogue."""
    session = FakeSession([], poison={MITAKE, SAITAKU})

    with pytest.raises(RuntimeError, match="none of the stock requests"):
        A.fetch_statuses([MITAKE, SAITAKU], session=session)


def test_too_many_unanswered_ids_are_left_alone_rather_than_written_off(factory):
    skus = [_uuid(i) for i in range(1, 11)]
    _seed(factory, [(sku, 1.0, 1) for sku in skus])
    # Half the batch answers; the rest 500s, one bisected id at a time.
    session = FakeSession([_product(sku) for sku in skus[:5]], poison=set(skus[5:]))

    result = A.refresh_stock(factory, skus, session=session)

    assert result.checked == 5, "only the half Ocado actually spoke about"
    assert result.sold_out == []
    assert all(_stock(factory, sku)[0] == 1 for sku in skus[5:])


@pytest.mark.parametrize("payload", [{}, [], {"products": "nonsense"}, "junk"])
def test_odd_payloads_yield_nothing_rather_than_raising(payload):
    assert A._product_nodes(payload) == []
