"""Typed, thin Ocado API wrappers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.ocado.session import OcadoSession


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


class OcadoClient:
    """Small names around Ocado's web endpoints; behaviour lives elsewhere."""

    def __init__(self, session: OcadoSession | None = None):
        self.session = session or OcadoSession()

    def cart_view(self) -> dict[str, Any]:
        return self._json("GET", "/webshop/getBasket.do")

    def apply_quantity(self, deltas: dict[str, int] | list[dict[str, Any]]) -> dict[str, Any]:
        payload = _delta_payload(deltas)
        return self._json("POST", "/webshop/trolley/items", json=payload)

    def checkout_walk(self) -> dict[str, Any]:
        return self._json("GET", "/checkout")

    def delivery_addresses(self) -> dict[str, Any]:
        return self._json("GET", "/webshop/delivery-addresses")

    def slots(self, ddid: str | None = None, region: str | None = None) -> list[Slot]:
        params = {k: v for k, v in {"ddid": ddid, "region": region}.items() if v}
        payload = self._json("GET", "/webshop/slots", params=params)
        return normalize_slots(payload)

    def reserve(self, slot_id: str, ddid: str | None = None, region: str | None = None) -> dict[str, Any]:
        payload = {"slotId": slot_id}
        if ddid:
            payload["ddid"] = ddid
        if region:
            payload["region"] = region
        return self._json("POST", "/webshop/slots/reserve", json=payload)

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, path, **kwargs)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()


def normalize_slots(payload: Any) -> list[Slot]:
    raw_slots = _find_slot_list(payload)
    return [_normalize_slot(slot) for slot in raw_slots if isinstance(slot, dict)]


def _delta_payload(deltas: dict[str, int] | list[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(deltas, list):
        return {"items": deltas}
    return {
        "items": [
            {"sku": sku, "quantityDelta": delta}
            for sku, delta in sorted(deltas.items())
            if delta
        ]
    }


def _find_slot_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        direct = [item for item in payload if isinstance(item, dict) and _looks_like_slot(item)]
        if direct:
            return direct
        found: list[Any] = []
        for item in payload:
            found.extend(_find_slot_list(item))
        return found
    if not isinstance(payload, dict):
        return []
    for key in ("slots", "deliverySlots", "availableSlots"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    found: list[Any] = []
    for value in payload.values():
        found.extend(_find_slot_list(value))
    return found


def _looks_like_slot(item: dict[str, Any]) -> bool:
    return any(key in item for key in ("slotId", "slot_id", "startTime", "deliveryPrice"))


def _normalize_slot(slot: dict[str, Any]) -> Slot:
    attrs = slot.get("attributes") if isinstance(slot.get("attributes"), dict) else {}
    price = _price(slot.get("deliveryPrice"))
    available = price is not None and bool(
        attrs.get("available", slot.get("available", True))
    )
    return Slot(
        slot_id=str(slot.get("slotId") or slot.get("id") or slot.get("slot_id") or ""),
        start=_string(slot.get("startTime") or slot.get("start") or slot.get("from")),
        end=_string(slot.get("endTime") or slot.get("end") or slot.get("to")),
        day=_day(slot),
        available=available,
        eco=bool(attrs.get("eco") or attrs.get("green") or slot.get("eco")),
        price=price,
        raw=slot,
    )


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _day(slot: dict[str, Any]) -> str | None:
    value = _string(slot.get("date") or slot.get("day"))
    if value:
        return value
    start = _string(slot.get("startTime") or slot.get("start") or slot.get("from"))
    if not start:
        return None
    try:
        return datetime.fromisoformat(start.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("amount", "value"):
            if key in value:
                return _price(value[key])
    if isinstance(value, str) and value.strip():
        text = value.replace("\u00a3", "").strip()
        try:
            return float(text)
        except ValueError:
            return None
    return None
