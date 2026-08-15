"""Sainsbury's product adapter.

Every path here is taken from traffic captured off the live site, and one choice
is worth recording because the obvious route is the wrong one.

Sainsbury's runs two front-ends. The newer ``/groceries`` app is a Next.js build
whose product data arrives through *server actions* — POSTs carrying a
``next-action`` header whose value is a build hash that changes on every deploy,
gated behind an A/B cookie the server only sets for a browser it likes. Anything
built on that breaks the next time they ship.

Underneath it, the older ``/gol-ui`` SPA — which is what a fresh session is
actually served — talks to a plain REST API that has been stable for years. That
is what this module uses. It is the same catalogue.

Reaching that API needs no browser, which took two goes to establish. The
endpoints are served to a caller with no cookies, no session and no warm-up
navigation; what the Akamai edge refuses is the *TLS handshake* of a Python HTTP
client, and it refuses it identically whether the request is cold or arrives on
the back of a perfectly good browser profile. That is why driving a headed
Chrome worked and every attempt to trim it down did not — the browser was never
supplying trust, only a handshake. Presenting a browser's handshake directly
(:mod:`app.scraper.products.http_session`) is answered the same way, from a
headless host, in about half a second.

Two shapes differ from Ocado and are easy to get wrong:

* There is **no pack-size field**. The weight is in the product title
  ("...Chorizo Ring 225g"), so :func:`base.pack_size_from_name` does the work.
* Shelf life is not a field either — it is a **display label**, one of a list
  that also carries marketing badges ("ALDI PRICE MATCH*"). "Typical life 14
  days" is parsed out of that list. Note *typical*, where Ocado states a
  guaranteed *minimum*: the two are not quite the same promise, and Sainsbury's
  numbers therefore run slightly optimistic against Ocado's for the same food.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlencode

from app.scraper.products.base import (
    NormalizedProduct,
    ProductStatus,
    category_is_frozen,
    base_price,
    basis,
    chunks,
    pack_count_from_name,
    pack_size_from_name,
    parse_pack_size,
)
from app.scraper.products.http_session import HttpJsonClient
from app.scraper.ratelimit import AdaptiveThrottle

RETAILER = "sainsburys"
BASE_URL = "https://www.sainsburys.co.uk"
API_PREFIX = "/groceries-api/gol-services"
SEARCH_PATH = f"{API_PREFIX}/product/v1/product"
PRODUCTS_PATH = f"{API_PREFIX}/product/v1/product"

#: Results per search. Sixty is what the site's own search page requests.
PAGE_SIZE = 60
#: Mirrors Ocado's ``MAX_PRODUCTS_TO_DECORATE`` so the pipeline can treat the two
#: adapters alike; the search response is already fully decorated here.
MAX_PRODUCTS_TO_DECORATE = PAGE_SIZE
#: How many ids the bulk ``uids=`` endpoint is asked for at once.
PRODUCT_BATCH_SIZE = 50

#: "Typical life 14 days", "Typical life 3 months". The unit is spelled out.
_LIFE_LABEL_RE = re.compile(
    r"typical\s+life\s+(?P<quantity>\d+(?:\.\d+)?)\s*(?P<unit>day|week|month|year)s?", re.I
)
_LIFE_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

#: Storage labels, which stand in for the aisle when the category list is all
#: marketing. See :func:`_category`.
_STORAGE_LABELS = {"chilled": "Fresh & Chilled Food", "frozen": "Frozen Food"}

#: Category names that describe an offer rather than an aisle. Sainsbury's files
#: a product under both, and the promotional ones would otherwise win by being
#: first — a bag of potatoes shelved under "5x Nectar points" tells the waste
#: model nothing about how long a potato keeps.
_PROMO_CATEGORY_RE = re.compile(
    r"nectar|aldi price match|price match|low everyday|best of british|"
    r"our best value|stamford street|essentials$",
    re.I,
)


def search_url(term: str, *, page: int = 1, page_size: int = PAGE_SIZE) -> str:
    params = {
        "filter[keyword]": term,
        "page_number": str(page),
        "page_size": str(page_size),
        "sort_order": "FAVOURITES_FIRST",
    }
    return f"{BASE_URL}{SEARCH_PATH}?{urlencode(params)}"


def products_url(skus: list[str]) -> str:
    """Bulk re-read for a batch of ids — the stock/price refresh path."""
    return f"{BASE_URL}{PRODUCTS_PATH}?{urlencode({'uids': ','.join(skus)})}"


def product_url(sku: str) -> str:
    return f"{BASE_URL}/gol-ui/product/{quote(sku)}"


def extract_product_ids(payload: Any) -> list[str]:
    """The ids in a search response, in the order the retailer ranked them."""
    seen: set[str] = set()
    ids: list[str] = []
    for product in _products(payload):
        sku = _sku(product)
        if sku and sku not in seen:
            seen.add(sku)
            ids.append(sku)
    return ids


def extract_product_objects(payload: Any) -> list[dict[str, Any]]:
    """Product rows from a search or bulk response, deduplicated by id.

    Unlike Ocado's, this does not walk the whole tree looking for product-shaped
    dicts: the response is a flat ``{"products": [...]}`` and the only other
    objects in it are ad slots, which are not products and must not be treated as
    ones.
    """
    deduped: dict[str, dict[str, Any]] = {}
    for product in _products(payload):
        sku = _sku(product)
        if sku and sku not in deduped:
            deduped[sku] = product
    return list(deduped.values())


def normalize_product(payload: dict[str, Any]) -> NormalizedProduct:
    sku = _sku(payload)
    if not sku:
        raise ValueError("product payload has no product_uid")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"product {sku} has no name")
    name = name.strip()

    unit_price, unit_basis = _unit_price(payload)
    base_unit = _base_unit_price(payload, unit_price, unit_basis)
    pack_raw = pack_size_from_name(name)
    pack_value, pack_unit = parse_pack_size(pack_raw)
    if pack_value is None and _priced_by_each(payload):
        # Loose produce: "Sainsbury's Baking Potatoes x4", priced per item. The
        # count is only believed when the retailer's own unit price agrees the
        # thing is sold by the each, so a weight-priced title that happens to
        # carry a count ("Chorizo Slices x34 170g") is never read as 34 packs.
        count = pack_count_from_name(name)
        if count is not None:
            pack_raw, pack_value, pack_unit = f"x{count:g}", count, "each"
    life_raw, life_days = parse_shelf_life(payload.get("labels"))

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
        is_nectar_price=_is_nectar_price(payload),
        category=category,
        is_frozen=category_is_frozen(category),
        in_stock=_in_stock(payload),
        shelf_life_raw=life_raw,
        shelf_life_days=life_days,
        avg_rating=_rating(payload)[0],
        ratings_count=_rating(payload)[1],
        image_url=_image_url(payload),
        url=_url(payload, sku),
        raw_json=json.dumps(payload, ensure_ascii=False),
    )


def product_status(node: dict[str, Any]) -> ProductStatus | None:
    """One product's live stock and prices, from a bulk-endpoint node."""
    sku = _sku(node)
    if not sku:
        return None
    unit_price, unit_basis = _unit_price(node)
    available = _in_stock(node)
    name = node.get("name")
    return ProductStatus(
        sku=sku,
        available=True if available is None else available,
        price=_price(node),
        base_price=_base_price(node),
        unit_price=unit_price,
        unit_price_basis=unit_basis,
        base_unit_price=_base_unit_price(node, unit_price, unit_basis),
        is_nectar_price=_is_nectar_price(node),
        name=name if isinstance(name, str) else None,
    )


def fetch_statuses(skus: list[str]) -> dict[str, ProductStatus]:
    """Live stock and prices for ``skus``.

    One HTTP request per fifty ids, sharing the retailer's session and backoff
    with the live search through :mod:`app.mapping.live_search` — so a basket
    refresh and a reviewer's re-search cannot separately decide how hard to push
    the shop. Nothing is launched to serve it; the runner is there for the
    shared throttle and connection, not for a browser.

    An id the shop does not answer for is reported ``unlisted`` rather than
    dropped: a delisted product looks exactly like a sold-out one to a basket.
    """
    from app.mapping.live_search import get_runner

    wanted = [sku for sku in dict.fromkeys(skus) if sku]
    if not wanted:
        return {}

    runner = get_runner(RETAILER)
    statuses: dict[str, ProductStatus] = {}
    answered = False
    for batch in chunks(wanted, PRODUCT_BATCH_SIZE):
        payload = runner.run(lambda client, batch=batch: client.products(batch, runner.throttle))
        answered = True
        for node in extract_product_objects(payload):
            status = product_status(node)
            if status is not None:
                statuses[status.sku] = status

    if not answered:
        raise RuntimeError("Sainsbury's answered none of the stock requests")

    for sku in wanted:
        if sku not in statuses:
            statuses[sku] = ProductStatus(sku=sku, available=False, unlisted=True)
    return statuses


def parse_shelf_life(labels: Any) -> tuple[str | None, int | None]:
    """Return ("14 DAY", 14) from the display-label list.

    Months and years are approximated at 30/365 days, as for Ocado. A product
    with no life label returns ``(None, None)`` — "this shop does not say", which
    the waste model then covers from the category.
    """
    if not isinstance(labels, list):
        return None, None
    for label in labels:
        text = _label_text(label)
        if not text:
            continue
        match = _LIFE_LABEL_RE.search(text)
        if not match:
            continue
        quantity = float(match.group("quantity"))
        unit = match.group("unit").lower()
        per_unit = _LIFE_UNIT_DAYS.get(unit)
        if per_unit is None or quantity <= 0:
            continue
        return f"{quantity:g} {unit.upper()}", int(round(quantity * per_unit))
    return None, None


def shelf_life_from_payload(payload: dict[str, Any]) -> tuple[str | None, int | None]:
    """Where shelf life lives in a stored Sainsbury's product payload."""
    return parse_shelf_life(payload.get("labels"))


class SainsburysClient(HttpJsonClient):
    """Fetch Sainsbury's JSON over HTTP, fingerprinted as a browser."""

    referer = f"{BASE_URL}/gol-ui/groceries"

    def search(self, term: str, throttle: AdaptiveThrottle) -> dict[str, Any]:
        return self.json_fetch("GET", search_url(term), None, throttle)

    def products(self, skus: list[str], throttle: AdaptiveThrottle) -> Any:
        """Re-read a batch of ids. Sainsbury's takes them as a query parameter."""
        return self.json_fetch("GET", products_url(skus), None, throttle)


#: The name :func:`app.scraper.products.registry.client` resolves.
Client = SainsburysClient


def _products(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    products = payload.get("products")
    if not isinstance(products, list):
        return []
    return [item for item in products if isinstance(item, dict)]


def _sku(node: dict[str, Any]) -> str | None:
    """The id the catalogue is keyed by.

    ``product_uid`` rather than ``sainId``: it is the one the bulk ``uids=``
    endpoint accepts, so it is the only one a stock refresh can use.
    """
    value = node.get("product_uid")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return None


def _label_text(label: Any) -> str | None:
    if isinstance(label, str):
        return label
    if isinstance(label, dict):
        for key in ("text", "label_uid", "alt_text"):
            value = label.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _money(node: Any) -> float | None:
    if not isinstance(node, dict):
        return None
    value = node.get("price")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _price(node: dict[str, Any]) -> float | None:
    """What it costs today.

    ``retail_price`` already carries the promotional price where one applies —
    ``promotions[].original_price`` is the struck-through figure — so a basket is
    priced at what would actually be charged. That includes Nectar prices, which
    are only charged to a shopper who scans a card; :func:`_base_price` is the
    other half of that bargain, and what the mapping sorts on.
    """
    return _money(node.get("retail_price"))


def _base_price(node: dict[str, Any]) -> float | None:
    """The list price, from the dearest promotion that undercuts today's.

    Dearest rather than first: a product can carry several promotions at once
    (a Nectar price *and* a multibuy), and the one that says what the shelf
    charges without any of them is the highest original price on offer.
    """
    price = _price(node)
    promotions = node.get("promotions")
    if price is None or not isinstance(promotions, list):
        return None
    originals = [
        float(promotion["original_price"])
        for promotion in promotions
        if isinstance(promotion, dict)
        and isinstance(promotion.get("original_price"), (int, float))
        and not isinstance(promotion.get("original_price"), bool)
    ]
    return base_price(price, max(originals)) if originals else None


def _is_nectar_price(node: dict[str, Any]) -> bool:
    """Whether today's stated price is tied to a confirmed Nectar offer."""
    promotions = node.get("promotions")
    if _base_price(node) is None or not isinstance(promotions, list):
        return False
    return any(
        isinstance(promotion, dict) and promotion.get("is_nectar") is True
        for promotion in promotions
    )


def _base_unit_price(
    node: dict[str, Any], unit_price: float | None, unit_basis: str | None
) -> float | None:
    """The list unit price, which Sainsbury's states outright.

    Only believed when it is quoted on the same basis as the price it would be
    compared against: ``original_unit_price`` is a separate object with its own
    measure, and £14 per litre against £7 per 100 ml is not a discount.
    """
    original = node.get("original_unit_price")
    stated = _money(original)
    if stated is None or not isinstance(original, dict):
        return None
    _, stated_basis = _unit_money(original)
    if stated_basis != unit_basis:
        return None
    return base_price(unit_price, stated)


def _unit_price(node: dict[str, Any]) -> tuple[float | None, str | None]:
    return _unit_money(node.get("unit_price"))


def _unit_money(unit: Any) -> tuple[float | None, str | None]:
    """One of Sainsbury's unit-price objects, as an amount and a basis."""
    price = _money(unit)
    if price is None or not isinstance(unit, dict):
        return None, None
    measure = str(unit.get("measure") or "").strip()
    if not measure:
        return price, None
    amount = unit.get("measure_amount")
    # Almost always 1 ("per kg"), but a "per 100g" basis is stated as
    # amount=100, measure="g" and has to keep the multiplier to mean anything.
    if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount != 1:
        return price, f"{amount:g}{measure.lower()}"
    return price, basis(measure)


def _priced_by_each(node: dict[str, Any]) -> bool:
    """Whether the retailer's own unit price is per item rather than per weight."""
    for key in ("unit_price", "retail_price"):
        value = node.get(key)
        if isinstance(value, dict):
            measure = str(value.get("measure") or "").strip().lower()
            if measure in {"ea", "each", "item"}:
                return True
    return False


def _in_stock(node: dict[str, Any]) -> bool | None:
    value = node.get("is_available")
    return value if isinstance(value, bool) else None


def _brand(node: dict[str, Any]) -> str | None:
    """Only the bulk/detail responses carry a brand; search results do not."""
    attributes = node.get("attributes")
    if not isinstance(attributes, dict):
        return None
    brands = attributes.get("brand")
    if isinstance(brands, list):
        for brand in brands:
            if isinstance(brand, str) and brand.strip():
                return brand.strip()
    if isinstance(brands, str) and brands.strip():
        return brands.strip()
    return None


def _category(node: dict[str, Any]) -> str | None:
    """The aisle, as a path the waste model can read.

    Sainsbury's ``categories`` mixes real shelving with promotional collections,
    so the marketing ones are dropped first. A storage label ("Chilled",
    "Frozen") is prepended when present, because that is the single most useful
    fact about how long the thing keeps and Sainsbury's states it separately from
    the aisle.
    """
    segments: list[str] = []
    for label in node.get("labels") or []:
        text = _label_text(label)
        if text and text.strip().lower() in _STORAGE_LABELS:
            segments.append(_STORAGE_LABELS[text.strip().lower()])
            break

    categories = node.get("categories")
    if isinstance(categories, list):
        for entry in categories:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            if _PROMO_CATEGORY_RE.search(name) or name in segments:
                continue
            segments.append(name)

    return " > ".join(segments) if segments else None


def _rating(node: dict[str, Any]) -> tuple[float | None, int | None]:
    reviews = node.get("reviews")
    if not isinstance(reviews, dict):
        return None, None
    average = reviews.get("average_rating")
    total = reviews.get("total")
    try:
        average_val = float(average) if average is not None else None
    except (TypeError, ValueError):
        average_val = None
    try:
        total_val = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_val = None
    return average_val, total_val


def _image_url(node: dict[str, Any]) -> str | None:
    value = node.get("image")
    if isinstance(value, str) and value.strip():
        return value.strip()
    assets = node.get("assets")
    if isinstance(assets, dict):
        plp = assets.get("plp_image")
        if isinstance(plp, str) and plp.strip():
            return plp.strip()
    return None


def _url(node: dict[str, Any], sku: str) -> str:
    value = node.get("full_url")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return product_url(sku)
