"""Normalized rows used by the combined Basket/Checkout page."""
from __future__ import annotations

import pytest

from app.api.cart import _checkout_items
from app.cart.merge import CartLedger, LedgerLine
from app.ocado.cart_payload import snapshot
from app.planner.basket import Basket, BasketLine, Cover, PackChoice
from app.planner.index import Pack


def _basket(quantity: int = 2, *, sku: str = "sku-a", external: bool = False) -> Basket:
    pack = Pack(
        sku=sku,
        product_name="Ocado Potatoes 1kg",
        capacity_g=1000,
        price=1.25,
        salvage=0,
        rank=1,
        match_type="exact",
        pack_size_raw="1kg",
        url="https://www.ocado.com/products/sku-a",
        retailer="manual" if external else "ocado",
    )
    cover = Cover(
        choices=(PackChoice(pack=pack, count=quantity),),
        need_g=1000,
        capacity_g=1000 * quantity,
        cost=1.25 * quantity,
        leftover_g=0,
        waste_gbp=0,
    )
    return Basket(lines=[BasketLine(key="potatoes", name="Potatoes", need_g=1000, cover=cover)])


def _ledger(quantity: int | None, *, synced: bool = True, sku: str = "sku-a") -> CartLedger:
    lines = () if quantity is None else (
        LedgerLine(sku=sku, quantity=quantity, name="Old potato name"),
    )
    return CartLedger(lines=lines, synced=synced)


def _cart(quantities: dict[str, int], totals: dict[str, float] | None = None):
    totals = totals or {}
    items = []
    for sku, quantity in quantities.items():
        item = {"productId": sku, "quantity": quantity, "itemType": "BasketItem"}
        if sku in totals:
            item["totalPrices"] = {
                "finalPrice": {"currency": "GBP", "amount": str(totals[sku])}
            }
        items.append(item)
    return snapshot(
        {"checkoutGroups": {"assignedCheckoutGroups": [{"itemGroups": [{"items": items}]}]}}
    )


@pytest.mark.parametrize(
    ("basket_quantity", "ledger", "cart_quantity", "expected"),
    [
        (2, _ledger(None, synced=False), 0, "not_synced"),
        (2, _ledger(2), 2, "synced"),
        (2, _ledger(1), 1, "changed"),
        (2, _ledger(2), 1, "deleted"),
        (2, _ledger(2), 3, "extra"),
        # A plan edit explains the lower cart quantity, so "changed" wins over
        # guessing that the retailer quantity was manually reduced.
        (1, _ledger(2), 1, "changed"),
    ],
)
def test_checkout_statuses(factory, basket_quantity, ledger, cart_quantity, expected):
    rows = _checkout_items(
        factory,
        "ocado",
        _basket(basket_quantity),
        ledger,
        _cart({"sku-a": cart_quantity} if cart_quantity else {}),
        owned_item_keys=set(),
    )

    assert [row.status for row in rows] == [expected]


def test_live_promotional_total_wins_over_planned_cost(factory):
    (row,) = _checkout_items(
        factory,
        "ocado",
        _basket(2),
        _ledger(2),
        _cart({"sku-a": 2}, {"sku-a": 1.75}),
        owned_item_keys=set(),
    )

    assert (row.cost, row.cost_source) == (1.75, "live")
    assert (row.name, row.pack_size_raw, row.url) == (
        "Ocado Potatoes 1kg",
        "1kg",
        "https://www.ocado.com/products/sku-a",
    )


def test_unsynced_product_uses_the_planned_line_total(factory):
    (row,) = _checkout_items(
        factory,
        "ocado",
        _basket(2),
        _ledger(None, synced=False),
        _cart({}),
        owned_item_keys=set(),
    )

    assert (row.cost, row.cost_source) == (2.5, "planned")


def test_an_already_removed_historical_line_is_not_rendered(factory):
    rows = _checkout_items(
        factory,
        "ocado",
        Basket(),
        _ledger(2),
        _cart({"personal-only": 4}),
        owned_item_keys=set(),
    )

    assert rows == []


def test_pending_removal_stays_visible_without_inventing_a_zero_price(factory):
    (row,) = _checkout_items(
        factory,
        "ocado",
        Basket(),
        _ledger(2),
        _cart({"sku-a": 2}),
        owned_item_keys=set(),
    )

    assert (row.sku, row.name, row.desired_quantity, row.status) == (
        "sku-a",
        "Old potato name",
        0,
        "changed",
    )
    assert (row.cost, row.cost_source) == (None, "planned")


def test_personal_external_and_owned_products_are_excluded(factory):
    personal_cart = _cart({"personal-only": 4})
    assert _checkout_items(
        factory, "ocado", Basket(), _ledger(None), personal_cart, owned_item_keys=set()
    ) == []
    assert _checkout_items(
        factory, "ocado", _basket(external=True), _ledger(None), personal_cart, owned_item_keys=set()
    ) == []
    assert _checkout_items(
        factory, "ocado", _basket(), _ledger(None), personal_cart, owned_item_keys={"potatoes"}
    ) == []
