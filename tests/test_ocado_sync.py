"""Planner basket → Ocado cart reconciliation."""
from __future__ import annotations

import json
from pathlib import Path

from app.ocado.sync import (
    CartLedger,
    LedgerLine,
    cart_quantities,
    merge_cart,
    plan_push,
    push_basket,
    refusal_reasons,
)
from app.planner.basket import Basket, BasketLine, Cover, PackChoice, Substitution
from app.planner.index import Pack

FIXTURES = Path(__file__).parent / "fixtures" / "ocado"

CUCUMBER = "9f24fc1d-281f-4c16-b7a0-94004918a720"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, before, *later, response=None):
        self.views = [before, *later]
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


def _ledger(**quantities: int) -> CartLedger:
    """A ledger from a previous sync, as ``sku=quantity``."""
    return CartLedger(
        lines=tuple(
            LedgerLine(sku=sku, quantity=qty, name=sku, ingredient=sku.upper())
            for sku, qty in quantities.items()
        ),
        synced=True,
    )


def _claims(result) -> dict[str, int]:
    return result.ledger.quantities


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

    result = push_basket(client, basket, ledger=_ledger(**{"sku-a": 1, "stale": 3}))

    # Deltas, not absolutes: +1 to reach 2, +1 for the new line, -3 to clear a
    # product the ledger claims and the week no longer wants.
    assert client.deltas == {"sku-a": 1, "sku-b": 1, "stale": -3}
    assert [(l.sku, l.quantity) for l in result.applied] == [("sku-a", 2)]
    assert [(l.sku, l.quantity) for l in result.dropped] == [("sku-b", 1)]
    assert [(l.sku, l.quantity) for l in result.removed] == [("stale", 3)]
    assert result.unmapped == ["mystery"]
    # External lines are bought elsewhere and never pushed.
    assert "manual-1" not in client.deltas


def test_owned_items_are_taken_back_only_if_hf_put_them_there():
    basket = Basket(
        lines=[
            _line("potato", "Potatoes", "sku-a", 2),
            _line("onion", "Onion", "sku-b", 1),
        ],
    )
    client = FakeClient(_cart({"sku-b": 1, "wine": 1}), _cart({"sku-a": 2, "wine": 1}))

    result = push_basket(
        client, basket, ledger=_ledger(**{"sku-b": 1}), owned_item_keys={"onion"}
    )

    # The onion HF bought last week is cleared now you have said you have it.
    # The wine was never HF's to clear.
    assert client.deltas == {"sku-a": 2, "sku-b": -1}
    assert [(l.sku, l.quantity) for l in result.applied] == [("sku-a", 2)]
    assert [(l.sku, l.quantity) for l in result.yours] == [("wine", 1)]
    assert result.dropped == []


def test_pushing_an_already_correct_basket_is_a_no_op():
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])
    client = FakeClient(_cart({"sku-a": 2}), _cart({"sku-a": 2}))

    result = push_basket(client, basket, ledger=_ledger(**{"sku-a": 2}))

    assert client.deltas is None, "nothing to change means no write"
    assert [(l.sku, l.quantity) for l in result.applied] == [("sku-a", 2)]
    assert result.dropped == []


def test_push_is_idempotent_across_repeat_runs():
    """The guard against double-adding: a second push must not stack quantities."""
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])

    first = FakeClient(_cart({}), _cart({"sku-a": 2}))
    result = push_basket(first, basket)
    assert first.deltas == {"sku-a": 2}
    assert _claims(result) == {"sku-a": 2}, "what it put in is what it now claims"

    second = FakeClient(_cart({"sku-a": 2}), _cart({"sku-a": 2}))
    push_basket(second, basket, ledger=result.ledger)
    assert second.deltas is None


# -- sharing the cart with the rest of the week's shopping ----------------
#
# One rule covers all of these: mine = max(0, cart - ledger), and HF only ever
# moves its own share. Each test is one way the cart drifts between syncs.


def test_items_you_add_yourself_are_left_alone():
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])
    client = FakeClient(_cart({"sku-a": 2, "beer": 6}), _cart({"sku-a": 2, "beer": 6}))

    result = push_basket(client, basket, ledger=_ledger(**{"sku-a": 2}))

    assert client.deltas is None
    assert [(l.sku, l.quantity) for l in result.yours] == [("beer", 6)]
    assert "beer" not in _claims(result), "never claim what it did not buy"


def test_hf_adds_on_top_of_a_basket_you_already_started():
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])
    client = FakeClient(_cart({"beer": 6}), _cart({"sku-a": 2, "beer": 6}))

    result = push_basket(client, basket)

    assert client.deltas == {"sku-a": 2}
    assert _claims(result) == {"sku-a": 2}


def test_a_pack_you_already_bought_is_added_to_not_counted_towards():
    """You bought milk for coffee; the week needs milk too, so it buys milk.

    The alternative - treating your carton as covering the recipe - is a
    guess about what you meant it for. Marking the ingredient owned is how you
    say that, and it is a different button.
    """
    basket = Basket(lines=[_line("milk", "Milk", "sku-a", 2)])
    client = FakeClient(_cart({"sku-a": 1}), _cart({"sku-a": 3}))

    result = push_basket(client, basket, ledger=_ledger())

    assert client.deltas == {"sku-a": 2}, "on top of yours, not up to the target"
    assert [(l.sku, l.quantity) for l in result.yours] == [("sku-a", 1)]
    assert _claims(result) == {"sku-a": 2}, "it claims its two, not your three"


def test_deleting_what_hf_bought_puts_it_back():
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])
    client = FakeClient(_cart({}), _cart({"sku-a": 2}))

    result = push_basket(client, basket, ledger=_ledger(**{"sku-a": 2}))

    assert client.deltas == {"sku-a": 2}
    # A deletion and a deliberate cut back look identical from here, so the
    # restore is reported rather than done quietly.
    assert [(l.sku, l.quantity) for l in result.restored] == [("sku-a", 2)]


def test_buying_more_of_an_hf_item_is_left_alone():
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])
    client = FakeClient(_cart({"sku-a": 5}), _cart({"sku-a": 5}))

    result = push_basket(client, basket, ledger=_ledger(**{"sku-a": 2}))

    assert client.deltas is None, "you may want the extra three; HF has its two"
    assert [(l.sku, l.quantity) for l in result.yours] == [("sku-a", 3)]
    assert _claims(result) == {"sku-a": 2}


def test_changing_recipes_removes_only_what_hf_put_in():
    """The hard one: the cart holds HF's items, yours, and an overlap.

    ``sku-b`` is dropped with its recipe; ``shared`` is a product you had also
    bought two of, so only HF's one goes back.
    """
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])
    client = FakeClient(
        _cart({"sku-a": 2, "sku-b": 1, "shared": 3, "beer": 6}),
        _cart({"sku-a": 2, "shared": 2, "beer": 6}),
    )

    result = push_basket(
        client, basket, ledger=_ledger(**{"sku-a": 2, "sku-b": 1, "shared": 1})
    )

    assert client.deltas == {"sku-b": -1, "shared": -1}
    assert [(l.sku, l.quantity) for l in result.removed] == [("shared", 1), ("sku-b", 1)]
    assert [(l.sku, l.quantity) for l in result.yours] == [("beer", 6), ("shared", 2)]
    assert _claims(result) == {"sku-a": 2}


def test_a_first_sync_adopts_matching_packs_rather_than_doubling_them():
    """The migration case: a cart filled by a push from before the ledger existed.

    Without the seed every one of those packs reads as yours and gets bought a
    second time. ``synced`` is what tells this apart from an emptied cart.
    """
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])
    client = FakeClient(_cart({"sku-a": 2}), _cart({"sku-a": 2}))

    result = push_basket(client, basket, ledger=CartLedger(synced=False))

    assert client.deltas is None
    assert _claims(result) == {"sku-a": 2}


def test_an_emptied_cart_is_refilled_rather_than_adopted():
    """After checkout the cart is empty and the ledger claims things that are gone.

    Which is not the first-sync case: ``synced`` is true, so nothing is adopted,
    and the week is simply bought again.
    """
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])
    client = FakeClient(_cart({}), _cart({"sku-a": 2}))

    result = push_basket(client, basket, ledger=_ledger(**{"sku-a": 2}))

    assert client.deltas == {"sku-a": 2}
    assert _claims(result) == {"sku-a": 2}


def test_the_ledger_never_claims_more_than_it_asked_for():
    """The one failure that would delete your shopping.

    The cart gained a fourth pack between the write and the read-back - another
    tab, or Ocado's own doing. Claiming all four would make the next sync
    "remove" one of yours.
    """
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 2)])
    client = FakeClient(_cart({}), _cart({"sku-a": 4}))

    result = push_basket(client, basket)

    assert _claims(result) == {"sku-a": 2}


def test_a_partial_fill_is_recorded_as_what_landed():
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 3)])
    client = FakeClient(_cart({}), _cart({"sku-a": 1}))

    result = push_basket(client, basket)

    assert _claims(result) == {"sku-a": 1}, "claim what is there, not what was asked"
    assert [(l.wanted, l.got) for l in result.dropped] == [(3, 1)]


def test_your_share_survives_a_push_that_ocado_partly_refused():
    """Your two are still yours even when HF's own order came up short."""
    basket = Basket(lines=[_line("potato", "Potatoes", "sku-a", 3)])
    client = FakeClient(_cart({"sku-a": 2}), _cart({"sku-a": 3}))

    result = push_basket(client, basket, ledger=_ledger())

    assert client.deltas == {"sku-a": 3}
    assert [(l.wanted, l.got) for l in result.dropped] == [(3, 1)]
    assert _claims(result) == {"sku-a": 1}


# -- the merge on its own -------------------------------------------------


def test_merge_leaves_a_cart_it_has_no_claim_on_untouched():
    merge = merge_cart({}, {"beer": 6}, {}, synced=True)
    assert merge.deltas == {}
    assert merge.yours == {"beer": 6}


def test_merge_reports_a_restore_only_against_its_own_claim():
    # sku-a: you cut HF's 3 down to 1, so 2 go back and it is reported.
    # sku-b: newly wanted this week, so it is an addition, not a restoration.
    merge = merge_cart({"sku-a": 3}, {"sku-a": 1}, {"sku-a": 3, "sku-b": 1}, synced=True)
    assert merge.deltas == {"sku-a": 2, "sku-b": 1}
    assert merge.restored == {"sku-a": (1, 3)}


def test_merge_seeds_only_up_to_what_the_week_wants():
    # Six in the cart, the week wants two: four of them are yours whatever a
    # pre-ledger push may have done, so the seed takes only two.
    merge = merge_cart({}, {"sku-a": 6}, {"sku-a": 2}, synced=False)
    assert merge.ledger == {"sku-a": 2}
    assert merge.yours == {"sku-a": 4}
    assert merge.deltas == {}


# -- previewing a push ----------------------------------------------------


def test_a_plan_reports_the_same_merge_without_writing_anything():
    basket = Basket(
        lines=[
            _line("potato", "Potatoes", "sku-a", 2),
            _line("onion", "Onion", "sku-c", 1),
        ]
    )
    client = FakeClient(_cart({"sku-a": 1, "sku-b": 1, "beer": 6}))

    plan = plan_push(client, basket, ledger=_ledger(**{"sku-a": 2, "sku-b": 1}))

    assert client.deltas is None, "a plan never touches the cart"
    assert [(l.sku, l.quantity) for l in plan.added] == [("sku-c", 1)]
    assert [(l.sku, l.quantity) for l in plan.removed] == [("sku-b", 1)]
    assert [(l.sku, l.quantity) for l in plan.restored] == [("sku-a", 1)]
    assert [(l.sku, l.quantity) for l in plan.yours] == [("beer", 6)]


def test_a_planned_removal_is_named_from_the_ledger():
    """The ledger carries the ingredient so a removal can say what it was for."""
    basket = Basket(lines=[])
    client = FakeClient(_cart({"sku-b": 1}))

    plan = plan_push(client, basket, ledger=_ledger(**{"sku-b": 1}))

    (removed,) = plan.removed
    assert (removed.name, removed.ingredient) == ("sku-b", "SKU-B")


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


def test_the_retry_merges_against_what_the_first_round_actually_bought():
    """Otherwise the retry reads HF's own additions as yours and buys them again.

    The first round gets the potatoes in and is refused the sesame. By the time
    the second round reads the cart, those two potato packs are in it - and the
    ledger the push *started* from knows nothing about them.
    """
    basket = Basket(
        lines=[
            _line("potato", "Potatoes", "sku-a", 2),
            _line("name:sesame seeds", "Sesame seeds", "mitake", 1),
        ]
    )
    swapped = Basket(
        lines=[
            _line("potato", "Potatoes", "sku-a", 2),
            _line("name:sesame seeds", "Sesame seeds", "saitaku", 1),
        ]
    )
    client = FakeClient(
        _cart({}),
        _cart({"sku-a": 2}),
        _cart({"sku-a": 2}),
        _cart({"sku-a": 2, "saitaku": 1}),
    )

    result = push_basket(client, basket, ledger=_ledger(), recover=lambda skus: swapped)

    assert client.applied[-1] == {"saitaku": 1}, "the potatoes are already bought"
    assert _claims(result) == {"sku-a": 2, "saitaku": 1}


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
