"""Catalogue-first product sync, for retailers small enough to hold entirely.

The Ocado pipeline is search-first: it has to be, because you cannot enumerate a
supermarket. Seasoned Pioneers is ~380 products held in a committed snapshot
(see :mod:`app.scraper.products.seasoned_pioneers` for why it is a snapshot and
not a scrape), so the cheaper and far more accurate thing is to load the whole
catalogue and match against it locally (:mod:`app.mapping.external`) — no
per-ingredient search, and no dependence on the store's own search relevance.

With no network stage there is nothing for Ocado's discover/fetch/normalize split
to mean, so this is one idempotent :func:`sync`. It still writes
``product_scrape_state`` rows, which are what make the result auditable: after a
sync you can ask which products were taken, which were set aside and why, without
re-reading the snapshot.

Products that turn out not to be a single buyable ingredient end at status
``skipped`` rather than ``error``: bundles and gift sets are a normal and expected
sixth of this catalogue, not a failure, and keeping them distinct means a genuine
breakage still shows up in the error count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Product, ProductScrapeState
from app.db.session import ensure_columns
from app.scraper.products import storage
from app.scraper.products.pipeline import _PRODUCT_COLUMNS, upsert_product
from app.scraper.products.seasoned_pioneers import (
    RETAILER,
    is_saleable_ingredient,
    load_snapshot,
    normalize_product,
    snapshot_meta,
)


@dataclass
class CatalogueStageResult:
    seen: int = 0
    normalized: int = 0
    skipped: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


def sync(
    session_factory: sessionmaker[Session],
    *,
    path: Path | None = None,
    cache_raw: bool = True,
) -> CatalogueStageResult:
    """Load the catalogue snapshot into ``products``, idempotently.

    Safe to re-run: products are upserted on ``(retailer, sku)`` and state rows
    are updated in place, so a re-sync after a snapshot refresh moves prices
    without duplicating anything or orphaning an accepted mapping.
    """
    result = CatalogueStageResult()
    entries = load_snapshot(path)
    result.seen = len(entries)

    with session_factory() as session:
        # A database created before shelf life was captured has no column to
        # write it to, and every product here states one. ``create_all`` only
        # makes whole tables, so the column has to be added explicitly — the
        # same step ``pipeline.backfill_shelf_life`` takes for Ocado.
        ensure_columns(session, "products", _PRODUCT_COLUMNS)

        states = {
            row.key: row
            for row in session.scalars(
                select(ProductScrapeState).where(
                    ProductScrapeState.retailer == RETAILER,
                    ProductScrapeState.kind == "product",
                )
            )
        }

        for entry in entries:
            sku = entry.sku
            state = states.get(sku)
            if state is None:
                state = ProductScrapeState(
                    retailer=RETAILER, kind="product", key=sku, status="discovered"
                )
                session.add(state)
                states[sku] = state
            state.label = str(entry.payload.get("name") or sku)
            state.url = str(entry.payload.get("permalink") or "") or None
            # A row added in this pass has not been flushed, so its column
            # defaults have not been applied yet and attempts is still None.
            state.attempts = (state.attempts or 0) + 1
            state.fetched_at = state.fetched_at or _now()

            try:
                if not is_saleable_ingredient(entry.payload, entry.size_raw):
                    _mark(state, "skipped", None)
                    result.skipped += 1
                    continue
                normalized = normalize_product(entry)
                if cache_raw:
                    # Mirrors the Ocado scrape's raw cache, so the same
                    # "what did the source actually say" question is answerable
                    # for both retailers from the same place.
                    storage.write_raw(
                        RETAILER,
                        "product",
                        sku,
                        {"sku": sku, "size_raw": entry.size_raw, "response": entry.payload},
                    )
                upsert_product(session, normalized)
                _mark(state, "normalized", None)
                result.normalized += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sync
                _mark(state, "error", str(exc))
                result.errors += 1
        session.commit()

    meta = snapshot_meta(path)
    captured = meta.get("captured_at")
    result.notes.append(
        f"{result.seen} products in snapshot"
        + (f" (captured {captured})" if captured else "")
        + f", {result.normalized} synced, {result.skipped} skipped"
    )
    return result


def status_counts(session_factory: sessionmaker[Session]) -> dict:
    with session_factory() as session:
        states = session.execute(
            select(ProductScrapeState.status, func.count())
            .where(
                ProductScrapeState.retailer == RETAILER,
                ProductScrapeState.kind == "product",
            )
            .group_by(ProductScrapeState.status)
        ).all()
        products = session.scalar(
            select(func.count()).select_from(Product).where(Product.retailer == RETAILER)
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
        priced = session.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.retailer == RETAILER, Product.price.is_not(None))
        ) or 0
        in_stock = session.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.retailer == RETAILER, Product.in_stock == 1)
        ) or 0
    return {
        "states": {status: count for status, count in states},
        "products": products,
        "pack_parsed": pack_parsed,
        "priced": priced,
        "in_stock": in_stock,
        "snapshot": snapshot_meta(),
    }


def _mark(state: ProductScrapeState, status: str, error: str | None) -> None:
    state.status = status
    state.error_message = error
    if status in ("normalized", "skipped"):
        state.normalized_at = _now()


def _now() -> datetime:
    return datetime.now(timezone.utc)
