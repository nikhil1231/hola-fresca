"""Ocado orders, reduced to what a person (or Noodle) asks about them.

The raw order carries checkout ids, carrier ids and payment plumbing; what
matters is when it arrives, until when it can still be changed, what it costs and
whether it repeats. ``confirmOrderChangesBy`` is the edit cutoff Ocado shows as
"edit until", and is the deadline worth surfacing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    status: str | None
    delivery_start: str | None
    delivery_end: str | None
    edit_until: str | None
    editable: bool
    cancelable: bool
    total: str | None
    currency: str | None
    items: int | None
    recurring: bool
    address: str | None


def summarise(raw: dict[str, Any]) -> Order:
    dates = raw.get("dates") or {}
    final = ((raw.get("orderTotals") or {}).get("finalPrice")) or {}
    return Order(
        order_id=str(raw.get("orderId") or raw.get("orderReference") or ""),
        status=raw.get("status"),
        delivery_start=dates.get("deliveryStartDate"),
        delivery_end=dates.get("deliveryEndDate"),
        edit_until=dates.get("confirmOrderChangesBy") or dates.get("deliveryLatestUpdateTime"),
        editable=raw.get("editStatus") == "EDITABLE",
        cancelable=bool(raw.get("cancelable")),
        total=final.get("amount"),
        currency=final.get("currency"),
        items=raw.get("totalItems"),
        recurring=bool(raw.get("recurringShoppingDefinition")),
        address=raw.get("addressNickName") or raw.get("address"),
    )
