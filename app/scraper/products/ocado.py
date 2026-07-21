"""Ocado product adapter and browser-session JSON fetcher."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlencode, urljoin

from app import config
from app.scraper.ratelimit import AdaptiveThrottle

RETAILER = "ocado"
BASE_URL = "https://www.ocado.com"
SEARCH_PATH = "/api/webproductpagews/v6/product-pages/search"
PRODUCTS_PATH = "/api/webproductpagews/v6/products"
MAX_PRODUCTS_TO_DECORATE = 50

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
_MONEY_RE = re.compile(r"(£\s*)?(\d+(?:\.\d+)?)\s*(p)?", re.I)
_PACK_MULTI_RE = re.compile(
    r"(?P<count>\d+(?:\.\d+)?)\s*x\s*(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>kg|g|l|litre|litres|ml)\b",
    re.I,
)
_PACK_SINGLE_RE = re.compile(r"(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>kg|g|l|litre|litres|ml)\b", re.I)
_PACK_EACH_RE = re.compile(r"(?P<count>\d+(?:\.\d+)?)\s*(?:per\s+pack|pack|pk|ct|count|items?)\b", re.I)
_UNIT_PRICE_RE = re.compile(
    r"(?P<money>£\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?p)\s*(?:/|per\s+)(?P<basis>100g|kg|kilo|litre|liter|l|ml|item|each)",
    re.I,
)


@dataclass(frozen=True)
class NormalizedProduct:
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
    category: str | None = None
    in_stock: bool | None = None
    avg_rating: float | None = None
    ratings_count: int | None = None
    image_url: str | None = None
    url: str | None = None
    raw_json: str | None = None


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
    avg_rating, ratings_count = _rating(payload)

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
        category=_category(payload),
        in_stock=_in_stock(payload),
        avg_rating=avg_rating,
        ratings_count=ratings_count,
        image_url=_image_url(payload),
        url=_url(payload, sku),
        raw_json=json.dumps(payload, ensure_ascii=False),
    )


def parse_pack_size(raw: str | None) -> tuple[float | None, str | None]:
    if not raw:
        return None, None
    text = raw.strip()
    multi = _PACK_MULTI_RE.search(text)
    if multi:
        value = float(multi.group("count")) * float(multi.group("size"))
        return _metric(value, multi.group("unit"))
    single = _PACK_SINGLE_RE.search(text)
    if single:
        return _metric(float(single.group("size")), single.group("unit"))
    each = _PACK_EACH_RE.search(text)
    if each:
        return float(each.group("count")), "each"
    return None, None


def parse_unit_price(raw: str | None) -> tuple[float | None, str | None]:
    if not raw:
        return None, None
    match = _UNIT_PRICE_RE.search(raw)
    if not match:
        return None, None
    return _parse_money(match.group("money")), _basis(match.group("basis"))


class OcadoBrowserClient:
    """Fetch Ocado JSON from a real browser session without bypassing WAF."""

    def __init__(self, *, profile_dir: Path | None = None, headless: bool = False):
        self.profile_dir = profile_dir or (config.DATA_DIR / "ocado" / "browser-profile")
        self.headless = headless
        self._playwright = None
        self._context = None
        self._page = None

    def __enter__(self) -> "OcadoBrowserClient":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - exercised only without optional dep
            raise RuntimeError("playwright is required for Ocado browser-session fetching") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                channel="chrome",
            )
        except Exception:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless
            )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto(f"{BASE_URL}/categories", wait_until="domcontentloaded")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()

    def search(self, term: str, throttle: AdaptiveThrottle) -> dict[str, Any]:
        return self._json_fetch("GET", search_url(term), None, throttle)

    def products(self, skus: list[str], throttle: AdaptiveThrottle) -> Any:
        return self._json_fetch("PUT", f"{BASE_URL}{PRODUCTS_PATH}", skus, throttle)

    def _json_fetch(
        self, method: str, url: str, body: Any, throttle: AdaptiveThrottle
    ) -> dict[str, Any] | list[Any]:
        if self._page is None:
            raise RuntimeError("OcadoBrowserClient must be used as a context manager")

        _before_request_sync(throttle)
        result = self._page.evaluate(
            """async ({ method, url, body }) => {
                const options = { method, headers: { "Accept": "application/json; charset=utf-8" } };
                if (body !== null) {
                    options.headers["Content-Type"] = "application/json; charset=utf-8";
                    options.body = JSON.stringify(body);
                }
                const response = await fetch(url, options);
                const text = await response.text();
                return {
                    status: response.status,
                    contentType: response.headers.get("content-type") || "",
                    text
                };
            }""",
            {"method": method, "url": url, "body": body},
        )
        status = int(result["status"])
        content_type = result["contentType"]
        text = result["text"]
        if status != 200 or "json" not in content_type.lower():
            _on_throttle_sync(throttle)
            raise RuntimeError(f"Ocado returned {status} {content_type or 'unknown content-type'}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            _on_throttle_sync(throttle)
            raise RuntimeError("Ocado returned non-JSON content") from exc
        _on_success_sync(throttle)
        return payload


def _before_request_sync(throttle: AdaptiveThrottle) -> None:
    if throttle.delay > 0:
        time.sleep(throttle.delay)


def _on_success_sync(throttle: AdaptiveThrottle) -> None:
    if throttle.delay > 0:
        throttle.delay = max(0.0, throttle.delay * throttle.recover_factor - 0.01)


def _on_throttle_sync(throttle: AdaptiveThrottle) -> None:
    throttle.delay = min(
        throttle.max_delay, throttle.delay * throttle.backoff_factor + throttle.backoff_floor
    )


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
    if value:
        return value
    match = re.search(r"(?:,\s*)?(\d+(?:\.\d+)?\s*(?:kg|g|l|litre|litres|ml)\b)", name, re.I)
    return match.group(1) if match else None


def _price(node: dict[str, Any]) -> float | None:
    for key in ("price", "currentPrice", "nowPrice", "displayPrice"):
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
    for key in ("unitPrice", "pricePerUnit", "unitPriceText"):
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
    return product_url(sku)


def _parse_money(value: str | int | float | None) -> float | None:
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


def _metric(value: float, unit: str) -> tuple[float, str]:
    u = unit.lower()
    if u == "kg":
        return value * 1000, "g"
    if u in {"l", "litre", "litres"}:
        return value * 1000, "ml"
    return value, u


def _basis(value: str) -> str:
    basis = value.lower()
    if basis in {"kilo", "kg", "per_1kg"}:
        return "kg"
    if basis in {"litre", "liter", "l", "per_litre"}:
        return "l"
    if basis in {"item", "each", "per_each"}:
        return "each"
    return basis


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]
