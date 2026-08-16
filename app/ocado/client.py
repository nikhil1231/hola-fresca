"""Typed, thin Ocado API wrappers.

Every path and body here is taken from traffic captured off the live site (see
``data/requests/ocado/``), not from guesswork. Two shapes are easy to get wrong:

* ``apply-quantity`` takes a **bare JSON array** of deltas, not an object, and the
  quantity is a *delta* (+1/-1), never an absolute.
* a slot's availability lives in ``attributes`` (a **list** of strings), and its
  times are nested under ``slotWindow``. An unavailable slot has an empty
  ``attributes`` and no ``deliveryPrice`` key at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.ocado.session import OcadoSession

CART_VIEW_PATH = "/api/cart/v2/carts/active/cart-view"
APPLY_QUANTITY_PATH = "/api/cart/v1/carts/active/apply-quantity"
CHECKOUT_WALK_PATH = "/api/cart/v1/carts/active/checkout-walk"
DELIVERY_ADDRESSES_PATH = "/api/ecomdeliverydestinations/v4/delivery-addresses"
SLOTS_PATH = "/api/ecomslots/v2/slots"
RESERVATION_PATH = "/api/ecomslots/v1/slots/reservation"

#: Ocado's own web client sends this for a standard home-delivery basket.
DEFAULT_SHIPPING_GROUP = "default home delivery"


@dataclass(frozen=True, slots=True)
class Slot:
    slot_id: str
    start: str | None = None
    end: str | None = None
    day: str | None = None
    available: bool = False
    eco: bool = False
    price: float | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Destination:
    """The address/region pair every slot call has to be scoped to."""

    delivery_destination_id: str
    region_id: str


class OcadoClient:
    """Small names around Ocado's web endpoints; behaviour lives elsewhere."""

    #: Required rather than defaulted to a shared session: which account's
    #: trolley this reads and writes is a decision the caller has to have made,
    #: and a default is how one person's basket ends up in another's. Mirrors
    #: :class:`app.sainsburys.client.SainsburysClient`.
    def __init__(self, session: OcadoSession):
        self.session = session
        self._destination: Destination | None = None

    def cart_view(self) -> dict[str, Any]:
        return self._json(
            "GET", CART_VIEW_PATH, params={"productGroupingType": "CATEGORIES"}
        )

    def apply_quantity(self, deltas: dict[str, int] | list[dict[str, Any]]) -> dict[str, Any]:
        return self._json(
            "POST",
            APPLY_QUANTITY_PATH,
            params={"cartProductSorting": "CATEGORIES"},
            json=_delta_payload(deltas),
        )

    def checkout_walk(self) -> dict[str, Any]:
        return self._json("GET", CHECKOUT_WALK_PATH)

    def delivery_addresses(self) -> list[dict[str, Any]]:
        payload = self._json(
            "GET", DELIVERY_ADDRESSES_PATH, params={"deliveryMethod": "HOME_DELIVERY"}
        )
        return payload if isinstance(payload, list) else []

    def destination(self, *, refresh: bool = False) -> Destination:
        """Resolve the address/region ids that slot calls need.

        Prefers the address book, since it names the primary address explicitly;
        falls back to the cart, which carries the same two ids.
        """
        if self._destination is not None and not refresh:
            return self._destination
        resolved = _destination_from_addresses(self.delivery_addresses())
        if resolved is None:
            resolved = _destination_from_cart(self.checkout_walk())
        if resolved is None:
            raise RuntimeError("could not resolve an Ocado delivery destination")
        self._destination = resolved
        return resolved

    def slots(
        self,
        ddid: str | None = None,
        region: str | None = None,
        *,
        days: int = 7,
        shipping_group: str = DEFAULT_SHIPPING_GROUP,
    ) -> list[Slot]:
        if not (ddid and region):
            resolved = self.destination()
            ddid = ddid or resolved.delivery_destination_id
            region = region or resolved.region_id
        payload = self._json(
            "POST",
            SLOTS_PATH,
            json={
                "deliveryDestinationId": ddid,
                "regionId": region,
                "displayConfiguration": "DELIVERY_METHOD",
                "shippingGroupType": shipping_group,
                "numberOfDays": days,
            },
        )
        return normalize_slots(payload)

    def reserve(
        self, slot_id: str, ddid: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        if not (ddid and region):
            resolved = self.destination()
            ddid = ddid or resolved.delivery_destination_id
            region = region or resolved.region_id
        return self._json(
            "POST",
            RESERVATION_PATH,
            json={"regionId": region, "slotId": slot_id, "deliveryDestinationId": ddid},
        )

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, path, **kwargs)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()


def _delta_payload(deltas: dict[str, int] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the bare array apply-quantity expects.

    ``meta`` is analytics attribution the web client fills in; an empty object is
    accepted, so nothing is invented here.
    """
    if isinstance(deltas, list):
        return deltas
    return [
        {"productId": sku, "quantity": delta, "meta": {}}
        for sku, delta in sorted(deltas.items())
        if delta
    ]


def _destination_from_addresses(addresses: list[dict[str, Any]]) -> Destination | None:
    if not addresses:
        return None
    primary = next((a for a in addresses if a.get("isPrimary")), addresses[0])
    ddid = primary.get("deliveryDestinationId")
    region = primary.get("resolvedRegionId")
    if not region:
        propositions = primary.get("propositions")
        if isinstance(propositions, list) and propositions:
            region = propositions[0].get("regionId")
    if isinstance(ddid, str) and isinstance(region, str):
        return Destination(delivery_destination_id=ddid, region_id=region)
    return None


def _destination_from_cart(cart: dict[str, Any]) -> Destination | None:
    ddid = cart.get("deliveryDestinationId")
    region = cart.get("regionId")
    if isinstance(ddid, str) and isinstance(region, str):
        return Destination(delivery_destination_id=ddid, region_id=region)
    return None


def normalize_slots(payload: Any) -> list[Slot]:
    """Flatten the carrier/day grid into one list of slots.

    The grid's ``day`` is authoritative for grouping - it is the local delivery
    date, which a UTC ``startTime`` can disagree with either side of midnight.
    """
    slots: list[Slot] = []
    if not isinstance(payload, dict):
        return slots
    carriers = payload.get("carriers")
    if not isinstance(carriers, list):
        return slots
    for carrier in carriers:
        if not isinstance(carrier, dict):
            continue
        for group in _slot_groups(carrier):
            day = group.get("day") if isinstance(group.get("day"), str) else None
            for slot in group.get("slots") or []:
                if isinstance(slot, dict):
                    slots.append(_normalize_slot(slot, day))
    return slots


def _slot_groups(carrier: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for key in ("gridSlots", "featuredSlots"):
        value = carrier.get(key)
        if isinstance(value, list):
            groups.extend(item for item in value if isinstance(item, dict))
    return groups


def _normalize_slot(slot: dict[str, Any], day: str | None = None) -> Slot:
    attributes = slot.get("attributes")
    attributes = [a for a in attributes if isinstance(a, str)] if isinstance(attributes, list) else []
    upper = {a.upper() for a in attributes}
    window = slot.get("slotWindow") if isinstance(slot.get("slotWindow"), dict) else {}
    start = _string(window.get("startTime"))
    end = _string(window.get("endTime"))
    return Slot(
        slot_id=str(slot.get("slotId") or ""),
        start=start,
        end=end,
        day=day or _day_from(start),
        available="AVAILABLE" in upper,
        eco="GREEN" in upper,
        price=_price(slot.get("deliveryPrice")),
        raw=slot,
    )


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _day_from(start: str | None) -> str | None:
    if not start:
        return None
    try:
        return datetime.fromisoformat(start.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("amount", "value"):
            if key in value:
                return _price(value[key])
        return None
    if isinstance(value, str) and value.strip():
        try:
            return float(value.replace("£", "").strip())
        except ValueError:
            return None
    return None
