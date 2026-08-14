"""Ocado product adapter and browser-session JSON fetcher."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin

from app import config
from app.scraper.products.browser import BrowserJsonClient
from app.scraper.products.base import (
    NormalizedProduct,
    ProductStatus,
    base_price as _base_of,
    basis as _basis,
    category_is_frozen,
    chunks,
    metric as _metric,
    pack_size_from_name,
    parse_money as _parse_money,
    parse_pack_size,
    parse_unit_price,
)
from app.scraper.ratelimit import AdaptiveThrottle

__all__ = [
    "RETAILER",
    "BASE_URL",
    "MAX_PRODUCTS_TO_DECORATE",
    "NormalizedProduct",
    "Client",
    "OcadoBrowserClient",
    "chunks",
    "extract_product_ids",
    "extract_product_objects",
    "normalize_product",
    "parse_pack_size",
    "parse_shelf_life",
    "parse_unit_price",
    "product_url",
    "search_url",
]

RETAILER = "ocado"
BASE_URL = "https://www.ocado.com"
SEARCH_PATH = "/api/webproductpagews/v6/product-pages/search"
PRODUCTS_PATH = "/api/webproductpagews/v6/products"
MAX_PRODUCTS_TO_DECORATE = 50
#: How many ids the bulk product endpoint is asked for at once.
PRODUCT_BATCH_SIZE = 50

#: Ocado's search and decorate endpoints are still fetched from a browser, and
#: are happy with a headless one. The live stock read needs no browser at all.
#: Contrast :data:`app.scraper.products.sainsburys.USES_BROWSER`, which is False
#: for the whole shop.
USES_BROWSER = True
BROWSER_HEADLESS = True

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
# Ocado's guaranteed minimum life on delivery, as {"quantity": n, "unit": ...}.
# Present only for products with a meaningful expiry (~98% of Fresh & Chilled,
# almost none of Home & Garden), so a null means "not perishable or not stated",
# never "zero days".
_LIFE_UNIT_DAYS = {"DAY": 1, "WEEK": 7, "MONTH": 30, "YEAR": 365}


def search_url(term: str) -> str:
    params = {
        "includeAdditionalPageInfo": "true",
        "maxPageSize": "300",
        "maxProductsToDecorate": str(MAX_PRODUCTS_TO_DECORATE),
        "q": term,
        "tag": "web",
    }
    return f"{BASE_URL}{SEARCH_PATH}?{urlencode(params)}"


def product_url(sku: str) -> str:
    return f"{BASE_URL}/products/{quote(sku)}"


def extract_product_ids(payload: Any) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []

    def walk(node: Any, parent_key: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and _looks_product_id_key(key):
                    add(value)
                walk(value, key)
        elif isinstance(node, list):
            for item in node:
                walk(item, parent_key)
        elif isinstance(node, str) and parent_key.lower() in {"id", "uuid", "sku", "productid"}:
            add(node)

    def add(value: str) -> None:
        match = _UUID_RE.search(value)
        if match:
            sku = match.group(0).lower()
            if sku not in seen:
                seen.add(sku)
                ids.append(sku)

    walk(payload)
    return ids


def extract_product_objects(payload: Any) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if _is_product_like(node):
                products.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    deduped: dict[str, dict[str, Any]] = {}
    for product in products:
        sku = _sku(product)
        if sku and sku not in deduped:
            deduped[sku] = product
    return list(deduped.values())


def normalize_product(payload: dict[str, Any]) -> NormalizedProduct:
    sku = _sku(payload)
    if not sku:
        raise ValueError("product payload has no sku/id")
    name = _string_field(payload, "name", "title", "productName", "displayName") or ""
    if not name:
        raise ValueError(f"product {sku} has no name")

    pack_raw = _pack_size_raw(payload, name)
    pack_value, pack_unit = parse_pack_size(pack_raw)
    unit_price, unit_basis = _unit_price(payload)
    base_unit = _base_unit_price(payload, unit_price, unit_basis)
    avg_rating, ratings_count = _rating(payload)
    life_raw, life_days = parse_shelf_life(payload.get("guaranteedProductLife"))

    category = _category(payload)
    return NormalizedProduct(
        retailer=RETAILER,
        sku=sku,
        name=name,
        brand=_brand(payload),
        pack_size_raw=pack_raw,
        pack_size_value=pack_value,
        pack_size_unit=pack_unit,
        price=_price(payload),
        unit_price=unit_price,
        unit_price_basis=unit_basis,
        base_price=_base_price(payload),
        base_unit_price=base_unit,
        category=category,
        is_frozen=_is_frozen(payload, category),
        in_stock=_in_stock(payload),
        shelf_life_raw=life_raw,
        shelf_life_days=life_days,
        avg_rating=avg_rating,
        ratings_count=ratings_count,
        image_url=_image_url(payload),
        url=_url(payload, sku),
        raw_json=json.dumps(payload, ensure_ascii=False),
    )


def product_status(node: dict[str, Any]) -> ProductStatus | None:
    """One product's live stock and prices, from a products-endpoint node."""
    sku = _sku(node)
    if not sku:
        return None
    unit_price, unit_basis = _unit_price(node)
    available = _in_stock(node)
    name = node.get("name")
    return ProductStatus(
        sku=sku,
        # Ocado states availability on every product it returns; a payload that
        # somehow omits it is taken at its word that the product exists.
        available=True if available is None else available,
        price=_price(node),
        base_price=_base_price(node),
        unit_price=unit_price,
        unit_price_basis=unit_basis,
        base_unit_price=_base_unit_price(node, unit_price, unit_basis),
        name=name if isinstance(name, str) else None,
    )


def fetch_statuses(skus: list[str]) -> dict[str, ProductStatus]:
    """Live stock and prices for ``skus``.

    Ocado's products endpoint answers an anonymous httpx session — no browser and
    no login — so this is a plain HTTP call. Imported where it is used because
    :mod:`app.ocado.availability` reads this module in turn.
    """
    from app.ocado.availability import fetch_statuses as _fetch

    return _fetch(skus)


def parse_shelf_life(raw: Any) -> tuple[str | None, int | None]:
    """Return ("2 WEEK", 14) from Ocado's guaranteedProductLife object.

    Months and years are approximated (30/365 days) — Ocado states them as a
    guaranteed *minimum*, so the rounding only ever understates the real life.
    """
    if not isinstance(raw, dict):
        return None, None
    unit = str(raw.get("unit") or "").strip().upper()
    quantity = raw.get("quantity")
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        return None, None
    per_unit = _LIFE_UNIT_DAYS.get(unit)
    if per_unit is None or quantity <= 0:
        return None, None
    return f"{quantity:g} {unit}", int(round(quantity * per_unit))


def shelf_life_from_payload(payload: dict[str, Any]) -> tuple[str | None, int | None]:
    """Where shelf life lives in a stored Ocado product payload.

    Each adapter answers this differently — Ocado states a structured
    ``guaranteedProductLife`` object, Sainsbury's hides it in a display label —
    so ``backfill-shelf-life`` asks the adapter rather than knowing itself.
    """
    return parse_shelf_life(payload.get("guaranteedProductLife"))


class OcadoBrowserClient(BrowserJsonClient):
    """Fetch Ocado JSON from a real browser session without bypassing WAF."""

    warmup_url = f"{BASE_URL}/categories"

    def __init__(self, *, profile_dir: Path | None = None, headless: bool = False):
        super().__init__(
            profile_dir=profile_dir or (config.DATA_DIR / "ocado" / "browser-profile"),
            headless=headless,
        )

    def search(self, term: str, throttle: AdaptiveThrottle) -> dict[str, Any]:
        return self.json_fetch("GET", search_url(term), None, throttle)

    def products(self, skus: list[str], throttle: AdaptiveThrottle) -> Any:
        """Decorate a batch of ids. Ocado takes a bare JSON array of product ids."""
        return self.json_fetch("PUT", f"{BASE_URL}{PRODUCTS_PATH}", skus, throttle)


#: The name :func:`app.scraper.products.registry.client` resolves.
Client = OcadoBrowserClient


def _looks_product_id_key(key: str) -> bool:
    return key.lower() in {"id", "uuid", "sku", "productid", "product_id"}


def _is_product_like(node: dict[str, Any]) -> bool:
    return bool(_sku(node) and _string_field(node, "name", "title", "productName", "displayName"))


def _sku(node: dict[str, Any]) -> str | None:
    for key in ("sku", "id", "uuid", "productId", "product_id"):
        value = node.get(key)
        if isinstance(value, str):
            match = _UUID_RE.search(value)
            return match.group(0).lower() if match else value
    return None


def _string_field(node: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _string_field(value, "text", "value", "amount", "displayValue")
            if nested:
                return nested
    return None


def _brand(node: dict[str, Any]) -> str | None:
    value = node.get("brand")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _string_field(value, "name", "title")
    return _string_field(node, "brandName")


def _pack_size_raw(node: dict[str, Any], name: str) -> str | None:
    value = _string_field(
        node, "packSize", "packSizeDescription", "size", "weight", "netContent", "displaySize"
    )
    return value or pack_size_from_name(name)


#: Where the shelf price is, in the order the field names have been seen.
_PRICE_KEYS = ("price", "currentPrice", "nowPrice", "displayPrice")
#: Where the promotional price is when one is running. Ocado states it beside the
#: shelf price rather than replacing it, which is the opposite of Sainsbury's.
_PROMO_PRICE_KEYS = ("promoPrice",)
_UNIT_PRICE_KEYS = ("unitPrice", "pricePerUnit", "unitPriceText")
_PROMO_UNIT_PRICE_KEYS = ("promoUnitPrice",)


def _price(node: dict[str, Any]) -> float | None:
    """What it costs today.

    ``promoPrice`` when Ocado is running one, so a basket is priced at what the
    trolley will actually charge; multibuys ("any 3 for £5") set no promo price
    and are correctly left at the shelf price, since buying one of something is
    not three of it.
    """
    listed = _money_at(node, _PRICE_KEYS)
    promo = _money_at(node, _PROMO_PRICE_KEYS)
    if promo is not None and (listed is None or promo < listed):
        return promo
    return listed


def _base_price(node: dict[str, Any]) -> float | None:
    """The shelf price, kept only when a promotion is undercutting it."""
    return _base_of(_price(node), _money_at(node, _PRICE_KEYS))


def _money_at(node: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            parsed = _parse_money(value)
            if parsed is not None:
                return parsed
        if isinstance(value, dict):
            parsed = _parse_money(_string_field(value, "amount", "value", "text", "displayValue"))
            if parsed is not None:
                return parsed
    return None


def _unit_price(node: dict[str, Any]) -> tuple[float | None, str | None]:
    """Today's unit price, on the same terms as :func:`_price`.

    A promotional unit price is only taken when it is quoted on the same basis as
    the shelf one: Ocado restates the basis alongside it, and £5.65 per kg is not
    an improvement on £6.71 per each.
    """
    listed = _unit_money_at(node, _UNIT_PRICE_KEYS)
    promo = _unit_money_at(node, _PROMO_UNIT_PRICE_KEYS)
    if promo[0] is not None and promo[1] == listed[1] and (listed[0] is None or promo[0] < listed[0]):
        return promo
    return listed


def _base_unit_price(
    node: dict[str, Any], unit_price: float | None, unit_basis: str | None
) -> float | None:
    stated, stated_basis = _unit_money_at(node, _UNIT_PRICE_KEYS)
    if stated_basis != unit_basis:
        return None
    return _base_of(unit_price, stated)


def _unit_money_at(
    node: dict[str, Any], keys: tuple[str, ...]
) -> tuple[float | None, str | None]:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str):
            parsed = parse_unit_price(value)
            if parsed != (None, None):
                return parsed
        if isinstance(value, dict):
            price = value.get("price")
            if isinstance(price, dict):
                amount = _parse_money(price.get("amount"))
                if amount is not None and str(price.get("currency", "")).upper() == "GBX":
                    amount = amount / 100
                basis = _basis(
                    str(value.get("unitName") or value.get("unit") or "").split(".")[-1]
                )
                return amount, basis or None
            parsed = parse_unit_price(
                _string_field(value, "text", "value", "displayValue", "amount")
            )
            if parsed != (None, None):
                return parsed
    return None, None


def _category(node: dict[str, Any]) -> str | None:
    value = node.get("category") or node.get("aisle")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _string_field(value, "name", "title")
    breadcrumbs = node.get("breadcrumbs") or node.get("breadcrumb") or node.get("categoryPath")
    if isinstance(breadcrumbs, list):
        parts = []
        for entry in breadcrumbs:
            if isinstance(entry, str):
                parts.append(entry)
            elif isinstance(entry, dict):
                name = _string_field(entry, "name", "title")
                if name:
                    parts.append(name)
        return " > ".join(parts) if parts else None
    return None


def _is_frozen(node: dict[str, Any], category: str | None = None) -> bool:
    """Read Ocado's explicit Frozen chip, with its aisle as a fallback.

    ``Suitable for freezing`` is deliberately not frozen: the website gives it
    a different ``freezable`` icon, while frozen stock uses label/file
    ``Frozen``/``frozen``.
    """
    for key in ("iconAttributes", "icons"):
        values = node.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            label = str(value.get("label") or "").strip().lower()
            file_name = str(value.get("file") or "").strip().lower()
            if label == "frozen" or file_name == "frozen":
                return True
    return category_is_frozen(category if category is not None else _category(node))


def _rating(node: dict[str, Any]) -> tuple[float | None, int | None]:
    summary = node.get("ratingSummary")
    if not isinstance(summary, dict):
        return None, None
    avg = summary.get("overallRating") if summary.get("overallRating") is not None else summary.get("averageRating")
    count = summary.get("count") if summary.get("count") is not None else summary.get("numberOfRatings")
    try:
        avg_val = float(avg) if avg is not None else None
    except (TypeError, ValueError):
        avg_val = None
    try:
        count_val = int(count) if count is not None else None
    except (TypeError, ValueError):
        count_val = None
    return avg_val, count_val


def _in_stock(node: dict[str, Any]) -> bool | None:
    for key in ("available", "inStock", "isAvailable"):
        value = node.get(key)
        if isinstance(value, bool):
            return value
    return None


def _image_url(node: dict[str, Any]) -> str | None:
    value = node.get("imageUrl") or node.get("image_url")
    if isinstance(value, str):
        return urljoin(BASE_URL, value)
    image = node.get("image") or node.get("thumbnail")
    if isinstance(image, str):
        return urljoin(BASE_URL, image)
    if isinstance(image, dict):
        nested = _string_field(image, "url", "src", "path")
        return urljoin(BASE_URL, nested) if nested else None
    return None


def _url(node: dict[str, Any], sku: str) -> str:
    value = node.get("url") or node.get("productUrl") or node.get("href")
    if isinstance(value, str) and value:
        return urljoin(BASE_URL, value)
    # Ocado's canonical product path is /products/<slug>/<retailerProductId>; the
    # slug is cosmetic, so /products/<retailerProductId> 301-redirects to it. The
    # UUID sku is NOT a valid path (404), so prefer the retailer product id.
    retailer_pid = node.get("retailerProductId")
    if retailer_pid:
        return f"{BASE_URL}/products/{quote(str(retailer_pid))}"
    return product_url(sku)
