"""Ocado's live stock and price read.

``PUT /api/webproductpagews/v6/products`` takes a bare array of product ids and
answers with price and availability. It needs no login, only the CSRF token any
page carries, so the basket page can call it on demand; fifty ids per call is the
batch size Ocado's own web client uses. That is the whole of what is Ocado-shaped
about a refresh — Sainsbury's asks the same question through a browser session —
so the write-back into ``products`` lives in :mod:`app.catalogue`. What stays
here is the read, plus the thin ``refresh_stock`` that pairs it with one
account's session; ``mark_unavailable`` is re-exported for the callers that have
always reached for it at this address.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy.orm import Session, sessionmaker

from app import catalogue
from app.catalogue import (  # noqa: F401  (re-exported for existing callers)
    MAX_UNLISTED_SHARE,
    StockRefresh,
    mark_unavailable,
)
from app.ocado.session import OcadoSession, get_shared_session
from app.scraper.products.base import ProductStatus
from app.scraper.products.ocado import product_status as _status

log = logging.getLogger(__name__)

PRODUCTS_PATH = "/api/webproductpagews/v6/products"
RETAILER = "ocado"

#: Ocado's own web client decorates fifty products per call.
BATCH_SIZE = 50

#: A week of recipes maps to a few hundred candidate packs; this is the ceiling
#: on one refresh so a pathological basket cannot turn into a hundred requests.
MAX_SKUS = 750

_PRODUCT_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


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
    response = None
    try:
        response = session.request(
            "PUT", PRODUCTS_PATH, json=batch, reauthenticate=False
        )
        response.raise_for_status()
    except Exception:  # noqa: BLE001 - the id that caused it is what matters
        # Only Ocado's known "one retired id poisoned this batch" 500 is safe to
        # bisect. Authentication failures and network outages affect every id;
        # splitting those would turn one failed price check into minutes of
        # identical retries while the UI remains stuck on "Checking Ocado".
        status = getattr(response, "status_code", getattr(response, "status", None))
        if status != 500:
            raise
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
    """Refresh ``skus`` against the live site and write the answer to the catalogue.

    The generic refresh reaches Ocado through this module anyway; what this adds
    is ``session``, so a caller already holding one account's session refreshes
    with it rather than with the shared one.
    """
    return catalogue.refresh_stock(
        factory,
        skus,
        retailer=retailer,
        fetch=lambda ids: fetch_statuses(ids, session=session),
    )


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
