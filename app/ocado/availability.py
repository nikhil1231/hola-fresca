"""Live stock and price for a set of SKUs, and the write-back into the catalogue.

``products.in_stock`` is a *scrape cache*: it records what Ocado stocked the last
time the product pipeline ran, which may be weeks ago, and the planner believes it
when it picks which pack to buy. That is how a week gets built around a product
Ocado will not sell you, with the drop only surfacing at push time — too late for
the planner to have chosen differently.

This module is the fresh read. ``PUT /api/webproductpagews/v6/products`` takes a
bare array of product ids and answers with price and availability. It needs no
login, only the CSRF token any page carries, so the basket page can call it on
demand; fifty ids per call is the batch size Ocado's own web client uses.

The results are written back onto ``products`` rather than kept beside them. That
is what makes this cheap: the planner index is a pure function of the database
and :mod:`app.planner.cache` rebuilds whenever the file is touched, so writing a
sold-out flag re-covers every affected ingredient on the next basket build — no
substitution machinery of its own required.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Product
from app.ocado.session import OcadoSession, get_shared_session
from app.scraper.products.ocado import _in_stock, _price, _sku

log = logging.getLogger(__name__)

PRODUCTS_PATH = "/api/webproductpagews/v6/products"
RETAILER = "ocado"

#: Ocado's own web client decorates fifty products per call.
BATCH_SIZE = 50

#: A week of recipes maps to a few hundred candidate packs; this is the ceiling
#: on one refresh so a pathological basket cannot turn into a hundred requests.
MAX_SKUS = 750

_PRODUCT_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


@dataclass(frozen=True, slots=True)
class ProductStatus:
    """What Ocado says about one product right now."""

    sku: str
    available: bool
    price: float | None = None
    name: str | None = None
    #: Set when Ocado did not return the id at all — retired, or never valid.
    unlisted: bool = False


@dataclass(slots=True)
class StockRefresh:
    """The outcome of one refresh, in the terms the UI reports it."""

    checked_at: datetime
    checked: int = 0
    available: int = 0
    sold_out: list[str] = field(default_factory=list)
    restocked: list[str] = field(default_factory=list)
    repriced: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.sold_out) + len(self.restocked) + len(self.repriced)


def fetch_statuses(
    skus: Sequence[str], *, session: OcadoSession | None = None
) -> dict[str, ProductStatus]:
    """Ask Ocado about ``skus``, in batches. Unknown ids come back unavailable.

    An id Ocado omits from its answer is one it will not sell — a delisted
    product reads exactly like a sold-out one from the basket's point of view —
    so it is reported rather than silently dropped. An id that is not a product
    id at all is a different matter: it is skipped and left out of the answer
    entirely, because it is not Ocado's to have an opinion about and a rejected
    batch would take the whole request down with it.
    """
    session = session or get_shared_session()
    wanted = [sku for sku in dict.fromkeys(skus) if _is_product_id(sku)][:MAX_SKUS]
    statuses: dict[str, ProductStatus] = {}
    state = _Fetch(budget=_request_budget(len(wanted)))

    for start in range(0, len(wanted), BATCH_SIZE):
        _fetch_batch(session, wanted[start : start + BATCH_SIZE], statuses, state)

    if wanted and not state.answered:
        # Nothing came back at all, which is a fact about the connection rather
        # than about the shelves. Reporting it as "everything is sold out" would
        # write an empty warehouse into the catalogue.
        raise RuntimeError("Ocado answered none of the stock requests")

    for sku in wanted:
        if sku not in statuses:
            statuses[sku] = ProductStatus(sku=sku, available=False, unlisted=True)
    return statuses


@dataclass(slots=True)
class _Fetch:
    """Shared state for one run: the request allowance, and whether Ocado replied."""

    budget: int
    answered: int = 0


def _request_budget(count: int) -> int:
    """Enough calls for the batches themselves plus a bisect or two per batch."""
    batches = max(1, -(-count // BATCH_SIZE))
    return batches * 12


def _fetch_batch(
    session: OcadoSession,
    batch: list[str],
    statuses: dict[str, ProductStatus],
    state: _Fetch,
) -> None:
    """Decorate one batch, halving it around whatever Ocado chokes on.

    A single retired product id makes the endpoint answer 500 for the *entire*
    batch, so one dead SKU in the mapping would otherwise cost the basket its
    whole stock check. Splitting isolates the offender in a handful of extra
    calls; alone and still failing, it is left out of the answer, which the
    caller already reads as "will not sell you this".
    """
    if not batch or state.budget <= 0:
        return
    state.budget -= 1
    try:
        response = session.request("PUT", PRODUCTS_PATH, json=batch)
        response.raise_for_status()
    except Exception:  # noqa: BLE001 - the id that caused it is what matters
        if len(batch) == 1:
            log.info("ocado stock: %s could not be decorated, treating as unlisted", batch[0])
            return
        half = len(batch) // 2
        _fetch_batch(session, batch[:half], statuses, state)
        _fetch_batch(session, batch[half:], statuses, state)
        return

    state.answered += 1
    for node in _product_nodes(response.json() if response.content else {}):
        status = _status(node)
        if status is not None:
            statuses[status.sku] = status


def refresh_stock(
    factory: sessionmaker[Session],
    skus: Sequence[str],
    *,
    session: OcadoSession | None = None,
    retailer: str = RETAILER,
) -> StockRefresh:
    """Refresh ``skus`` against the live site and write the answer to the catalogue."""
    checked_at = datetime.now(timezone.utc)
    statuses = _trustworthy(fetch_statuses(skus, session=session))
    if not statuses:
        return StockRefresh(checked_at=checked_at)

    with factory() as db:
        rows = db.scalars(
            select(Product).where(
                Product.retailer == retailer, Product.sku.in_(list(statuses))
            )
        ).all()
        result = _apply_statuses(rows, statuses, checked_at)
        db.commit()

    log.info(
        "ocado stock: checked %d, %d available, %d sold out, %d restocked, %d repriced",
        result.checked,
        result.available,
        len(result.sold_out),
        len(result.restocked),
        len(result.repriced),
    )
    return result


#: Above this share of ids Ocado would not answer for, the run is read as a
#: partial outage rather than as a shelf full of delistings. Real delistings in
#: an approved shortlist are a handful; a fifth of it is a broken connection.
MAX_UNLISTED_SHARE = 0.2


def _trustworthy(statuses: dict[str, ProductStatus]) -> dict[str, ProductStatus]:
    """Drop the "never answered" verdicts when there are too many to believe.

    Writing them would mark a quarter of the catalogue sold out on the strength
    of a wobble, and the planner would then price a week around substitutes for
    products that are sitting on the shelf.
    """
    unlisted = [sku for sku, status in statuses.items() if status.unlisted]
    if not unlisted or len(unlisted) <= MAX_UNLISTED_SHARE * len(statuses):
        return statuses
    log.warning(
        "ocado stock: %d of %d ids went unanswered - treating the run as unreliable "
        "and leaving their stock alone",
        len(unlisted),
        len(statuses),
    )
    return {sku: status for sku, status in statuses.items() if not status.unlisted}


def mark_unavailable(
    factory: sessionmaker[Session],
    skus: Iterable[str],
    *,
    retailer: str = RETAILER,
) -> int:
    """Record that Ocado refused these products, so the next cover avoids them.

    The push is the most authoritative availability signal there is — the cart
    itself declined the item — and it arrives for free. Believing it costs one
    write and spares the next basket the same drop.
    """
    wanted = [sku for sku in dict.fromkeys(skus) if sku]
    if not wanted:
        return 0
    now = datetime.now(timezone.utc)
    with factory() as db:
        rows = db.scalars(
            select(Product).where(Product.retailer == retailer, Product.sku.in_(wanted))
        ).all()
        for row in rows:
            row.in_stock = 0
            row.stock_checked_at = now
        db.commit()
        return len(rows)


def _apply_statuses(
    rows: Sequence[Product], statuses: dict[str, ProductStatus], checked_at: datetime
) -> StockRefresh:
    result = StockRefresh(checked_at=checked_at)
    for row in rows:
        status = statuses.get(row.sku)
        if status is None:
            continue
        result.checked += 1
        result.available += 1 if status.available else 0

        was_in_stock = row.in_stock is None or bool(row.in_stock)
        if status.available and not was_in_stock:
            result.restocked.append(row.sku)
        elif not status.available and was_in_stock:
            result.sold_out.append(row.sku)

        # A price that moved matters as much as one that vanished: the pack
        # arithmetic is priced in pounds, so a stale price picks the wrong pack.
        if status.price is not None and row.price != status.price:
            result.repriced.append(row.sku)
            row.price = status.price

        row.in_stock = 1 if status.available else 0
        row.stock_checked_at = checked_at
    return result


def _is_product_id(sku: str | None) -> bool:
    """Ocado product ids are UUIDs, and it rejects a whole batch containing anything else.

    Other retailers' SKUs are prefixed (``manual:``, ``sp:``) and reach here only
    by accident, so the shape is a reliable filter.
    """
    return bool(sku) and _PRODUCT_ID_RE.fullmatch(sku) is not None


def _product_nodes(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [node for node in payload if isinstance(node, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("products", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [node for node in value if isinstance(node, dict)]
    return []


def _status(node: dict[str, Any]) -> ProductStatus | None:
    sku = _sku(node)
    if not sku:
        return None
    available = _in_stock(node)
    name = node.get("name")
    return ProductStatus(
        sku=sku,
        # Ocado states availability on every product it returns; a payload that
        # somehow omits it is taken at its word that the product exists.
        available=True if available is None else available,
        price=_price(node),
        name=name if isinstance(name, str) else None,
    )
