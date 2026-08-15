"""Reading and changing the Sainsbury's trolley.

Two things here would be silent wrong orders rather than errors if they broke,
which is why they are pinned: a product already in the trolley must be *set*
through its line id rather than posted again (posting adds to it), and a trolley
that splits one product across two lines must read as the sum.
"""
from __future__ import annotations

import pytest

from app.sainsburys.client import (
    BasketError,
    BasketLine,
    SainsburysClient,
    basket_lines,
    basket_quantities,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses) or [FakeResponse()]
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _payload(*items):
    return {"items": list(items)}


def _item(sku, quantity, *, item_uid=None, uom="ea", name=None):
    node = {"product_uid": sku, "quantity": quantity, "uom": uom}
    if item_uid:
        node["item_uid"] = item_uid
    if name:
        node["name"] = name
    return node


# -- reading the trolley ------------------------------------------------------


def test_lines_carry_the_id_a_change_needs():
    (line,) = basket_lines(_payload(_item("sku-a", 2, item_uid="line-1", name="Potatoes")))

    assert (line.sku, line.quantity, line.item_uid) == ("sku-a", 2, "line-1")
    assert line.name == "Potatoes"


def test_a_product_split_across_two_lines_reads_as_the_sum():
    # A multi-buy can split one product in two. Seeing only the smaller line
    # would read as a shortfall and buy the difference a second time.
    payload = _payload(
        _item("sku-a", 2, item_uid="line-1"),
        _item("sku-a", 3, item_uid="line-2"),
    )

    assert basket_quantities(payload) == {"sku-a": 5}


def test_the_first_line_id_is_the_one_kept_for_a_split_product():
    payload = _payload(_item("sku-a", 2, item_uid="line-1"), _item("sku-a", 1, item_uid="line-2"))

    (line,) = basket_lines(payload)
    assert line.item_uid == "line-1"


def test_an_empty_trolley_reads_as_no_quantities():
    assert basket_quantities({"items": []}) == {}
    assert basket_quantities({}) == {}
    assert basket_quantities(None) == {}


def test_lines_are_found_however_the_payload_nests_them():
    nested = {"basket": {"items": [_item("sku-a", 1)]}}
    assert basket_quantities(nested) == {"sku-a": 1}


def test_a_line_without_a_product_id_is_skipped_not_guessed():
    assert basket_lines(_payload({"quantity": 2}, _item("sku-a", 1))) == [
        BasketLine(sku="sku-a", quantity=1)
    ]


# -- changing the trolley -----------------------------------------------------


def test_a_product_not_in_the_trolley_is_posted():
    session = FakeSession()
    client = SainsburysClient(session)

    client.set_quantity(BasketLine(sku="sku-a", quantity=0), 3)

    method, path, kwargs = session.calls[0]
    assert (method, path) == ("POST", "/groceries-api/gol-services/basket/v2/basket/items")
    assert kwargs["json"] == [{"product_uid": "sku-a", "quantity": 3, "uom": "ea"}]


def test_a_product_already_there_is_set_through_its_line_id():
    """A POST would add three more; only a PUT means "make it three"."""
    session = FakeSession()
    client = SainsburysClient(session)

    client.set_quantity(BasketLine(sku="sku-a", quantity=1, item_uid="line-1"), 3)

    method, path, kwargs = session.calls[0]
    assert (method, path) == ("PUT", "/groceries-api/gol-services/basket/v2/basket")
    (item,) = kwargs["json"]["items"]
    assert item["item_uid"] == "line-1"
    assert item["quantity"] == 3


def test_a_removal_is_a_set_to_zero():
    session = FakeSession()
    client = SainsburysClient(session)

    client.set_quantity(BasketLine(sku="sku-a", quantity=2, item_uid="line-1"), 0)

    method, _, kwargs = session.calls[0]
    assert method == "PUT"
    assert kwargs["json"]["items"][0]["quantity"] == 0


def test_removing_something_not_in_the_trolley_asks_nothing():
    session = FakeSession()
    client = SainsburysClient(session)

    client.set_quantity(BasketLine(sku="sku-a", quantity=0), 0)

    assert session.calls == []


def test_updating_without_a_line_id_is_refused_rather_than_guessed():
    client = SainsburysClient(FakeSession())

    with pytest.raises(BasketError, match="without its basket item id"):
        client.update(BasketLine(sku="sku-a", quantity=1))


def test_multibuy_lines_are_added_last():
    # The site sorts them this way: they can reprice the lines around them, so
    # they are applied once the rest of the trolley is settled.
    session = FakeSession()
    client = SainsburysClient(session)

    client.add(
        [
            BasketLine(sku="multi", quantity=1, uom="C62"),
            BasketLine(sku="plain", quantity=1, uom="ea"),
        ]
    )

    assert [item["product_uid"] for item in session.calls[0][2]["json"]] == ["plain", "multi"]


def test_adding_nothing_asks_nothing():
    session = FakeSession()
    assert SainsburysClient(session).add([]) == {}
    assert session.calls == []


def test_a_refusal_carries_the_shop_s_own_reason():
    session = FakeSession(
        FakeResponse(409, {"errors": [{"code": "BASKET_ITEM_QUANTITY_EXCEEDED"}]})
    )
    client = SainsburysClient(session)

    with pytest.raises(BasketError) as caught:
        client.add([BasketLine(sku="sku-a", quantity=99)])

    assert caught.value.status == 409
    assert caught.value.payload["errors"][0]["code"] == "BASKET_ITEM_QUANTITY_EXCEEDED"
