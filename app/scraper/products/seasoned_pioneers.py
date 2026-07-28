"""Seasoned Pioneers product adapter, over the public WooCommerce Store API.

Seasoned Pioneers is where a good number of HelloFresh's own spice blends come
from — they sell a "Hello Fresh Spices" bundle naming six of them — so their
catalogue closes the gap Ocado leaves on exactly the ingredients that otherwise
have to be hand-entered as manual products: "Chermoula Spice Mix", "Central
American Style Spice Mix", "North Indian Style Spice Mix" and the rest.

Unlike Ocado, this source is **not scraped on demand**. It is read from a
committed snapshot, ``app/data/seasoned_pioneers_catalogue.json``. Two reasons,
in order of how much they bind:

1. The store is fronted by a Cloudflare *managed challenge*. Plain HTTP is
   answered 403 on every path including the sitemap; a scripted browser gets a
   little way and is then blocked as well. The store has plainly configured
   itself to refuse automated clients, so the catalogue is captured by hand
   through an ordinary browser session and committed, rather than re-fetched by
   a job that would be both unreliable and unwelcome.
2. Even if it were freely fetchable, it would not want re-fetching often. This
   is dried spice: prices move on the order of a year. Ocado's weekly freshness
   argument simply does not apply, and a reviewable ``git diff`` of a snapshot is
   a better fit than a silent nightly overwrite.

So the shape here is *catalogue-first*: the whole (small, ~380 product) shop is
held locally and matching happens against those rows
(:mod:`app.mapping.external`), rather than issuing a search per ingredient. That
also sidesteps WooCommerce's fairly weak ``search`` parameter.

Two fields the Store API does not give us, and how they are handled:

* **Pack size** is rendered only into the product page HTML, as
  ``<div class="meta"><span><strong>SIZE</strong> 35g</span>``. It is not
  optional — without it :class:`~app.planner.index.Pack` has no capacity and the
  planner cannot cost the ingredient at all — so the snapshot carries a
  ``size_raw`` per product alongside the API payload. It is consistent across the
  catalogue: all 317 non-bundle products state one.
* **Shelf life** is not published anywhere. Dried spice keeps well past any
  weekly plan, so :data:`DEFAULT_SHELF_LIFE_DAYS` states that outright rather
  than leaving the waste model to infer it from a null.

Products are keyed ``sp:<woo product id>``, not on the store's own ``sku``
field: 67 of the 379 products have an empty ``sku`` (bundles and gift items),
which would collide under ``Product``'s unique ``(retailer, sku)``. The Woo id is
always present and stable. The EAN is kept in ``raw_json``.

Refreshing the snapshot
-----------------------
Capture is manual and deliberately so. From a normal browser on the store's own
origin, read every page of ``/wp-json/wc/store/v1/products?per_page=100``, then
fetch each product's ``permalink`` and pull ``SIZE`` out of its meta block with
:func:`parse_size_from_html`. Write the results as ``{"products": [...]}`` with a
``size_raw`` on each, and hand the file to::

    python -m app.scraper.products --retailer seasoned_pioneers refresh --from FILE

which validates it and rewrites :data:`CATALOGUE_PATH` in place, ready to review
as a diff.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from app.scraper.products.ocado import NormalizedProduct

RETAILER = "seasoned_pioneers"
#: Shown in the UI and on the shopping list, where "seasoned_pioneers" would read badly.
DISPLAY_NAME = "Seasoned Pioneers"
BASE_URL = "https://www.seasonedpioneers.com"
PRODUCTS_PATH = "/wp-json/wc/store/v1/products"
#: The committed catalogue. Lives beside the other reference data in app/data/
#: rather than under /data/, which is gitignored scrape output.
CATALOGUE_PATH = Path(__file__).resolve().parents[2] / "data" / "seasoned_pioneers_catalogue.json"

#: Dried spice outlives any weekly plan, and the store states no life of its own.
DEFAULT_SHELF_LIFE_DAYS = 365

_SKU_PREFIX = "sp:"

# Categories that are not a single buyable ingredient. Bundles and gift sets are
# excluded even though some are genuinely good value (the "Hello Fresh Spices"
# bundle is six of our highest-frequency blends for £19.95) because one pack
# covering one ingredient is the only shape the planner's arithmetic understands.
# Revisit as a multi-ingredient pack if it ever earns the complexity.
EXCLUDED_CATEGORY_SLUGS = frozenset(
    {
        "seasoning-collections",
        "spice-gifts-boxes",
        "foodie-gifts",
        "gift-wrap",
        "uncategorized",
    }
)

# "SIZE" from the product page's meta block. The store templates this identically
# on every product that has a weight; its absence is the signal that something is
# a bundle or a non-food item, and is used as a filter rather than an error.
_SIZE_RE = re.compile(r"<strong>\s*SIZE\s*</strong>\s*([^<]+)", re.I)
_PACK_RE = re.compile(r"(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>kg|g|l|litre|litres|ml)\b", re.I)


@dataclass(frozen=True)
class CatalogueEntry:
    """One Store API product plus the pack size scraped from its page."""

    woo_id: int
    payload: dict[str, Any]
    size_raw: str | None = None

    @property
    def sku(self) -> str:
        return product_sku(self.woo_id)


def product_sku(woo_id: int | str) -> str:
    return f"{_SKU_PREFIX}{woo_id}"


def is_catalogue_sku(sku: str) -> bool:
    return sku.startswith(_SKU_PREFIX)


def parse_pack_size(raw: str | None) -> tuple[float | None, str | None]:
    """Return (42.0, 'g') from "42g". Grams and millilitres, like Ocado's."""
    if not raw:
        return None, None
    match = _PACK_RE.search(raw.strip())
    if not match:
        return None, None
    value = float(match.group("size"))
    unit = match.group("unit").lower()
    if unit == "kg":
        return value * 1000, "g"
    if unit in {"l", "litre", "litres"}:
        return value * 1000, "ml"
    return value, unit


def parse_size_from_html(page_html: str) -> str | None:
    """Pull the ``SIZE`` field out of a product page's meta block."""
    match = _SIZE_RE.search(page_html)
    if not match:
        return None
    return html.unescape(match.group(1)).strip() or None


def is_saleable_ingredient(payload: dict[str, Any], size_raw: str | None) -> bool:
    """True when this row is a single buyable ingredient, not a bundle or gift.

    Both tests are needed. Most bundles have no ``SIZE`` and fall out on that
    alone, but a few gift boxes state a combined weight ("Gin & Tonic Garnish
    Box", 155g) and would otherwise pass; conversely the spice *tins* sit in a
    gift-adjacent category but are a perfectly ordinary 60g jar of one blend.
    """
    slugs = {c.get("slug") for c in payload.get("categories") or []}
    if slugs & EXCLUDED_CATEGORY_SLUGS:
        return False
    if payload.get("type") != "simple":
        # Variable products (the gift card) price as a range; nothing to cost.
        return False
    value, _unit = parse_pack_size(size_raw)
    return value is not None


def normalize_product(entry: CatalogueEntry) -> NormalizedProduct:
    """Map a Store API payload onto the shared :class:`NormalizedProduct`."""
    payload = entry.payload
    woo_id = payload.get("id")
    if woo_id is None:
        raise ValueError("product payload has no id")
    name = html.unescape(str(payload.get("name") or "")).strip()
    if not name:
        raise ValueError(f"product {woo_id} has no name")

    pack_value, pack_unit = parse_pack_size(entry.size_raw)
    price = _price(payload)
    unit_price, unit_basis = _unit_price(price, pack_value, pack_unit)

    return NormalizedProduct(
        retailer=RETAILER,
        sku=product_sku(woo_id),
        name=name,
        # Single-brand store: naming it explicitly is what lets the basket and
        # the review UI say where a line has to be ordered from.
        brand=DISPLAY_NAME,
        pack_size_raw=entry.size_raw,
        pack_size_value=pack_value,
        pack_size_unit=pack_unit,
        price=price,
        unit_price=unit_price,
        unit_price_basis=unit_basis,
        category=_category(payload),
        in_stock=bool(payload.get("is_in_stock")),
        shelf_life_raw=None,
        shelf_life_days=DEFAULT_SHELF_LIFE_DAYS,
        avg_rating=_float(payload.get("average_rating")),
        ratings_count=_int(payload.get("review_count")),
        image_url=_image_url(payload),
        url=_url(payload),
        raw_json=json.dumps(
            {"size_raw": entry.size_raw, "response": payload}, ensure_ascii=False
        ),
    )


def load_snapshot(path: Path | None = None) -> list[CatalogueEntry]:
    """Read the committed catalogue snapshot into entries.

    Raises ``ValueError`` on a malformed file rather than silently yielding an
    empty catalogue: a snapshot that fails to parse should stop a refresh, not
    quietly wipe every Seasoned Pioneers product on the next normalize.
    """
    source = path or CATALOGUE_PATH
    try:
        with source.open(encoding="utf-8") as fh:
            document = json.load(fh)
    except FileNotFoundError as exc:
        raise ValueError(f"no catalogue snapshot at {source}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"catalogue snapshot at {source} is not valid JSON: {exc}") from exc

    products = document.get("products") if isinstance(document, dict) else document
    if not isinstance(products, list) or not products:
        raise ValueError(f"catalogue snapshot at {source} has no products")

    entries: list[CatalogueEntry] = []
    for payload in products:
        if not isinstance(payload, dict):
            continue
        woo_id = payload.get("id")
        if woo_id is None:
            continue
        size_raw = payload.get("size_raw")
        entries.append(
            CatalogueEntry(
                woo_id=int(woo_id),
                payload=payload,
                size_raw=size_raw if isinstance(size_raw, str) else None,
            )
        )
    if not entries:
        raise ValueError(f"catalogue snapshot at {source} has no usable products")
    return entries


def snapshot_meta(path: Path | None = None) -> dict[str, Any]:
    """Provenance for the status command: where the snapshot came from and when."""
    source = path or CATALOGUE_PATH
    try:
        with source.open(encoding="utf-8") as fh:
            document = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    return {
        key: document.get(key)
        for key in ("source", "captured_at", "product_count")
        if document.get(key) is not None
    }


def write_snapshot(entries_document: Any, path: Path | None = None) -> int:
    """Validate a captured catalogue and write it over the committed snapshot.

    Deliberately strict about what it will accept, because the thing being
    overwritten is the only copy: a capture that lost the pack sizes would
    otherwise land as a catalogue in which nothing is buyable.
    """
    target = path or CATALOGUE_PATH
    products = (
        entries_document.get("products")
        if isinstance(entries_document, dict)
        else entries_document
    )
    if not isinstance(products, list) or not products:
        raise ValueError("captured catalogue has no products")

    sized = 0
    for payload in products:
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise ValueError("every captured product needs an id")
        if payload.get("size_raw"):
            sized += 1
    if not sized:
        raise ValueError(
            "no captured product carries a size_raw — the pack sizes were not "
            "collected, and without them nothing in the catalogue can be costed"
        )

    document = {
        "_comment": _snapshot_comment(),
        "retailer": RETAILER,
        "source": f"{BASE_URL}{PRODUCTS_PATH}",
        "captured_at": (
            entries_document.get("captured_at")
            if isinstance(entries_document, dict)
            else None
        )
        or date.today().isoformat(),
        "product_count": len(products),
        "products": sorted(products, key=lambda p: p["id"]),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    return len(products)


def _snapshot_comment() -> str:
    """Keep the committed file self-describing when it is rewritten."""
    existing = snapshot_meta()
    if existing:
        try:
            with CATALOGUE_PATH.open(encoding="utf-8") as fh:
                comment = json.load(fh).get("_comment")
            if isinstance(comment, str) and comment:
                return comment
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return (
        "Seasoned Pioneers catalogue snapshot from their public WooCommerce Store "
        "API, plus the pack size that only the product page states. See "
        "app/scraper/products/seasoned_pioneers.py for the refresh procedure."
    )


def _price(payload: dict[str, Any]) -> float | None:
    """Store API prices are integer minor units: "350" with minor_unit 2 is £3.50."""
    prices = payload.get("prices")
    if not isinstance(prices, dict):
        return None
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return None
    try:
        minor = int(prices.get("currency_minor_unit", 2))
    except (TypeError, ValueError):
        minor = 2
    return round(amount / (10**minor), 2)


def _unit_price(
    price: float | None, pack_value: float | None, pack_unit: str | None
) -> tuple[float | None, str | None]:
    """Derive the comparable unit price the review UI sorts on."""
    if price is None or not pack_value or pack_unit not in ("g", "ml"):
        return None, None
    return round(price / pack_value * 1000, 2), "kg" if pack_unit == "g" else "l"


def _category(payload: dict[str, Any]) -> str | None:
    names = [
        html.unescape(str(c.get("name")))
        for c in payload.get("categories") or []
        if c.get("name")
    ]
    return " > ".join(names) if names else None


def _image_url(payload: dict[str, Any]) -> str | None:
    for image in payload.get("images") or []:
        src = image.get("src") or image.get("thumbnail")
        if isinstance(src, str) and src:
            return urljoin(BASE_URL, src)
    return None


def _url(payload: dict[str, Any]) -> str:
    permalink = payload.get("permalink")
    if isinstance(permalink, str) and permalink:
        return permalink
    return f"{BASE_URL}/?p={payload.get('id')}"


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    # WooCommerce reports "0.00" for unrated products; that is absence, not 0★.
    return parsed or None


def _int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed or None
