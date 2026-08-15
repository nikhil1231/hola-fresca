"""The shape every retailer adapter produces, and the parsing they share.

A retailer adapter is a *module*, not a class: it exposes ``RETAILER``, a couple
of URL builders, three extract/normalize functions and a browser client, and
:func:`app.scraper.products.registry.get_adapter` hands the module itself back.
That keeps :mod:`app.scraper.products.ocado` exactly as it was written — it was
always this interface, it just had no name for it.

What lives here is the part that is genuinely not about any one retailer: pack
sizes, unit prices and money are written the same way on every UK grocery site,
because they are written for the same shoppers. ``"4 x 415g"`` parses the same
whoever printed it. What does *not* live here is anything that reads a payload:
field names are the retailer's business and belong in its own module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_MONEY_RE = re.compile(r"(£\s*)?(\d+(?:\.\d+)?)\s*(p)?", re.I)
_PACK_MULTI_RE = re.compile(
    r"(?P<count>\d+(?:\.\d+)?)\s*x\s*(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>kg|g|l|litre|litres|ml)\b",
    re.I,
)
_PACK_SINGLE_RE = re.compile(r"(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>kg|g|l|litre|litres|ml)\b", re.I)
_PACK_EACH_RE = re.compile(
    r"(?P<count>\d+(?:\.\d+)?)\s*(?:per\s+pack|pack|pk|ct|count|items?)\b", re.I
)
_UNIT_PRICE_RE = re.compile(
    r"(?P<money>£\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?p)\s*(?:/|per\s+)(?P<basis>100g|kg|kilo|litre|liter|l|ml|item|each)",
    re.I,
)


@dataclass(frozen=True)
class NormalizedProduct:
    """One retailer product, in the shape :func:`upsert_product` writes.

    Every field beyond ``retailer``/``sku``/``name`` is optional because retailers
    differ in what they state, and a missing value has to stay distinguishable
    from a zero. ``shelf_life_days`` is the sharpest example: Ocado publishes a
    guaranteed minimum life and Sainsbury's does not, so a NULL there means "this
    shop does not say", never "it goes off today". :func:`app.planner.waste`
    reads it that way.

    ``price``/``unit_price`` are **what you would pay today**, promotions
    included; ``base_price``/``base_unit_price`` are the same product at its list
    price, and are NULL when nothing is on offer. Two numbers because two
    questions are being asked: what a basket costs is a live figure and wants the
    promotion, while which product is better value has to survive a promotion
    ending — see :mod:`app.mapping.ordering`.
    """

    retailer: str
    sku: str
    name: str
    brand: str | None = None
    pack_size_raw: str | None = None
    pack_size_value: float | None = None
    pack_size_unit: str | None = None
    price: float | None = None
    unit_price: float | None = None
    unit_price_basis: str | None = None
    base_price: float | None = None
    base_unit_price: float | None = None
    is_nectar_price: bool = False
    category: str | None = None
    # A storage form, not merely a long shelf life. Retailer adapters prefer an
    # explicit "Frozen" badge/label and fall back to the category path.
    is_frozen: bool = False
    in_stock: bool | None = None
    shelf_life_raw: str | None = None
    shelf_life_days: int | None = None
    avg_rating: float | None = None
    ratings_count: int | None = None
    image_url: str | None = None
    url: str | None = None
    raw_json: str | None = None


def category_is_frozen(category: str | None) -> bool:
    """Whether a retailer category path explicitly identifies frozen stock."""
    if not category:
        return False
    return any("frozen" in part.strip().lower() for part in category.split(">"))


@dataclass(frozen=True)
class ProductStatus:
    """What a shop says about one product *right now*.

    The live counterpart to :class:`NormalizedProduct`, and deliberately much
    smaller. A refresh re-reads what goes stale between scrapes — stock and the
    four prices — and writes only those back, because a live response is not
    always as decorated as the search response the catalogue was built from and
    overwriting a good brand or category with a missing one would be a downgrade.

    Every price field may be ``None`` for "the shop did not say", which is left
    alone rather than written as a null price. ``unlisted`` is the exception that
    is not a fact about the product at all: it means the shop never answered for
    this id, which reads as unavailable but is worth telling apart — see
    ``app.catalogue.refresh_stock``.
    """

    sku: str
    available: bool
    price: float | None = None
    base_price: float | None = None
    unit_price: float | None = None
    unit_price_basis: str | None = None
    base_unit_price: float | None = None
    is_nectar_price: bool = False
    name: str | None = None
    unlisted: bool = False


def parse_pack_size(raw: str | None) -> tuple[float | None, str | None]:
    """Return a metric quantity and unit from a pack-size string.

    Multipacks are multiplied out ("4 x 415g" -> 1660 g) because the planner
    covers demand from total capacity and does not care how the box is divided.
    """
    if not raw:
        return None, None
    text = raw.strip()
    multi = _PACK_MULTI_RE.search(text)
    if multi:
        value = float(multi.group("count")) * float(multi.group("size"))
        return metric(value, multi.group("unit"))
    single = _PACK_SINGLE_RE.search(text)
    if single:
        return metric(float(single.group("size")), single.group("unit"))
    each = _PACK_EACH_RE.search(text)
    if each:
        return float(each.group("count")), "each"
    return None, None


#: A size in a product title, with the multipack prefix if there is one. The
#: prefix has to be part of the match: "Heinz Baked Beans 4 x 415g" is 1660 g,
#: and a pattern that captured only "415g" would silently buy a quarter of the
#: box — which is exactly what a name-only retailer would get wrong every time.
_NAME_PACK_RE = re.compile(
    r"(?:,\s*)?((?:\d+(?:\.\d+)?\s*x\s*)?\d+(?:\.\d+)?\s*(?:kg|g|l|litre|litres|ml))\b", re.I
)
#: A count stated as a suffix ("Baking Potatoes x4", "Eggs x6"), which is how
#: loose produce sold by the item is titled. Only trusted when the retailer also
#: prices the thing by the each — see the callers.
_NAME_COUNT_RE = re.compile(r"\bx\s*(?P<count>\d+)\b", re.I)


def pack_size_from_name(name: str) -> str | None:
    """The stated size in a product title, for retailers that state none.

    Sainsbury's has no pack-size field at all — the weight lives in the name
    ("Sainsbury's Baked Beans In Tomato Sauce 400g"), which is also Ocado's
    fallback when its own field is absent.
    """
    match = _NAME_PACK_RE.search(name)
    return match.group(1) if match else None


def pack_count_from_name(name: str) -> float | None:
    """A whole-unit count from a product title ("Baking Potatoes x4" -> 4).

    Deliberately not folded into :func:`pack_size_from_name`: a title can carry
    both a count and a weight ("Chorizo Slices x34 170g"), and there the weight
    is the buyable quantity. Callers use this only when no metric size was found
    *and* the retailer prices the product by the each.
    """
    match = _NAME_COUNT_RE.search(name)
    if not match:
        return None
    count = float(match.group("count"))
    return count if count > 0 else None


def parse_unit_price(raw: str | None) -> tuple[float | None, str | None]:
    if not raw:
        return None, None
    match = _UNIT_PRICE_RE.search(raw)
    if not match:
        return None, None
    return parse_money(match.group("money")), basis(match.group("basis"))


def base_price(current: float | None, stated: float | None) -> float | None:
    """A list price, kept only where it is really one.

    Both shops state a was-price on some products that are not actually cheaper
    today — a multibuy quotes the single-unit price it has always charged, and a
    just-ended promotion can leave its own original price behind. Anything not
    strictly above what you would pay now is discarded, so ``base_price is not
    None`` means "this is on offer" everywhere it is read.
    """
    if current is None or stated is None:
        return None
    return stated if stated > current else None


def parse_money(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _MONEY_RE.search(value.replace(",", ""))
    if not match:
        return None
    amount = float(match.group(2))
    if match.group(3) or ("£" not in value and "p" in value.lower()):
        return amount / 100
    return amount


def metric(value: float, unit: str) -> tuple[float, str]:
    """Normalise a stated size to grams or millilitres."""
    u = unit.lower()
    if u == "kg":
        return value * 1000, "g"
    if u in {"l", "litre", "litres", "ltr"}:
        return value * 1000, "ml"
    return value, u


def basis(value: str) -> str:
    """Normalise a unit-price denominator ("kilo", "ltr", "per_each")."""
    value = value.lower()
    if value in {"kilo", "kg", "per_1kg"}:
        return "kg"
    if value in {"litre", "liter", "l", "ltr", "per_litre"}:
        return "l"
    if value in {"item", "each", "ea", "per_each"}:
        return "each"
    return value


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]
