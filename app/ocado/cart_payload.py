"""Normalising Ocado's cart payloads into the shape the API renders.

Lived in the API router while Ocado was the only shop with a cart. It is payload
parsing, so it belongs next to the rest of Ocado's protocol knowledge, and
moving it is what let the router stop knowing how any one shop nests its lines
(see :mod:`app.cart.adapters`).
"""
from __future__ import annotations

from typing import Any

from app.cart.adapters import CartItem, CartSnapshot
from app.ocado.sync import cart_quantities


def snapshot(payload: Any) -> CartSnapshot:
    """The live cart as normalized lines, keeping the payload for the merge."""
    items = cart_view_items(payload)
    return CartSnapshot(
        items=tuple(
            CartItem(sku=sku, quantity=quantity, cost=line_cost(items.get(sku)))
            for sku, quantity in cart_quantities(payload).items()
        ),
        raw=payload,
    )


def cart_view_items(payload: Any) -> dict[str, dict]:
    """Index the product rows in the cart-view response by SKU."""
    if not isinstance(payload, dict):
        return {}
    groups = payload.get("checkoutGroups")
    if not isinstance(groups, dict):
        return {}
    indexed: dict[str, dict] = {}
    for checkout_group in groups.get("assignedCheckoutGroups") or []:
        if not isinstance(checkout_group, dict):
            continue
        for item_group in checkout_group.get("itemGroups") or []:
            if not isinstance(item_group, dict):
                continue
            for item in item_group.get("items") or []:
                if not isinstance(item, dict):
                    continue
                sku = item.get("productId")
                if isinstance(sku, str) and sku:
                    indexed[sku] = item
    return indexed


def line_cost(item: dict | None) -> float | None:
    """What Ocado says this line costs now, where it says so.

    Prefers the stated line total over unit price times quantity: a multi-buy
    makes those two disagree, and the one the checkout will actually charge is
    the total.
    """
    if not isinstance(item, dict):
        return None
    total_prices = item.get("totalPrices")
    if isinstance(total_prices, dict):
        total = _amount(total_prices.get("finalPrice"))
        if total is not None:
            return total

    unit = _amount(item.get("finalPrice"))
    if unit is None:
        product_prices = item.get("productPrices")
        if isinstance(product_prices, dict):
            unit = _amount(product_prices.get("finalPrice"))
    if unit is None:
        return None
    try:
        return unit * int(item.get("quantity", 0))
    except (TypeError, ValueError):
        return None


def _amount(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    try:
        return float(value.get("amount"))
    except (TypeError, ValueError):
        return None
