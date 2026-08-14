"""Restartable retailer product-cache pipeline.

Every stage is parameterised by ``retailer`` and reaches the retailer-specific
parts through :func:`app.scraper.products.registry.get_adapter`, so one scrape of
Sainsbury's and one of Ocado are the same code over separate ``ProductScrapeState``
rows. Nothing here knows a field name or a URL.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Product, ProductScrapeState, ProductSearchHit
from app.db.session import ensure_columns
from app.retailers import DEFAULT_RETAILER
from app.scraper.products import registry, storage
from app.scraper.products.base import base_price, chunks
from app.scraper.products.registry import get_adapter
from app.scraper.products.worklist import IngredientWorkItem, load_worklist
from app.scraper.ratelimit import AdaptiveThrottle


#: Failures in a row that mean the *run* has stopped working, rather than that
#: this many products are individually bad. Ten is comfortably more than a run
#: hits by chance — the observed rate is roughly one transient in a hundred and
#: fifty — and small enough that a dead connection cannot get far.
MAX_CONSECUTIVE_FAILURES = 10


class ScrapeAborted(RuntimeError):
    """The run was stopped because it had stopped working."""


class _RunHealth:
    """Stops a broken run instead of letting it condemn its own worklist.

    This is the surviving half of something the browser stack used to do. A
    Chrome crash once failed every remaining item in a worklist — 179 searches
    succeeded, the browser closed, and the last 71 were each recorded as their
    own error without ever being tried. The browser is gone now, but that
    failure *shape* is not: a dropped connection or a shop that starts refusing
    everything would walk the rest of the worklist marking each row bad just as
    efficiently, and the rows would look permanently broken rather than merely
    interrupted.

    So the rule is kept and made transport-independent. One failure is a row's
    own problem; ten in a row is the run's, and the untried remainder is left
    alone for the next one.
    """

    def __init__(self, limit: int = MAX_CONSECUTIVE_FAILURES):
        self.limit = limit
        self.consecutive = 0

    def succeeded(self) -> None:
        self.consecutive = 0

    def failed(self, exc: BaseException) -> None:
        self.consecutive += 1
        if self.consecutive >= self.limit:
            raise ScrapeAborted(
                f"stopping the run after {self.consecutive} failures in a row; "
                f"the remaining worklist is untouched (last error: {exc})"
            ) from exc


@dataclass
class ProductStageResult:
    discovered_new: int = 0
    fetched: int = 0
    normalized: int = 0
    products: int = 0
    hits: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


def discover(
    session_factory: sessionmaker[Session],
    *,
    limit: int = 250,
    csv_path: Path | None = None,
    retailer: str = DEFAULT_RETAILER,
) -> ProductStageResult:
    adapter = get_adapter(retailer)
    result = ProductStageResult()
    items = load_worklist(csv_path, limit=limit)
    with session_factory() as session:
        existing = {
            row[0]
            for row in session.execute(
                select(ProductScrapeState.key).where(
                    ProductScrapeState.retailer == adapter.RETAILER,
                    ProductScrapeState.kind == "search",
                )
            )
        }
        rows = []
        for item in items:
            key = search_state_key(item)
            if key in existing:
                continue
            rows.append(
                ProductScrapeState(
                    retailer=adapter.RETAILER,
                    kind="search",
                    key=key,
                    label=_state_label(item),
                    url=adapter.search_url(item.name),
                    status="discovered",
                )
            )
        session.add_all(rows)
        session.commit()
        result.discovered_new = len(rows)
    result.notes.append(f"{len(items)} search terms in worklist, {result.discovered_new} new")
    return result


def fetch(
    session_factory: sessionmaker[Session],
    *,
    limit: int | None = None,
    retry_errors: bool = False,
    throttle: AdaptiveThrottle | None = None,
    retailer: str = DEFAULT_RETAILER,
) -> ProductStageResult:
    adapter = get_adapter(retailer)
    result = ProductStageResult()
    throttle = throttle or AdaptiveThrottle(workers=1, delay=1.5, max_delay=20.0)
    health = _RunHealth()
    with registry.client(retailer) as client:
        _fetch_searches(adapter, client, session_factory, result, limit, retry_errors, throttle, health)
        _fetch_products(adapter, client, session_factory, result, limit, retry_errors, throttle, health)
    return result


def normalize(
    session_factory: sessionmaker[Session],
    *,
    limit: int = 250,
    csv_path: Path | None = None,
    force: bool = False,
    retailer: str = DEFAULT_RETAILER,
) -> ProductStageResult:
    adapter = get_adapter(retailer)
    result = ProductStageResult()
    items = {search_state_key(item): item for item in load_worklist(csv_path, limit=limit)}
    _normalize_products(adapter, session_factory, result, force=force)
    _normalize_search_hits(adapter, session_factory, result, items, force=force)
    return result


# Columns added to an already-populated products table, name -> SQLite decl.
_PRODUCT_COLUMNS = {
    "shelf_life_raw": "VARCHAR(32)",
    "shelf_life_days": "INTEGER",
}

_BASE_PRICE_COLUMNS = {
    "base_price": "FLOAT",
    "base_unit_price": "FLOAT",
}


def backfill_prices(
    session_factory: sessionmaker[Session], *, retailer: str = DEFAULT_RETAILER
) -> ProductStageResult:
    """Recover each cached product's pre-promotion price from its raw payload.

    Both shops state the shelf price beside the offer, so this needs no re-fetch.

    What it deliberately does *not* do is move ``price``. A stored payload is as
    old as the scrape that wrote it, and adopting a promotional price out of a
    three-week-old Ocado response would fill the catalogue with discounts that
    ended a fortnight ago — worse than the shelf price it replaced, because it is
    wrong in the direction that makes a basket look cheap. The base is therefore
    written *relative to the price already on the row*: where the two agree there
    is nothing on offer as far as this catalogue is concerned, and the answer is
    NULL rather than a discount of zero. Today's price is the live refresh's job
    (:mod:`app.catalogue.status`), which reads both halves at once.

    Idempotent, and safe to run before either shop has been re-scraped.
    """
    adapter = get_adapter(retailer)
    result = ProductStageResult()
    with session_factory() as session:
        ensure_columns(session, "products", _BASE_PRICE_COLUMNS)

        products = session.scalars(
            select(Product).where(
                Product.retailer == adapter.RETAILER, Product.raw_json.is_not(None)
            )
        )
        for product in products:
            result.products += 1
            try:
                payload = json.loads(product.raw_json)
                normalized = adapter.normalize_product(payload)
            except (json.JSONDecodeError, ValueError):
                result.errors += 1
                continue
            product.base_price = base_price(
                product.price, normalized.base_price or normalized.price
            )
            product.base_unit_price = base_price(
                product.unit_price, normalized.base_unit_price or normalized.unit_price
            )
            if product.base_price is not None or product.base_unit_price is not None:
                result.normalized += 1
        session.commit()
    return result


def backfill_shelf_life(
    session_factory: sessionmaker[Session], *, retailer: str = DEFAULT_RETAILER
) -> ProductStageResult:
    """Re-derive shelf life from each cached product's stored raw payload.

    The field was already being captured inside ``raw_json``, so this needs no
    re-fetch. Idempotent: re-running it just recomputes the same values.
    """
    adapter = get_adapter(retailer)
    result = ProductStageResult()
    with session_factory() as session:
        ensure_columns(session, "products", _PRODUCT_COLUMNS)

        products = session.scalars(
            select(Product).where(Product.retailer == adapter.RETAILER, Product.raw_json.is_not(None))
        )
        for product in products:
            result.products += 1
            try:
                payload = json.loads(product.raw_json)
            except json.JSONDecodeError:
                result.errors += 1
                continue
            raw, days = adapter.shelf_life_from_payload(payload)
            product.shelf_life_raw = raw
            product.shelf_life_days = days
            if days is not None:
                result.normalized += 1
        session.commit()
    return result


def status_counts(
    session_factory: sessionmaker[Session], *, retailer: str = DEFAULT_RETAILER
) -> dict:
    adapter = get_adapter(retailer)
    with session_factory() as session:
        states = session.execute(
            select(ProductScrapeState.kind, ProductScrapeState.status, func.count())
            .where(ProductScrapeState.retailer == adapter.RETAILER)
            .group_by(ProductScrapeState.kind, ProductScrapeState.status)
        ).all()
        products = session.scalar(
            select(func.count()).select_from(Product).where(Product.retailer == adapter.RETAILER)
        ) or 0
        terms_with_hits = session.scalar(
            select(func.count(func.distinct(ProductSearchHit.ingredient_key))).where(
                ProductSearchHit.retailer == adapter.RETAILER
            )
        ) or 0
        hits = session.scalar(
            select(func.count()).select_from(ProductSearchHit).where(ProductSearchHit.retailer == adapter.RETAILER)
        ) or 0
        pack_parsed = session.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                Product.retailer == adapter.RETAILER,
                Product.pack_size_value.is_not(None),
                Product.pack_size_unit.is_not(None),
            )
        ) or 0
        unit_parsed = session.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                Product.retailer == adapter.RETAILER,
                Product.unit_price.is_not(None),
                Product.unit_price_basis.is_not(None),
            )
        ) or 0
        shelf_life = session.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.retailer == adapter.RETAILER, Product.shelf_life_days.is_not(None))
        ) or 0
    return {
        "states": {(kind, status): count for kind, status, count in states},
        "products": products,
        "terms_with_hits": terms_with_hits,
        "hits": hits,
        "pack_parsed": pack_parsed,
        "unit_parsed": unit_parsed,
        "shelf_life": shelf_life,
    }


def search_state_key(item: IngredientWorkItem) -> str:
    return f"{item.rank}:{item.ingredient_key}"


def _fetch_searches(
    adapter: ModuleType,
    client: Any,
    session_factory: sessionmaker[Session],
    result: ProductStageResult,
    limit: int | None,
    retry_errors: bool,
    throttle: AdaptiveThrottle,
    health: "_RunHealth",
) -> None:
    statuses = ["discovered", "error"] if retry_errors else ["discovered"]
    with session_factory() as session:
        stmt = (
            select(ProductScrapeState.id, ProductScrapeState.key, ProductScrapeState.label)
            .where(
                ProductScrapeState.retailer == adapter.RETAILER,
                ProductScrapeState.kind == "search",
                ProductScrapeState.status.in_(statuses),
            )
            .order_by(ProductScrapeState.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = list(session.execute(stmt))

    for state_id, key, label in rows:
        item = _item_from_label(label)
        try:
            response = client.search(item.name, throttle)
            product_ids = adapter.extract_product_ids(response)[:adapter.MAX_PRODUCTS_TO_DECORATE]
            decorated_products = adapter.extract_product_objects(response)
            decorated_skus = []
            for product in decorated_products:
                try:
                    normalized = adapter.normalize_product(product)
                except ValueError:
                    continue
                if product_ids and normalized.sku not in product_ids:
                    continue
                decorated_skus.append(normalized.sku)
                storage.write_raw(
                    adapter.RETAILER,
                    "product",
                    normalized.sku,
                    {"sku": normalized.sku, "response": product, "source": "search"},
                )
            if not product_ids:
                product_ids = decorated_skus
            envelope = {
                "search_term": item.name,
                "ingredient_key": item.ingredient_key,
                "term_rank": item.rank,
                "line_count": item.line_count,
                "product_ids": product_ids,
                "response": response,
            }
            storage.write_raw(adapter.RETAILER, "search", key, envelope)
            with session_factory() as session:
                _set_state(session, state_id, "fetched", None)
                _add_product_states(adapter, session, product_ids, fetched_skus=set(decorated_skus))
            result.fetched += 1
        except Exception as exc:  # noqa: BLE001 - keep scrape restartable
            with session_factory() as session:
                _set_state(session, state_id, "error", str(exc))
            result.errors += 1
            health.failed(exc)
        else:
            health.succeeded()


def _fetch_products(
    adapter: ModuleType,
    client: Any,
    session_factory: sessionmaker[Session],
    result: ProductStageResult,
    limit: int | None,
    retry_errors: bool,
    throttle: AdaptiveThrottle,
    health: "_RunHealth",
) -> None:
    statuses = ["discovered", "error"] if retry_errors else ["discovered"]
    with session_factory() as session:
        stmt = (
            select(ProductScrapeState.id, ProductScrapeState.key)
            .where(
                ProductScrapeState.retailer == adapter.RETAILER,
                ProductScrapeState.kind == "product",
                ProductScrapeState.status.in_(statuses),
            )
            .order_by(ProductScrapeState.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit * adapter.MAX_PRODUCTS_TO_DECORATE)
        rows = list(session.execute(stmt))

    by_key = {key: state_id for state_id, key in rows}
    for batch in chunks(list(by_key), adapter.MAX_PRODUCTS_TO_DECORATE):
        try:
            response = client.products(batch, throttle)
            objects = adapter.extract_product_objects(response)
            seen: set[str] = set()
            for product in objects:
                normalized = adapter.normalize_product(product)
                if normalized.sku not in by_key:
                    continue
                storage.write_raw(
                    adapter.RETAILER,
                    "product",
                    normalized.sku,
                    {"sku": normalized.sku, "response": product},
                )
                seen.add(normalized.sku)
                with session_factory() as session:
                    _set_state(session, by_key[normalized.sku], "fetched", None)
                result.fetched += 1
            missing = set(batch) - seen
            for sku in missing:
                with session_factory() as session:
                    _set_state(session, by_key[sku], "error", f"product missing from the {adapter.RETAILER} response")
                result.errors += 1
        except Exception as exc:  # noqa: BLE001 - keep scrape restartable
            for sku in batch:
                with session_factory() as session:
                    _set_state(session, by_key[sku], "error", str(exc))
                result.errors += 1
            health.failed(exc)
        else:
            health.succeeded()


def _normalize_products(
    adapter: ModuleType,
    session_factory: sessionmaker[Session],
    result: ProductStageResult,
    *,
    force: bool,
) -> None:
    statuses = ["fetched", "normalized"] if force else ["fetched"]
    with session_factory() as session:
        rows = list(
            session.execute(
                select(ProductScrapeState.id, ProductScrapeState.key)
                .where(
                    ProductScrapeState.retailer == adapter.RETAILER,
                    ProductScrapeState.kind == "product",
                    ProductScrapeState.status.in_(statuses),
                )
                .order_by(ProductScrapeState.id)
            )
        )
    for state_id, sku in rows:
        try:
            raw = storage.read_raw(adapter.RETAILER, "product", sku)
            payload = raw.get("response", raw)
            product = adapter.normalize_product(payload)
            with session_factory() as session:
                upsert_product(session, product)
                _set_state(session, state_id, "normalized", None)
            result.normalized += 1
            result.products += 1
        except Exception as exc:  # noqa: BLE001
            with session_factory() as session:
                _set_state(session, state_id, "error", f"normalize failed: {exc}")
            result.errors += 1


def _normalize_search_hits(
    adapter: ModuleType,
    session_factory: sessionmaker[Session],
    result: ProductStageResult,
    items: dict[str, IngredientWorkItem],
    *,
    force: bool,
) -> None:
    statuses = ["fetched", "normalized"] if force else ["fetched"]
    with session_factory() as session:
        rows = list(
            session.execute(
                select(ProductScrapeState.id, ProductScrapeState.key)
                .where(
                    ProductScrapeState.retailer == adapter.RETAILER,
                    ProductScrapeState.kind == "search",
                    ProductScrapeState.status.in_(statuses),
                )
                .order_by(ProductScrapeState.id)
            )
        )
    for state_id, key in rows:
        item = items.get(key)
        if item is None:
            continue
        try:
            raw = storage.read_raw(adapter.RETAILER, "search", key)
            product_ids = raw.get("product_ids") or adapter.extract_product_ids(raw.get("response"))
            decorated_products = adapter.extract_product_objects(raw.get("response"))
            with session_factory() as session:
                decorated_skus = []
                for product_payload in decorated_products:
                    try:
                        product = adapter.normalize_product(product_payload)
                    except ValueError:
                        continue
                    if product_ids and product.sku not in product_ids:
                        continue
                    storage.write_raw(
                        adapter.RETAILER,
                        "product",
                        product.sku,
                        {"sku": product.sku, "response": product_payload, "source": "search"},
                    )
                    upsert_product(session, product)
                    decorated_skus.append(product.sku)
                if not product_ids:
                    product_ids = decorated_skus
                if decorated_skus:
                    _add_product_states(adapter, session, decorated_skus, fetched_skus=set(decorated_skus))
                    _mark_product_states_normalized(adapter.RETAILER, session, set(decorated_skus))
                session.execute(
                    delete(ProductSearchHit).where(
                        ProductSearchHit.retailer == adapter.RETAILER,
                        ProductSearchHit.ingredient_key == item.ingredient_key,
                    )
                )
                for rank, sku in enumerate(product_ids, start=1):
                    product = session.scalar(
                        select(Product).where(Product.retailer == adapter.RETAILER, Product.sku == sku)
                    )
                    if product is None:
                        continue
                    session.add(
                        ProductSearchHit(
                            product_id=product.id,
                            retailer=adapter.RETAILER,
                            ingredient_key=item.ingredient_key,
                            search_term=item.name,
                            term_rank=item.rank,
                            line_count=item.line_count,
                            sku=sku,
                            result_rank=rank,
                        )
                    )
                    result.hits += 1
                _set_state(session, state_id, "normalized", None)
        except Exception as exc:  # noqa: BLE001
            with session_factory() as session:
                _set_state(session, state_id, "error", f"normalize failed: {exc}")
            result.errors += 1


def upsert_product(session: Session, product) -> Product:
    existing = session.scalar(
        select(Product).where(Product.retailer == product.retailer, Product.sku == product.sku)
    )
    if existing is None:
        existing = Product(retailer=product.retailer, sku=product.sku, name=product.name)
        session.add(existing)
    existing.name = product.name
    existing.brand = product.brand
    existing.pack_size_raw = product.pack_size_raw
    existing.pack_size_value = product.pack_size_value
    existing.pack_size_unit = product.pack_size_unit
    existing.price = product.price
    existing.unit_price = product.unit_price
    existing.unit_price_basis = product.unit_price_basis
    existing.base_price = product.base_price
    existing.base_unit_price = product.base_unit_price
    existing.category = product.category
    existing.is_frozen = product.is_frozen
    existing.in_stock = product.in_stock
    # A scrape *is* a stock reading, so it dates one - otherwise a basket built
    # straight after a scrape claims its stock has never been checked at all.
    if product.in_stock is not None:
        existing.stock_checked_at = datetime.now(timezone.utc)
    existing.shelf_life_raw = product.shelf_life_raw
    existing.shelf_life_days = product.shelf_life_days
    existing.avg_rating = product.avg_rating
    existing.ratings_count = product.ratings_count
    existing.image_url = product.image_url
    existing.url = product.url
    existing.raw_json = product.raw_json
    existing.scraped_at = datetime.now(timezone.utc)
    session.commit()
    return existing


def _add_product_states(
    adapter: ModuleType,
    session: Session,
    skus: list[str],
    *,
    fetched_skus: set[str] | None = None,
) -> None:
    fetched_skus = fetched_skus or set()
    if not skus:
        session.commit()
        return
    existing = {
        row.key: row
        for row in session.scalars(
            select(ProductScrapeState).where(
                ProductScrapeState.retailer == adapter.RETAILER,
                ProductScrapeState.kind == "product",
                ProductScrapeState.key.in_(skus),
            )
        )
    }
    for sku in skus:
        if sku not in existing:
            session.add(
                ProductScrapeState(
                    retailer=adapter.RETAILER,
                    kind="product",
                    key=sku,
                    label=sku,
                    url=adapter.product_url(sku),
                    status="fetched" if sku in fetched_skus else "discovered",
                )
            )
        elif sku in fetched_skus and existing[sku].status in {"discovered", "error"}:
            existing[sku].status = "fetched"
            existing[sku].error_message = None
            existing[sku].fetched_at = datetime.now(timezone.utc)
    session.commit()


def _mark_product_states_normalized(
    retailer: str, session: Session, skus: set[str]
) -> None:
    if not skus:
        return
    now = datetime.now(timezone.utc)
    for state in session.scalars(
        select(ProductScrapeState).where(
            ProductScrapeState.retailer == retailer,
            ProductScrapeState.kind == "product",
            ProductScrapeState.key.in_(skus),
        )
    ):
        state.status = "normalized"
        state.error_message = None
        state.fetched_at = state.fetched_at or now
        state.normalized_at = now
    session.commit()


def _set_state(session: Session, state_id: int, status: str, error: str | None) -> None:
    state = session.get(ProductScrapeState, state_id)
    if state is None:
        return
    state.status = status
    state.error_message = error
    state.attempts += 1
    now = datetime.now(timezone.utc)
    if status == "fetched":
        state.fetched_at = now
    if status == "normalized":
        state.normalized_at = now
    session.commit()


def _state_label(item: IngredientWorkItem) -> str:
    return json.dumps(
        {
            "rank": item.rank,
            "ingredient_key": item.ingredient_key,
            "name": item.name,
            "line_count": item.line_count,
        },
        ensure_ascii=False,
    )


def _item_from_label(label: str | None) -> IngredientWorkItem:
    if not label:
        raise ValueError("search state missing worklist label")
    data = json.loads(label)
    return IngredientWorkItem(
        rank=int(data["rank"]),
        ingredient_key=data["ingredient_key"],
        name=data["name"],
        line_count=int(data["line_count"]),
    )
