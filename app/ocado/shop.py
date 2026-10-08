"""The live trolley and search, reduced to what a person (or Noodle) asks of them.

cart-view states SKUs, quantities and prices but no names, so a basket is named
from the catalogue first and only the SKUs it has never seen are asked of Ocado.
Whether the trolley can be checked out, and why not, is Ocado's own verdict
(``checkoutRestrictions``: ``MISSING_SLOT``, ``NOT_REACHED_THRESHOLD``...), passed
through rather than recomputed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.ocado.cart_payload import cart_view_items, line_cost
from app.ocado.sync import cart_quantities
from app.scraper.products.ocado import extract_product_objects, normalize_product


@dataclass(frozen=True, slots=True)
class Line:
    sku: str
    name: str | None
    quantity: int
    cost: float | None


@dataclass(frozen=True, slots=True)
class Basket:
    lines: tuple[Line, ...]
    total: float | None
    can_checkout: bool
    restrictions: tuple[str, ...] = field(default_factory=tuple)
    minimum: float | None = None
    order_id: str | None = None


@dataclass(frozen=True, slots=True)
class Found:
    sku: str
    name: str
    price: float | None
    pack: str | None
    unit_price: float | None
    unit_basis: str | None
    in_stock: bool | None


def basket(payload: Any, names: Callable[[list[str]], dict[str, str]]) -> Basket:
    """``names`` maps SKUs to product names for whatever SKUs it knows."""
    payload = payload if isinstance(payload, dict) else {}
    rows = cart_view_items(payload)
    quantities = cart_quantities(payload)
    known = names(list(quantities)) if quantities else {}
    group = payload.get("activeCheckoutGroup") or {}
    totals = payload.get("totals") or {}
    return Basket(
        lines=tuple(Line(sku=sku, name=known.get(sku), quantity=qty, cost=line_cost(rows.get(sku)))
                    for sku, qty in quantities.items()),
        total=_amount(totals.get("itemPriceAfterPromos")),
        can_checkout=bool(group.get("canCheckout")),
        restrictions=tuple(group.get("checkoutRestrictions") or ()),
        minimum=_amount(group.get("minimumCheckoutThreshold")),
        order_id=group.get("orderId"),
    )


def search_results(payload: Any, limit: int) -> list[Found]:
    found = []
    for raw in extract_product_objects(payload):
        try:
            product = normalize_product(raw)
        except ValueError:
            continue
        found.append(Found(sku=product.sku, name=product.name, price=product.price,
                           pack=product.pack_size_raw, unit_price=product.unit_price,
                           unit_basis=product.unit_price_basis, in_stock=product.in_stock))
        if len(found) >= limit:
            break
    return found


def _amount(value: Any) -> float | None:
    try:
        return float(value["amount"])
    except (TypeError, KeyError, ValueError):
        return None
