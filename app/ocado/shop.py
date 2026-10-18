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
class Offer:
    id: str
    label: str
    required_quantity: int | None = None


@dataclass(frozen=True, slots=True)
class Line:
    sku: str
    name: str | None
    quantity: int
    cost: float | None
    image_url: str | None = None
    pack: str | None = None
    original_cost: float | None = None
    offers: tuple[Offer, ...] = ()


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
    image_url: str | None = None
    base_price: float | None = None
    offers: tuple[Offer, ...] = ()


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
                           unit_basis=product.unit_price_basis, in_stock=product.in_stock,
                           image_url=product.image_url, base_price=product.base_price,
                           offers=offers(raw)))
        if len(found) >= limit:
            break
    return found


def offers(raw: dict) -> tuple[Offer, ...]:
    result = []
    for promo in raw.get("promotions") or []:
        identifier = promo.get("promoId") or promo.get("promotionId")
        label = promo.get("description") or promo.get("name")
        if not identifier or not label:
            continue
        quantity = promo.get("requiredProductQuantity")
        result.append(Offer(str(identifier), str(label), quantity if isinstance(quantity, int) and 1 < quantity <= 50 else None))
    return tuple(result)


def order_lines(raw: dict) -> tuple[Line, ...]:
    """Decorated prices.offered is the entire line, including multibuy savings."""
    products = (raw.get("groupedProducts") or {}).get("products") or []
    result = []
    for row in products:
        sku, quantity = row.get("productId"), row.get("quantity")
        if not sku or not isinstance(quantity, int) or quantity <= 0:
            continue
        prices = row.get("prices") or {}
        cost, original = _amount(prices.get("offered")), _amount(prices.get("retail"))
        result.append(Line(str(sku), row.get("name"), quantity, cost,
                           image_url=(row.get("image") or {}).get("src"),
                           pack=(row.get("packInfo") or {}).get("packSizeDescription"),
                           original_cost=original if original is not None and cost is not None and original > cost else None,
                           offers=offers(row)))
    return tuple(result)


def _amount(value: Any) -> float | None:
    try:
        return float(value["amount"])
    except (TypeError, KeyError, ValueError):
        return None
