"""Planner basket → Ocado cart reconciliation."""
from __future__ import annotations

import json
from pathlib import Path

from app.ocado.sync import cart_quantities, push_basket, refusal_reasons
from app.planner.basket import Basket, BasketLine, Cover, PackChoice, Substitution
from app.planner.index import Pack

FIXTURES = Path(__file__).parent / "fixtures" / "ocado"

CUCUMBER = "9f24fc1d-281f-4c16-b7a0-94004918a720"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, before, after, *later, response=None):
        self.views = [before, after, *later]
        self.deltas = None
        self.applied = []
        self.response = response or {"ok": True}

    def cart_view(self):
        # The last view stands in for every later read, so a test only has to
        # spell out the carts whose contents it is actually about.
        return self.views.pop(0) if len(self.views) > 1 else self.views[0]

    def apply_quantity(self, deltas):
        self.deltas = deltas
        self.applied.append(deltas)
        return self.response


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


def _cover(*choices, substitution=None):
    return Cover(
        choices=choices,
        need_g=100,
        capacity_g=100,
        cost=1,
        leftover_g=0,
        waste_gbp=0,
        substitution=substitution,
    )


def _line(key, name, sku, count, *, external=False, substitution=None):
    return BasketLine(
        key=key,
        name=name,
        need_g=100,
        cover=_cover(
            PackChoice(_pack(sku, name, external=external), count), substitution=substitution
        ),
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


# -- when Ocado refuses something -----------------------------------------


def test_a_drop_is_reported_against_its_ingredient_and_its_reason():
    """"Dropped: Mitake Irigoma Shiro" is a brand name and a shrug.

    What the week is missing is *sesame seeds*, and Ocado said why in the same
    breath as it refused - both of which used to be thrown away.
    """
    basket = Basket(lines=[_line("name:sesame seeds", "Sesame seeds", "mitake", 2)])
    client = FakeClient(
        _cart({}),
        _cart({"mitake": 1}),
        response={"unavailableData": [{"productId": "mitake", "reason": "OUT_OF_STOCK"}]},
    )

    result = push_basket(client, basket)

    (dropped,) = result.dropped
    assert dropped.ingredient == "Sesame seeds"
    assert dropped.ingredient_key == "name:sesame seeds"
    assert (dropped.wanted, dropped.got) == (2, 1), "a partial fill is not a total failure"
    assert dropped.quantity == 1, "the shortfall, not the quantity in the cart"
    assert dropped.reason == "out of stock"


def test_a_refused_product_is_re_covered_and_pushed_again():
    basket = Basket(lines=[_line("name:sesame seeds", "Sesame seeds", "mitake", 1)])
    swapped = Basket(
        lines=[
            _line(
                "name:sesame seeds",
                "Sesame seeds",
                "saitaku",
                1,
                substitution=Substitution(
                    displaced=("Mitake",),
                    displaced_skus=("mitake",),
                    baseline_cost=1.30,
                    cost_delta=0.90,
                ),
            )
        ]
    )
    client = FakeClient(_cart({}), _cart({}), _cart({}), _cart({"saitaku": 1}))
    recovered = []

    result = push_basket(
        client,
        basket,
        recover=lambda skus: (recovered.extend(skus), swapped)[1],
    )

    assert recovered == ["mitake"], "the cart's refusal is what drives the re-cover"
    assert [(l.sku, l.quantity) for l in result.applied] == [("saitaku", 1)]
    assert result.dropped == []
    assert result.retried is True
    assert result.basket is swapped, "the caller reports the swap it actually pushed"
    # Both rounds are reported: the refused +1 was still asked for, and saying so
    # is what makes the deltas match the requests the cart actually received.
    assert result.deltas == {"mitake": 1, "saitaku": 1}


def test_a_second_refusal_is_reported_rather_than_chased():
    basket = Basket(lines=[_line("name:sesame seeds", "Sesame seeds", "mitake", 1)])
    swapped = Basket(lines=[_line("name:sesame seeds", "Sesame seeds", "saitaku", 1)])
    client = FakeClient(_cart({}), _cart({}))

    result = push_basket(client, basket, recover=lambda skus: swapped)

    assert len(client.applied) == 2, "one retry, not a loop"
    assert [l.sku for l in result.dropped] == ["saitaku"]


def test_refusals_are_read_out_of_the_carts_own_answer():
    assert refusal_reasons(
        {
            "unavailableData": [{"productId": "a"}],
            "limitedItems": [{"productId": "b", "message": "Only 2 available"}],
            "basketUpdateResult": {
                "itemGroups": [{"items": [{"productId": "c", "maxQuantityReached": True}]}]
            },
        }
    ) == {"a": "unavailable", "b": "only 2 available", "c": "maximum quantity reached"}
    assert refusal_reasons(fixture("apply_quantity")) == {}, "a clean push blames nobody"


def test_lines_without_a_cover_are_reported_unmapped():
    basket = Basket(lines=[BasketLine(key="ghost", name="Ghost", need_g=100, cover=None)])
    client = FakeClient(_cart({}), _cart({}))

    result = push_basket(client, basket)

    assert result.unmapped == ["Ghost"]
    assert client.deltas is None
