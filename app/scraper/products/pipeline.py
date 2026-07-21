"""Restartable retailer product-cache pipeline."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Product, ProductScrapeState, ProductSearchHit
from app.scraper.products import storage
from app.scraper.products.ocado import (
    MAX_PRODUCTS_TO_DECORATE,
    RETAILER,
    OcadoBrowserClient,
    chunks,
    extract_product_ids,
    extract_product_objects,
    normalize_product,
    search_url,
)
from app.scraper.products.worklist import IngredientWorkItem, load_worklist
from app.scraper.ratelimit import AdaptiveThrottle


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
    session_factory: sessionmaker[Session], *, limit: int = 250, csv_path: Path | None = None
) -> ProductStageResult:
    result = ProductStageResult()
    items = load_worklist(csv_path, limit=limit)
    with session_factory() as session:
        existing = {
            row[0]
            for row in session.execute(
                select(ProductScrapeState.key).where(
                    ProductScrapeState.retailer == RETAILER,
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
                    retailer=RETAILER,
                    kind="search",
                    key=key,
                    label=_state_label(item),
                    url=search_url(item.name),
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
    headless: bool = False,
    throttle: AdaptiveThrottle | None = None,
) -> ProductStageResult:
    result = ProductStageResult()
    throttle = throttle or AdaptiveThrottle(workers=1, delay=1.5, max_delay=20.0)
    with OcadoBrowserClient(headless=headless) as client:
        _fetch_searches(client, session_factory, result, limit, retry_errors, throttle)
        _fetch_products(client, session_factory, result, limit, retry_errors, throttle)
    return result


def normalize(
    session_factory: sessionmaker[Session],
    *,
    limit: int = 250,
    csv_path: Path | None = None,
    force: bool = False,
) -> ProductStageResult:
    result = ProductStageResult()
    items = {search_state_key(item): item for item in load_worklist(csv_path, limit=limit)}
    _normalize_products(session_factory, result, force=force)
    _normalize_search_hits(session_factory, result, items, force=force)
    return result


def status_counts(session_factory: sessionmaker[Session]) -> dict:
    with session_factory() as session:
        states = session.execute(
            select(ProductScrapeState.kind, ProductScrapeState.status, func.count())
            .where(ProductScrapeState.retailer == RETAILER)
            .group_by(ProductScrapeState.kind, ProductScrapeState.status)
        ).all()
        products = session.scalar(
            select(func.count()).select_from(Product).where(Product.retailer == RETAILER)
        ) or 0
        terms_with_hits = session.scalar(
            select(func.count(func.distinct(ProductSearchHit.ingredient_key))).where(
                ProductSearchHit.retailer == RETAILER
            )
        ) or 0
        hits = session.scalar(
            select(func.count()).select_from(ProductSearchHit).where(ProductSearchHit.retailer == RETAILER)
        ) or 0
        pack_parsed = session.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                Product.retailer == RETAILER,
                Product.pack_size_value.is_not(None),
                Product.pack_size_unit.is_not(None),
            )
        ) or 0
        unit_parsed = session.scalar(
            select(func.count())
            .select_from(Product)
            .where(
                Product.retailer == RETAILER,
                Product.unit_price.is_not(None),
                Product.unit_price_basis.is_not(None),
            )
        ) or 0
    return {
        "states": {(kind, status): count for kind, status, count in states},
        "products": products,
        "terms_with_hits": terms_with_hits,
        "hits": hits,
        "pack_parsed": pack_parsed,
        "unit_parsed": unit_parsed,
    }


def search_state_key(item: IngredientWorkItem) -> str:
    return f"{item.rank}:{item.ingredient_key}"


def _fetch_searches(
    client: OcadoBrowserClient,
    session_factory: sessionmaker[Session],
    result: ProductStageResult,
    limit: int | None,
    retry_errors: bool,
    throttle: AdaptiveThrottle,
) -> None:
    statuses = ["discovered", "error"] if retry_errors else ["discovered"]
    with session_factory() as session:
        stmt = (
            select(ProductScrapeState.id, ProductScrapeState.key, ProductScrapeState.label)
            .where(
                ProductScrapeState.retailer == RETAILER,
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
            product_ids = extract_product_ids(response)[:MAX_PRODUCTS_TO_DECORATE]
            decorated_products = extract_product_objects(response)
            decorated_skus = []
            for product in decorated_products:
                try:
                    normalized = normalize_product(product)
                except ValueError:
                    continue
                if product_ids and normalized.sku not in product_ids:
                    continue
                decorated_skus.append(normalized.sku)
                storage.write_raw(
                    RETAILER,
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
            storage.write_raw(RETAILER, "search", key, envelope)
            with session_factory() as session:
                _set_state(session, state_id, "fetched", None)
                _add_product_states(session, product_ids, fetched_skus=set(decorated_skus))
            result.fetched += 1
        except Exception as exc:  # noqa: BLE001 - keep scrape restartable
            with session_factory() as session:
                _set_state(session, state_id, "error", str(exc))
            result.errors += 1


def _fetch_products(
    client: OcadoBrowserClient,
    session_factory: sessionmaker[Session],
    result: ProductStageResult,
    limit: int | None,
    retry_errors: bool,
    throttle: AdaptiveThrottle,
) -> None:
    statuses = ["discovered", "error"] if retry_errors else ["discovered"]
    with session_factory() as session:
        stmt = (
            select(ProductScrapeState.id, ProductScrapeState.key)
            .where(
                ProductScrapeState.retailer == RETAILER,
                ProductScrapeState.kind == "product",
                ProductScrapeState.status.in_(statuses),
            )
            .order_by(ProductScrapeState.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit * MAX_PRODUCTS_TO_DECORATE)
        rows = list(session.execute(stmt))

    by_key = {key: state_id for state_id, key in rows}
    for batch in chunks(list(by_key), MAX_PRODUCTS_TO_DECORATE):
        try:
            response = client.products(batch, throttle)
            objects = extract_product_objects(response)
            seen: set[str] = set()
            for product in objects:
                normalized = normalize_product(product)
                if normalized.sku not in by_key:
                    continue
                storage.write_raw(
                    RETAILER,
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
                    _set_state(session, by_key[sku], "error", "product missing from Ocado response")
                result.errors += 1
        except Exception as exc:  # noqa: BLE001 - keep scrape restartable
            for sku in batch:
                with session_factory() as session:
                    _set_state(session, by_key[sku], "error", str(exc))
                result.errors += 1


def _normalize_products(
    session_factory: sessionmaker[Session], result: ProductStageResult, *, force: bool
) -> None:
    statuses = ["fetched", "normalized"] if force else ["fetched"]
    with session_factory() as session:
        rows = list(
            session.execute(
                select(ProductScrapeState.id, ProductScrapeState.key)
                .where(
                    ProductScrapeState.retailer == RETAILER,
                    ProductScrapeState.kind == "product",
                    ProductScrapeState.status.in_(statuses),
                )
                .order_by(ProductScrapeState.id)
            )
        )
    for state_id, sku in rows:
        try:
            raw = storage.read_raw(RETAILER, "product", sku)
            payload = raw.get("response", raw)
            product = normalize_product(payload)
            with session_factory() as session:
                _upsert_product(session, product)
                _set_state(session, state_id, "normalized", None)
            result.normalized += 1
            result.products += 1
        except Exception as exc:  # noqa: BLE001
            with session_factory() as session:
                _set_state(session, state_id, "error", f"normalize failed: {exc}")
            result.errors += 1


def _normalize_search_hits(
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
                    ProductScrapeState.retailer == RETAILER,
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
            raw = storage.read_raw(RETAILER, "search", key)
            product_ids = raw.get("product_ids") or extract_product_ids(raw.get("response"))
            decorated_products = extract_product_objects(raw.get("response"))
            with session_factory() as session:
                decorated_skus = []
                for product_payload in decorated_products:
                    try:
                        product = normalize_product(product_payload)
                    except ValueError:
                        continue
                    if product_ids and product.sku not in product_ids:
                        continue
                    storage.write_raw(
                        RETAILER,
                        "product",
                        product.sku,
                        {"sku": product.sku, "response": product_payload, "source": "search"},
                    )
                    _upsert_product(session, product)
                    decorated_skus.append(product.sku)
                if not product_ids:
                    product_ids = decorated_skus
                if decorated_skus:
                    _add_product_states(session, decorated_skus, fetched_skus=set(decorated_skus))
                    _mark_product_states_normalized(session, set(decorated_skus))
                session.execute(
                    delete(ProductSearchHit).where(
                        ProductSearchHit.retailer == RETAILER,
                        ProductSearchHit.ingredient_key == item.ingredient_key,
                    )
                )
                for rank, sku in enumerate(product_ids, start=1):
                    product = session.scalar(
                        select(Product).where(Product.retailer == RETAILER, Product.sku == sku)
                    )
                    if product is None:
                        continue
                    session.add(
                        ProductSearchHit(
                            product_id=product.id,
                            retailer=RETAILER,
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


def _upsert_product(session: Session, product) -> Product:
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
    existing.category = product.category
    existing.in_stock = product.in_stock
    existing.avg_rating = product.avg_rating
    existing.ratings_count = product.ratings_count
    existing.image_url = product.image_url
    existing.url = product.url
    existing.raw_json = product.raw_json
    existing.scraped_at = datetime.now(timezone.utc)
    session.commit()
    return existing


def _add_product_states(
    session: Session, skus: list[str], *, fetched_skus: set[str] | None = None
) -> None:
    fetched_skus = fetched_skus or set()
    if not skus:
        session.commit()
        return
    existing = {
        row.key: row
        for row in session.scalars(
            select(ProductScrapeState).where(
                ProductScrapeState.retailer == RETAILER,
                ProductScrapeState.kind == "product",
                ProductScrapeState.key.in_(skus),
            )
        )
    }
    for sku in skus:
        if sku not in existing:
            session.add(
                ProductScrapeState(
                    retailer=RETAILER,
                    kind="product",
                    key=sku,
                    label=sku,
                    url=f"https://www.ocado.com/products/{sku}",
                    status="fetched" if sku in fetched_skus else "discovered",
                )
            )
        elif sku in fetched_skus and existing[sku].status in {"discovered", "error"}:
            existing[sku].status = "fetched"
            existing[sku].error_message = None
            existing[sku].fetched_at = datetime.now(timezone.utc)
    session.commit()


def _mark_product_states_normalized(session: Session, skus: set[str]) -> None:
    if not skus:
        return
    now = datetime.now(timezone.utc)
    for state in session.scalars(
        select(ProductScrapeState).where(
            ProductScrapeState.retailer == RETAILER,
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
