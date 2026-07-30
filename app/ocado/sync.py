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


def push_basket(
    client: OcadoClient,
    basket: Basket,
    *,
    owned_item_keys: set[str] | None = None,
) -> PushResult:
    current = cart_quantities(client.cart_view())
    targets, names, unmapped = basket_targets(basket, owned_item_keys=owned_item_keys)
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


def basket_targets(
    basket: Basket,
    *,
    owned_item_keys: set[str] | None = None,
) -> tuple[dict[str, int], dict[str, str], list[str]]:
    targets: dict[str, int] = {}
    names: dict[str, str] = {}
    unmapped = list(basket.unmapped) + list(basket.unpriceable)
    owned_item_keys = owned_item_keys or set()
    for line in basket.lines:
        if line.key in owned_item_keys:
            continue
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
    """Read ``{productId: quantity}`` out of any of Ocado's basket payloads.

    The three that matter nest their lines differently, so the paths are spelled
    out rather than searched for - a generic walker silently returns ``{}`` on
    cart-view, which reads as "basket empty" and makes every push a double-add:

    * ``cart-view``      → ``checkoutGroups.assignedCheckoutGroups[].itemGroups[].items[]``
    * ``checkout-walk``  → ``items[]``
    * ``apply-quantity`` → ``basketUpdateResult.itemGroups[].items[]``
    """
    quantities: dict[str, int] = {}
    for item in _cart_items(payload):
        sku = item.get("productId")
        quantity = _quantity(item)
        if isinstance(sku, str) and sku and quantity is not None:
            quantities[sku] = quantity
    return quantities


def _cart_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items: list[dict[str, Any]] = []

    groups = payload.get("checkoutGroups")
    if isinstance(groups, dict):
        for group in _dicts(groups.get("assignedCheckoutGroups")):
            items.extend(_item_group_items(group))

    update = payload.get("basketUpdateResult")
    if isinstance(update, dict):
        items.extend(_item_group_items(update))

    items.extend(_item_group_items(payload))

    # checkout-walk carries its lines at the top level; its itemGroups hold bare
    # product-id strings, which _item_group_items already skips.
    items.extend(_dicts(payload.get("items")))

    deduped: dict[int, dict[str, Any]] = {id(item): item for item in items}
    return list(deduped.values())


def _item_group_items(node: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for group in _dicts(node.get("itemGroups")):
        items.extend(_dicts(group.get("items")))
    return items


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _quantity(item: dict[str, Any]) -> int | None:
    value = item.get("quantity")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
