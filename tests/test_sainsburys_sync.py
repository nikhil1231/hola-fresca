"""Planner basket → Sainsbury's trolley reconciliation.

The merge itself is tested against Ocado (same code, :mod:`app.cart.merge`).
What is tested here is the half that is Sainsbury's own and that the merge
cannot check for itself: that a delta becomes the right *absolute* quantity,
that reductions are applied before additions, and that one refused product does
not take the rest of the week's shopping down with it.
"""
from __future__ import annotations

from app.cart.merge import CartLedger, LedgerLine
from app.planner.basket import Basket, BasketLine, Cover, PackChoice
from app.planner.index import Pack
from app.sainsburys import sync
from app.sainsburys.client import BasketError
from app.sainsburys.client import BasketLine as TrolleyLine


class FakeClient:
    """A trolley that actually applies what it is told, so reads after writes agree."""

    def __init__(self, held: dict[str, int] | None = None, refuse: dict[str, BasketError] | None = None):
        self.held = dict(held or {})
        self.refuse = refuse or {}
        #: (sku, goal) in the order they were applied.
        self.applied: list[tuple[str, int]] = []

    def lines(self) -> list[TrolleyLine]:
        return [
            TrolleyLine(sku=sku, quantity=qty, item_uid=f"line-{sku}")
            for sku, qty in sorted(self.held.items())
        ]

    def set_quantity(self, line: TrolleyLine, quantity: int):
        self.applied.append((line.sku, quantity))
        if line.sku in self.refuse:
            raise self.refuse[line.sku]
        if quantity <= 0:
            self.held.pop(line.sku, None)
        else:
            self.held[line.sku] = quantity
        return {}


def _pack(sku, name):
    # ``shop`` has to be Sainsbury's too: a pack is "external" — priced into the
    # week but never pushed to a cart — when its retailer differs from the shop
    # the basket is being built for, so leaving the default here would make every
    # line look like something you go out and buy by hand.
    return Pack(
        sku=sku,
        product_name=name,
        capacity_g=100,
        price=1,
        salvage=0,
        rank=1,
        match_type="exact",
        retailer="sainsburys",
        shop="sainsburys",
    )


def _basket(*lines) -> Basket:
    return Basket(lines=list(lines))


def _line(key, sku, count):
    return BasketLine(
        key=key,
        name=key,
        need_g=100,
        cover=Cover(
            choices=(PackChoice(_pack(sku, sku), count),),
            need_g=100,
            capacity_g=100,
            cost=1,
            leftover_g=0,
            waste_gbp=0,
        ),
    )


def _ledger(**quantities: int) -> CartLedger:
    return CartLedger(
        lines=tuple(
            LedgerLine(sku=sku, quantity=qty, name=sku, ingredient=sku.upper())
            for sku, qty in quantities.items()
        ),
        synced=True,
    )


# -- absolute quantities ------------------------------------------------------


def test_a_delta_is_sent_as_the_quantity_the_line_should_end_at():
    """The bug this guards is silent: +2 sent as "2" under-orders by one."""
    client = FakeClient({"sku-a": 1})

    sync.push_basket(client, _basket(_line("potatoes", "sku-a", 3)), ledger=_ledger(**{"sku-a": 1}))

    assert client.applied == [("sku-a", 3)]
    assert client.held == {"sku-a": 3}


def test_a_product_the_week_no_longer_needs_is_set_to_zero():
    client = FakeClient({"sku-a": 2})

    result = sync.push_basket(client, _basket(), ledger=_ledger(**{"sku-a": 2}))

    assert client.applied == [("sku-a", 0)]
    assert client.held == {}
    assert [line.sku for line in result.removed] == ["sku-a"]


def test_your_own_shopping_is_left_alone():
    # The whole reason the ledger exists: HF claims one, you added two more.
    client = FakeClient({"sku-a": 3})

    result = sync.push_basket(
        client, _basket(_line("potatoes", "sku-a", 1)), ledger=_ledger(**{"sku-a": 1})
    )

    assert client.applied == []
    assert client.held == {"sku-a": 3}
    assert {line.sku: line.quantity for line in result.yours} == {"sku-a": 2}


def test_something_you_deleted_comes_back_and_is_reported():
    client = FakeClient({})

    result = sync.push_basket(
        client, _basket(_line("potatoes", "sku-a", 2)), ledger=_ledger(**{"sku-a": 2})
    )

    assert client.held == {"sku-a": 2}
    assert [line.sku for line in result.restored] == ["sku-a"]


def test_reductions_are_applied_before_additions():
    # A trolley has a size cap; clearing first is what stops a big swap being
    # refused for the space its own leftovers are taking.
    client = FakeClient({"drop-me": 4})

    sync.push_basket(
        client, _basket(_line("new", "add-me", 3)), ledger=_ledger(**{"drop-me": 4})
    )

    assert client.applied == [("drop-me", 0), ("add-me", 3)]


# -- refusals -----------------------------------------------------------------


def test_one_refused_product_does_not_stop_the_rest():
    refusal = BasketError("nope", status=409, payload={"errors": [{"code": "PRODUCT_NOT_ORDERABLE"}]})
    client = FakeClient({}, refuse={"bad": refusal})

    result = sync.push_basket(
        client, _basket(_line("good", "good", 1), _line("bad", "bad", 1))
    )

    assert client.held == {"good": 1}
    assert [line.sku for line in result.applied] == ["good"]
    assert [line.sku for line in result.dropped] == ["bad"]


def test_a_refusal_is_reported_in_the_shop_s_own_words():
    refusal = BasketError("nope", status=409, payload={"errors": [{"code": "PRODUCT_NOT_ORDERABLE"}]})
    client = FakeClient({}, refuse={"bad": refusal})

    result = sync.push_basket(client, _basket(_line("bad", "bad", 1)))

    (dropped,) = result.dropped
    assert dropped.reason == "product not orderable"
    # The shortfall, not the quantity bought - it is the number worth acting on.
    assert (dropped.wanted, dropped.got, dropped.quantity) == (1, 0, 1)


def test_a_refused_product_is_never_claimed_in_the_ledger():
    """An over-claiming ledger is the one failure that deletes your shopping."""
    refusal = BasketError("nope", status=409)
    client = FakeClient({}, refuse={"bad": refusal})

    result = sync.push_basket(client, _basket(_line("bad", "bad", 1)))

    assert result.ledger.quantities == {}


def test_the_ledger_records_what_the_trolley_holds_not_what_was_asked_for():
    client = FakeClient({})

    result = sync.push_basket(client, _basket(_line("potatoes", "sku-a", 2)))

    assert result.ledger.quantities == {"sku-a": 2}
    (line,) = result.ledger.lines
    assert line.ingredient == "potatoes"


# -- substitution -------------------------------------------------------------


def test_a_refusal_is_covered_again_without_the_product():
    refusal = BasketError("nope", status=409)
    client = FakeClient({}, refuse={"bad": refusal})
    recovered = _basket(_line("dinner", "good", 1))

    result = sync.push_basket(
        client, _basket(_line("dinner", "bad", 1)), recover=lambda skus: recovered
    )

    assert result.retried is True
    assert client.held == {"good": 1}
    assert [line.sku for line in result.applied] == ["good"]


def test_recovery_is_attempted_once_not_chased():
    refusal = BasketError("nope", status=409)
    client = FakeClient({}, refuse={"bad": refusal, "also-bad": refusal})
    calls: list[list[str]] = []

    def recover(skus):
        calls.append(skus)
        return _basket(_line("dinner", "also-bad", 1))

    result = sync.push_basket(client, _basket(_line("dinner", "bad", 1)), recover=recover)

    assert len(calls) == 1
    assert [line.sku for line in result.dropped] == ["also-bad"]


# -- planning -----------------------------------------------------------------


def test_the_plan_describes_the_push_without_making_it():
    client = FakeClient({"keep": 1})

    plan = sync.plan_push(
        client, _basket(_line("dinner", "add", 2)), ledger=_ledger(keep=1)
    )

    assert client.applied == []
    assert client.held == {"keep": 1}
    assert [line.sku for line in plan.added] == ["add"]
    assert [line.sku for line in plan.removed] == ["keep"]
