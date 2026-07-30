"""Planner basket → Ocado cart reconciliation."""
from __future__ import annotations

import json
from pathlib import Path

from app.ocado.sync import cart_quantities, push_basket
from app.planner.basket import Basket, BasketLine, Cover, PackChoice
from app.planner.index import Pack

FIXTURES = Path(__file__).parent / "fixtures" / "ocado"

CUCUMBER = "9f24fc1d-281f-4c16-b7a0-94004918a720"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, before, after):
        self.views = [before, after]
        self.deltas = None

    def cart_view(self):
        return self.views.pop(0)

    def apply_quantity(self, deltas):
        self.deltas = deltas
        return {"ok": True}


def _pack(sku, name, *, external=False):
    return Pack(
        sku=sku,
        product_name=name,
        capacity_g=100,
        price=1,
        salvage=0,
        rank=1,
        match_type="exact",
        retailer="manual" if external else "ocado",
    )


def _cover(*choices):
    return Cover(choices=choices, need_g=100, capacity_g=100, cost=1, leftover_g=0, waste_gbp=0)


def _line(key, name, sku, count, *, external=False):
    return BasketLine(
        key=key,
        name=name,
        need_g=100,
        cover=_cover(PackChoice(_pack(sku, name, external=external), count)),
    )


def _cart(quantities: dict[str, int]):
    """A cart-view-shaped payload with the real nesting."""
    return {
        "checkoutGroups": {
            "assignedCheckoutGroups": [
                {
                    "itemGroups": [
                        {
                            "items": [
                                {"productId": sku, "quantity": qty, "itemType": "BasketItem"}
                                for sku, qty in quantities.items()
                            ]
                        }
                    ]
                }
            ]
        }
    }


# -- reading quantities out of the real payloads --------------------------


def test_quantities_read_from_a_real_cart_view():
    # The lines are nested under checkoutGroups; a naive top-level search finds
    # nothing here, which reads as "basket empty" and makes every push re-add.
    assert cart_quantities(fixture("cart_view")) == {
        CUCUMBER: 3,
        "fc5f2e19-02e8-4a57-b804-1a948d4fcc7c": 1,
        "f1bfad9a-36e5-410c-a158-81cb353ba67d": 1,
    }


def test_quantities_read_from_a_real_checkout_walk():
    assert cart_quantities(fixture("checkout_walk")) == {CUCUMBER: 2}


def test_quantities_read_from_a_real_apply_quantity_response():
    assert cart_quantities(fixture("apply_quantity")) == {CUCUMBER: 3}


def test_quantities_tolerate_an_empty_or_odd_payload():
    assert cart_quantities({}) == {}
    assert cart_quantities({"items": [{"productId": "a"}]}) == {}
    assert cart_quantities("nonsense") == {}


# -- reconciliation -------------------------------------------------------


def test_push_sends_deltas_and_detects_dropped_products():
    basket = Basket(
        lines=[
            _line("potato", "Potatoes", "sku-a", 2),
            _line("onion", "Onion", "sku-b", 1),
            _line("spice", "Spice", "manual-1", 1, external=True),
        ],
        unmapped=["mystery"],
    )
    client = FakeClient(
        _cart({"sku-a": 1, "stale": 3}),
        _cart({"sku-a": 2}),
    )

    result = push_basket(client, basket)

    # Deltas, not absolutes: +1 to reach 2, +1 for the new line, -3 to clear.
    assert client.deltas == {"sku-a": 1, "sku-b": 1, "stale": -3}
    assert [(l.sku, l.quantity) for l in result.applied] == [("sku-a", 2)]
    assert [(l.sku, l.quantity) for l in result.dropped] == [("sku-b", 1)]
    assert result.unmapped == ["mystery"]
    # External lines are bought elsewhere and never pushed.
    assert "manual-1" not in client.deltas


def test_push_skips_owned_items():
    basket = Basket(
        lines=[
            _line("potato", "Potatoes", "sku-a", 2),
            _line("onion", "Onion", "sku-b", 1),
        ],
    )
    client = FakeClient(_cart({"sku-b": 1}), _cart({"sku-a": 2}))

    result = push_basket(client, basket, owned_item_keys={"onion"})

    # Already in the cupboard, so it is cleared from the cart rather than kept.
    assert client.deltas == {"sku-a": 2, "sku-b": -1}
    assert [(l.sku, l.quantity) for l in result.applied] == [("sku-a", 2)]
    assert result.dropped == []


def test_pushing_an_already_correct_basket_is_a_no_op():
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])
    client = FakeClient(_cart({"sku-a": 2}), _cart({"sku-a": 2}))

    result = push_basket(client, basket)

    assert client.deltas is None, "nothing to change means no write"
    assert [(l.sku, l.quantity) for l in result.applied] == [("sku-a", 2)]
    assert result.dropped == []


def test_push_is_idempotent_across_repeat_runs():
    """The guard against double-adding: a second push must not stack quantities."""
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])

    first = FakeClient(_cart({}), _cart({"sku-a": 2}))
    push_basket(first, basket)
    assert first.deltas == {"sku-a": 2}

    second = FakeClient(_cart({"sku-a": 2}), _cart({"sku-a": 2}))
    push_basket(second, basket)
    assert second.deltas is None


def test_lines_without_a_cover_are_reported_unmapped():
    basket = Basket(lines=[BasketLine(key="ghost", name="Ghost", need_g=100, cover=None)])
    client = FakeClient(_cart({}), _cart({}))

    result = push_basket(client, basket)

    assert result.unmapped == ["Ghost"]
    assert client.deltas is None
