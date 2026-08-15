"""Live stock and price for a set of SKUs, and the write-back into the catalogue.

``products`` is a *scrape cache*: stock and price record what a shop said the
last time the product pipeline ran, which may be weeks ago, and the planner
believes both when it picks which pack to buy. That is how a week gets built
around a product the shop will not sell you, or priced at a promotion that ended
a fortnight ago.

This module is the fresh read, for whichever shop the basket is being priced at.
How the read is *made* is the retailer's own business and lives in its adapter —
Ocado answers an anonymous HTTP call, Sainsbury's needs a browser session —
so nothing here branches on a retailer name; it asks the adapter whether it can
do a live read and reports :class:`LiveStatusUnsupported` when it cannot.

The results are written back onto ``products`` rather than kept beside them. That
is what makes this cheap: the planner index is a pure function of the database
and :mod:`app.planner.cache` rebuilds whenever the file is touched, so writing a
sold-out flag re-covers every affected ingredient on the next basket build — no
substitution machinery of its own required.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Product
from app.retailers import DEFAULT_RETAILER
from app.scraper.products.base import ProductStatus
from app.scraper.products.registry import get_adapter

log = logging.getLogger(__name__)

#: Above this share of ids a shop would not answer for, the run is read as a
#: partial outage rather than as a shelf full of delistings. Real delistings in
#: an approved shortlist are a handful; a fifth of it is a broken connection.
MAX_UNLISTED_SHARE = 0.2


class LiveStatusUnsupported(RuntimeError):
    """This retailer's adapter cannot do a live read (yet)."""


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


def supports_live_status(retailer: str) -> bool:
    """Whether a basket at ``retailer`` can be refreshed against the live shop."""
    return hasattr(get_adapter(retailer), "fetch_statuses")


def fetch_statuses(
    skus: Sequence[str], *, retailer: str = DEFAULT_RETAILER
) -> dict[str, ProductStatus]:
    """Ask ``retailer`` about ``skus``, through whichever path its adapter uses."""
    adapter = get_adapter(retailer)
    fetch = getattr(adapter, "fetch_statuses", None)
    if fetch is None:
        raise LiveStatusUnsupported(f"{retailer} has no live stock and price read")
    return fetch(list(skus))


def refresh_stock(
    factory: sessionmaker[Session],
    skus: Sequence[str],
    *,
    retailer: str = DEFAULT_RETAILER,
    fetch: Callable[[Sequence[str]], dict[str, ProductStatus]] | None = None,
) -> StockRefresh:
    """Refresh ``skus`` against the live shop and write the answer to the catalogue.

    ``fetch`` overrides how the shop is asked, for the callers that already hold
    a signed-in session and should not have the shared one used underneath them
    (:func:`app.ocado.availability.refresh_stock`).
    """
    checked_at = datetime.now(timezone.utc)
    read = fetch or (lambda ids: fetch_statuses(ids, retailer=retailer))
    statuses = _trustworthy(read(skus), retailer)
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
        "%s stock: checked %d, %d available, %d sold out, %d restocked, %d repriced",
        retailer,
        result.checked,
        result.available,
        len(result.sold_out),
        len(result.restocked),
        len(result.repriced),
    )
    return result


def mark_unavailable(
    factory: sessionmaker[Session],
    skus: Iterable[str],
    *,
    retailer: str = DEFAULT_RETAILER,
) -> int:
    """Record that the shop refused these products, so the next cover avoids them.

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


def _trustworthy(
    statuses: dict[str, ProductStatus], retailer: str
) -> dict[str, ProductStatus]:
    """Drop the "never answered" verdicts when there are too many to believe.

    Writing them would mark a quarter of the catalogue sold out on the strength
    of a wobble, and the planner would then price a week around substitutes for
    products that are sitting on the shelf.
    """
    unlisted = [sku for sku, status in statuses.items() if status.unlisted]
    if not unlisted or len(unlisted) <= MAX_UNLISTED_SHARE * len(statuses):
        return statuses
    log.warning(
        "%s stock: %d of %d ids went unanswered - treating the run as unreliable "
        "and leaving their stock alone",
        retailer,
        len(unlisted),
        len(statuses),
    )
    return {sku: status for sku, status in statuses.items() if not status.unlisted}


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
        _apply_prices(row, status)

        row.in_stock = 1 if status.available else 0
        row.stock_checked_at = checked_at
    return result


def _apply_prices(row: Product, status: ProductStatus) -> None:
    """Write the four price fields, keeping today's and the shelf's in step.

    A promotion that has *ended* since the scrape is the case worth being careful
    about: the shop simply stops mentioning it, so the base price has to be
    cleared rather than left behind, or the product would keep advertising a
    discount off a price nobody is charging. Both halves therefore move together
    whenever the live read stated a price at all, and are left entirely alone
    when it did not.
    """
    if status.price is not None:
        row.price = status.price
        row.base_price = status.base_price
        # A stated live price is also an authoritative end to an old Nectar
        # offer when the retailer no longer marks it as one.
        row.is_nectar_price = status.is_nectar_price
    if status.unit_price is not None:
        row.unit_price = status.unit_price
        row.base_unit_price = status.base_unit_price
        if status.unit_price_basis is not None:
            row.unit_price_basis = status.unit_price_basis
