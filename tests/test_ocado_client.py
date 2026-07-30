"""Ocado endpoint and slot-parsing tests, driven by saved live captures.

The fixtures in ``tests/fixtures/ocado/`` are real responses off the site. An
earlier version of this module invented its own payload shapes, which let the
suite stay green while none of the parsers worked against Ocado - so everything
here is asserted against the captures.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.ocado.client import (
    Destination,
    OcadoClient,
    _delta_payload,
    normalize_slots,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ocado"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class RecordingSession:
    """Stands in for OcadoSession, capturing what the client asks for."""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": kwargs.get("params"),
                "json": kwargs.get("json"),
            }
        )
        return httpx.Response(200, json=self.payload, request=httpx.Request(method, url))

    @property
    def last(self):
        return self.calls[-1]


# -- endpoints ------------------------------------------------------------


def test_cart_view_hits_the_captured_endpoint():
    session = RecordingSession(fixture("cart_view"))
    OcadoClient(session).cart_view()

    assert session.last["method"] == "GET"
    assert session.last["url"] == "/api/cart/v2/carts/active/cart-view"
    assert session.last["params"] == {"productGroupingType": "CATEGORIES"}


def test_apply_quantity_posts_a_bare_array_of_deltas():
    session = RecordingSession(fixture("apply_quantity"))
    OcadoClient(session).apply_quantity({"sku-a": 2, "sku-b": -1})

    assert session.last["method"] == "POST"
    assert session.last["url"] == "/api/cart/v1/carts/active/apply-quantity"
    assert session.last["params"] == {"cartProductSorting": "CATEGORIES"}
    # A bare list, not an object - Ocado rejects {"items": [...]}.
    assert session.last["json"] == [
        {"productId": "sku-a", "quantity": 2, "meta": {}},
        {"productId": "sku-b", "quantity": -1, "meta": {}},
    ]


def test_delta_payload_drops_no_op_quantities():
    assert _delta_payload({"keep": 1, "skip": 0}) == [
        {"productId": "keep", "quantity": 1, "meta": {}}
    ]


def test_slots_posts_the_destination_scoped_body():
    session = RecordingSession(fixture("slots"))
    OcadoClient(session).slots(ddid="dd-1", region="rg-1", days=5)

    assert session.last["method"] == "POST"
    assert session.last["url"] == "/api/ecomslots/v2/slots"
    assert session.last["json"] == {
        "deliveryDestinationId": "dd-1",
        "regionId": "rg-1",
        "displayConfiguration": "DELIVERY_METHOD",
        "shippingGroupType": "default home delivery",
        "numberOfDays": 5,
    }


def test_reserve_uses_the_reservation_endpoint_and_field_names():
    session = RecordingSession(fixture("reservation"))
    OcadoClient(session).reserve("slot-9", ddid="dd-1", region="rg-1")

    assert session.last["url"] == "/api/ecomslots/v1/slots/reservation"
    assert session.last["json"] == {
        "regionId": "rg-1",
        "slotId": "slot-9",
        "deliveryDestinationId": "dd-1",
    }


def test_destination_is_resolved_from_the_address_book_and_cached():
    session = RecordingSession(fixture("delivery_addresses"))
    client = OcadoClient(session)

    assert client.destination() == Destination(
        delivery_destination_id="2e74f2bf-73f5-403c-a3a7-50bdf3eca4b1",
        region_id="9138094d-f307-46aa-a62d-86c8bdaeb4b9",
    )
    client.destination()
    assert len(session.calls) == 1  # cached, not refetched


def test_slots_fills_in_a_missing_destination():
    session = RecordingSession(fixture("delivery_addresses"))
    client = OcadoClient(session)
    client.slots()

    body = session.last["json"]
    assert body["deliveryDestinationId"] == "2e74f2bf-73f5-403c-a3a7-50bdf3eca4b1"
    assert body["regionId"] == "9138094d-f307-46aa-a62d-86c8bdaeb4b9"


# -- slot parsing ---------------------------------------------------------


def test_slots_parse_from_the_captured_grid():
    slots = normalize_slots(fixture("slots"))

    assert len(slots) == 64
    assert sum(s.available for s in slots) == 35
    # `attributes` is a list of strings - reading it as a dict silently yields
    # zero eco slots, which is what the previous implementation did.
    assert sum(s.eco for s in slots) == 12


def test_slot_times_come_from_the_nested_window():
    slots = {s.slot_id: s for s in normalize_slots(fixture("slots"))}
    slot = slots["6d6aafb9-1154-481d-9146-490810b085a4"]

    # Times live under `slotWindow`, not at the top level.
    assert slot.start == "2026-07-29T22:30:00+01:00"
    assert slot.end == "2026-07-29T23:30:00+01:00"
    assert slot.day == "2026-07-29"
    assert slot.price == 0.0
    assert slot.available is True
    assert slot.eco is False


def test_green_attribute_marks_an_eco_slot():
    slots = {s.slot_id: s for s in normalize_slots(fixture("slots"))}
    slot = slots["d831850a-c346-4724-9088-5cdc188f56a1"]

    assert slot.eco is True
    assert slot.available is True


def test_slots_without_a_delivery_price_are_unavailable():
    slots = normalize_slots(fixture("slots"))
    unpriced = [s for s in slots if s.price is None]

    assert unpriced, "fixture should contain unavailable slots"
    assert all(s.available is False for s in unpriced)


def test_slots_are_grouped_by_the_grids_local_day():
    slots = normalize_slots(fixture("slots"))
    assert sorted({s.day for s in slots}) == ["2026-07-29", "2026-07-30"]


@pytest.mark.parametrize("payload", [{}, {"carriers": None}, [], "nonsense"])
def test_normalize_slots_tolerates_junk(payload):
    assert normalize_slots(payload) == []
