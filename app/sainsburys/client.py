"""Typed, thin Sainsbury's basket wrappers.

Every path and body here is taken from the groceries site's own client bundle,
not from guesswork. Three shapes differ from Ocado and are easy to get wrong:

* quantities are **absolute**, not deltas. Ocado's ``apply-quantity`` takes
  ``+1``/``-1``; here ``quantity: 3`` means "make it three". The merge in
  :mod:`app.sainsburys.sync` therefore works in goals rather than in deltas, and
  a delta accidentally sent as a quantity is a silent wrong order, not an error.
* adding and changing are **different calls**. A product not in the trolley is
  ``POST``ed to ``/basket/items``; one already there has an ``item_uid`` and must
  be ``PUT`` to ``/basket``, which is also the only way to remove it (quantity
  zero). Posting a product that is already in the trolley adds to it rather than
  setting it.
* every line carries a **unit of measure**. ``ea`` for things sold by the item,
  ``kg`` for loose weight, and ``C62`` — the UN/CEFACT code for "one" — for the
  multi-buy lines the site sorts to the end of a bulk add, which is why
  :func:`_ordered` reproduces that sort.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.sainsburys.session import SainsburysSession, get_shared_session

BASKET_API = "/groceries-api/gol-services/basket"
BASKET_PATH = f"{BASKET_API}/v2/basket"
BASKET_ITEM_PATH = f"{BASKET_API}/v2/basket/item"
BASKET_ITEMS_PATH = f"{BASKET_API}/v2/basket/items"

#: Sold by the item. What almost everything the planner buys is priced in.
UOM_EACH = "ea"
#: Sold by weight, priced per kilo — loose fruit, veg and butcher counter lines.
UOM_KG = "kg"
#: "One of", the code the site gives multi-buy lines. Sorted last in a bulk add
#: because the site does the same: these lines can change the price of the ones
#: around them, so they are applied once the rest of the trolley is settled.
UOM_MULTIBUY = "C62"


@dataclass(frozen=True, slots=True)
class BasketLine:
    """One line of the live trolley, in the terms a merge needs."""

    sku: str
    quantity: int
    #: The trolley's own id for this line, needed to change or remove it. Absent
    #: means the product is not in the trolley.
    item_uid: str | None = None
    uom: str = UOM_EACH
    name: str | None = None
    price: float | None = None


class BasketError(RuntimeError):
    """A basket call Sainsbury's refused."""

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class SainsburysClient:
    """Small names around Sainsbury's basket endpoints; behaviour lives elsewhere."""

    def __init__(self, session: SainsburysSession | None = None):
        self.session = session or get_shared_session()

    def basket(self, *, calculate: bool = True) -> dict[str, Any]:
        """The live trolley.

        ``calculate`` asks Sainsbury's to price it, which is what the site does
        on the basket page and what makes the totals trustworthy. It costs the
        server a little more, so a caller that only wants quantities can turn it
        off.
        """
        params = {"calculate": "true"} if calculate else None
        return self._json("GET", BASKET_PATH, params=params)

    def lines(self) -> list[BasketLine]:
        return basket_lines(self.basket(calculate=False))

    def add(self, items: list[BasketLine]) -> dict[str, Any]:
        """Put products in the trolley that are not in it yet."""
        if not items:
            return {}
        body = [
            {"product_uid": line.sku, "quantity": line.quantity, "uom": line.uom}
            for line in _ordered(items)
        ]
        return self._json("POST", BASKET_ITEMS_PATH, json=body)

    def update(self, line: BasketLine) -> dict[str, Any]:
        """Set an existing line to an absolute quantity, or to zero to remove it.

        ``decreasing_quantity`` is not decoration: Sainsbury's uses it to decide
        whether a reduction should also drop any multi-buy the line was part of,
        and a shrinking line sent without it can come back the size it was.
        """
        if not line.item_uid:
            raise BasketError(f"cannot update {line.sku} without its basket item id")
        payload = {
            "product_uid": line.sku,
            "item_uid": line.item_uid,
            "quantity": line.quantity,
            "uom": line.uom,
        }
        return self._json("PUT", BASKET_PATH, json={"items": [payload]})

    def set_quantity(self, line: BasketLine, quantity: int) -> dict[str, Any]:
        """Make this product's line exactly ``quantity``, adding it if absent."""
        if quantity <= 0:
            if not line.item_uid:
                return {}
            return self.update(
                BasketLine(sku=line.sku, quantity=0, item_uid=line.item_uid, uom=line.uom)
            )
        if line.item_uid:
            return self.update(
                BasketLine(
                    sku=line.sku, quantity=quantity, item_uid=line.item_uid, uom=line.uom
                )
            )
        return self.add([BasketLine(sku=line.sku, quantity=quantity, uom=line.uom)])

    def empty(self) -> dict[str, Any]:
        """Throw the whole trolley away. Not used by the sync — see the ledger."""
        return self._json("DELETE", BASKET_PATH)

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise BasketError(
                f"Sainsbury's refused {method} {path} ({response.status_code})",
                status=response.status_code,
                payload=_safe_json(response),
            )
        if not response.content:
            return {}
        return _safe_json(response)


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def _ordered(items: list[BasketLine]) -> list[BasketLine]:
    """Multi-buy lines last, which is the order the site's own client sends."""
    return sorted(items, key=lambda line: 1 if line.uom == UOM_MULTIBUY else 0)


def basket_lines(payload: Any) -> list[BasketLine]:
    """The trolley as lines, tolerant of where Sainsbury's nests them.

    Deduplicated by product: a trolley can hold two lines for one product when a
    multi-buy splits it, and a merge that saw them separately would count the
    smaller one as the whole holding and buy the difference again.
    """
    merged: dict[str, BasketLine] = {}
    for item in _items(payload):
        line = _line(item)
        if line is None:
            continue
        seen = merged.get(line.sku)
        if seen is None:
            merged[line.sku] = line
        else:
            merged[line.sku] = BasketLine(
                sku=line.sku,
                quantity=seen.quantity + line.quantity,
                # Keep the first line's id: it is the one a reduction should
                # touch, and the merge only ever needs one handle per product.
                item_uid=seen.item_uid or line.item_uid,
                uom=seen.uom,
                name=seen.name or line.name,
                price=seen.price if seen.price is not None else line.price,
            )
    return list(merged.values())


def basket_quantities(payload: Any) -> dict[str, int]:
    return {line.sku: line.quantity for line in basket_lines(payload) if line.quantity > 0}


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "basket_items", "products"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    basket = payload.get("basket")
    if isinstance(basket, dict):
        return _items(basket)
    return []


def _line(item: dict[str, Any]) -> BasketLine | None:
    sku = _text(item, "product_uid", "productUid", "sku")
    if not sku:
        product = item.get("product")
        if isinstance(product, dict):
            sku = _text(product, "product_uid", "productUid", "sku")
    if not sku:
        return None
    quantity = _int(item, "quantity", "qty")
    if quantity is None:
        return None
    return BasketLine(
        sku=sku,
        quantity=quantity,
        item_uid=_text(item, "item_uid", "itemUid", "id"),
        uom=_text(item, "uom", "unit_of_measure") or UOM_EACH,
        name=_text(item, "name", "product_name"),
        price=_float(item, "price", "total_price", "line_price"),
    )


def _text(node: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _int(node: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _float(node: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            nested = _float(value, "price", "amount", "value")
            if nested is not None:
                return nested
    return None
