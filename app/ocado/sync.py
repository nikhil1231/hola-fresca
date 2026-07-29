"""Reconcile a planner basket into Ocado cart quantities."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ocado.client import OcadoClient
from app.planner.basket import Basket


@dataclass(frozen=True, slots=True)
class PushLine:
    sku: str
    quantity: int
    name: str | None = None


@dataclass(frozen=True, slots=True)
class PushResult:
    applied: list[PushLine] = field(default_factory=list)
    dropped: list[PushLine] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    deltas: dict[str, int] = field(default_factory=dict)


def push_basket(client: OcadoClient, basket: Basket) -> PushResult:
    current = cart_quantities(client.cart_view())
    targets, names, unmapped = basket_targets(basket)
    deltas = {
        sku: targets.get(sku, 0) - current.get(sku, 0)
        for sku in sorted(set(current) | set(targets))
    }
    deltas = {sku: delta for sku, delta in deltas.items() if delta}
    if deltas:
        client.apply_quantity(deltas)
    after = cart_quantities(client.cart_view())
    applied = [
        PushLine(sku=sku, quantity=after.get(sku, 0), name=names.get(sku))
        for sku in sorted(targets)
        if after.get(sku, 0) == targets[sku]
    ]
    dropped = [
        PushLine(sku=sku, quantity=targets[sku] - after.get(sku, 0), name=names.get(sku))
        for sku in sorted(targets)
        if after.get(sku, 0) != targets[sku]
    ]
    return PushResult(applied=applied, dropped=dropped, unmapped=unmapped, deltas=deltas)


def basket_targets(basket: Basket) -> tuple[dict[str, int], dict[str, str], list[str]]:
    targets: dict[str, int] = {}
    names: dict[str, str] = {}
    unmapped = list(basket.unmapped) + list(basket.unpriceable)
    for line in basket.lines:
        if line.external:
            continue
        if line.cover is None:
            unmapped.append(line.name)
            continue
        for choice in line.cover.choices:
            targets[choice.pack.sku] = targets.get(choice.pack.sku, 0) + choice.count
            names.setdefault(choice.pack.sku, choice.pack.product_name)
    return targets, names, sorted(set(unmapped))


def cart_quantities(payload: Any) -> dict[str, int]:
    quantities: dict[str, int] = {}
    for item in _cart_items(payload):
        sku = _sku(item)
        if not sku:
            continue
        quantity = _quantity(item)
        if quantity is not None:
            quantities[sku] = quantity
    return quantities


def _cart_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "lines", "basketItems", "cartItems", "products"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for value in payload.values():
        found = _cart_items(value)
        if found:
            return found
    return []


def _sku(item: dict[str, Any]) -> str | None:
    for key in ("sku", "productId", "product_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    product = item.get("product")
    if isinstance(product, dict):
        return _sku(product)
    return None


def _quantity(item: dict[str, Any]) -> int | None:
    for key in ("quantity", "qty", "itemQuantity"):
        value = item.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return None

