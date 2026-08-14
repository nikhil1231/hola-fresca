"""On-demand retailer search for the mapping review UI.

The batch scrape covers each ingredient with one search term taken from the
recipe library ("Vegetable Stock Paste"). That term is not always the one that
finds the right products — searching "vegetable stock" instead surfaces stock
cubes, which are a fine (and cheaper) stand-in. This module lets the reviewer
re-search with their own wording and pick from the results by hand, with no LLM
involved.

One runner per retailer, each owning its own browser and profile: the profiles
are what the sites trust, and sharing one context between two shops would throw
away that trust on every switch.

Results are persisted exactly like the batch scrape (raw cache + ``products`` +
``product_search_hits``), so a re-search permanently enriches the candidate pool
and everything downstream keeps working off product ids.

Playwright's sync API is thread-affine, so the browser lives in one dedicated
worker thread and searches are dispatched to it. The browser is started lazily
on the first search and closed after an idle period.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import IngredientMapping, Product, ProductSearchHit
from app.retailers import DEFAULT_RETAILER
from app.scraper.products import storage
from app.scraper.products.pipeline import upsert_product
from app.scraper.products.registry import get_adapter
from app.scraper.ratelimit import AdaptiveThrottle

log = logging.getLogger("holafresca.mapping")

#: Close the browser after this many seconds with no searches.
IDLE_TIMEOUT_S = 600.0
#: Give a single search this long before giving up.
SEARCH_TIMEOUT_S = 90.0


def browser_headless(retailer: str) -> bool:
    """Whether this shop's browser calls can be made without a visible window.

    The shop decides, not us: Sainsbury's Akamai profile refuses a headless
    Chrome outright (see ``sainsburys.BROWSER_HEADLESS``), and a live search or
    price refresh there fails with "Access Denied" until it is run headed. The
    environment gets the last word, because a host with no display cannot open a
    window whatever the shop would prefer — there the honest failure is the
    request erroring rather than a browser that never appears.
    """
    override = os.environ.get("HOLAFRESCA_LIVE_BROWSER_HEADLESS")
    if override is not None and override.strip():
        return override.strip().lower() not in {"0", "false", "no"}
    return bool(getattr(get_adapter(retailer), "BROWSER_HEADLESS", True))


@dataclass
class _Job:
    """One call to make on the browser thread. ``call`` takes the client."""

    call: Callable[[Any], Any]
    future: Future


class LiveSearchRunner:
    """Serialises live retailer calls onto one long-lived headless browser session.

    Named for its first caller, but it is now the shared browser worker for
    anything the app has to ask a shop in the moment: the review page's
    re-searches, and the basket page's stock and price refresh at a retailer
    whose API will not answer an HTTP client. :meth:`run` is the general form and
    :meth:`search` the shorthand.
    """

    def __init__(
        self,
        *,
        retailer: str = DEFAULT_RETAILER,
        headless: bool | None = None,
        idle_timeout: float = IDLE_TIMEOUT_S,
    ):
        self.retailer = retailer
        self.headless = browser_headless(retailer) if headless is None else headless
        self.idle_timeout = idle_timeout
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._throttle = AdaptiveThrottle(workers=1, delay=0.0, max_delay=20.0)

    @property
    def throttle(self) -> AdaptiveThrottle:
        """The backoff this retailer's calls share, so one caller slows them all."""
        return self._throttle

    def run(self, call: Callable[[Any], Any], timeout: float = SEARCH_TIMEOUT_S):
        """Run ``call(client)`` on the browser thread and return its result.

        Playwright's sync API is thread-affine, so every call has to arrive here
        rather than touch the client directly.
        """
        job = _Job(call=call, future=Future())
        with self._lock:
            self._ensure_thread()
            self._queue.put(job)
        return job.future.result(timeout=timeout)

    def search(self, term: str, timeout: float = SEARCH_TIMEOUT_S) -> dict:
        return self.run(lambda client: client.search(term, self._throttle), timeout=timeout)

    # -- worker -----------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name=f"{self.retailer}-search", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        client = None
        try:
            while True:
                try:
                    job = self._queue.get(timeout=self.idle_timeout)
                except queue.Empty:
                    return  # idle: close the browser and let the thread exit
                try:
                    if client is None:
                        log.info(
                            "starting %s browser session (headless=%s)",
                            self.retailer,
                            self.headless,
                        )
                        client = get_adapter(self.retailer).BrowserClient(
                            headless=self.headless
                        )
                        client.__enter__()
                    job.future.set_result(job.call(client))
                except Exception as exc:  # noqa: BLE001 - surface to the caller
                    job.future.set_exception(exc)
                    # A failed search may mean a dead browser; drop it so the
                    # next search starts a fresh session.
                    if client is not None:
                        try:
                            client.__exit__(None, None, None)
                        except Exception:  # noqa: BLE001
                            pass
                        client = None
        finally:
            if client is not None:
                try:
                    client.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass


_runners: dict[str, LiveSearchRunner] = {}


def get_runner(retailer: str = DEFAULT_RETAILER) -> LiveSearchRunner:
    """The process-wide search runner for one retailer, started on first use."""
    runner = _runners.get(retailer)
    if runner is None:
        runner = LiveSearchRunner(retailer=retailer)
        _runners[retailer] = runner
    return runner


def search_and_store(
    session: Session,
    ingredient_key: str,
    term: str,
    *,
    runner: LiveSearchRunner | None = None,
    term_rank: int | None = None,
    line_count: int | None = None,
    retailer: str = DEFAULT_RETAILER,
) -> int:
    """Search ``retailer`` for ``term`` and merge the results into ``ingredient_key``.

    Existing candidates are kept — a re-search widens the pool rather than
    replacing it, so an earlier good match is never lost. Returns the number of
    candidates added or refreshed.
    """
    term = term.strip()
    if not term:
        raise ValueError("search term must not be empty")

    runner = runner or get_runner(retailer)
    # The runner is the authority on which shop this is: a caller that passed one
    # in (the tests, and any future per-account runner) must not have the
    # retailer argument silently disagree with it.
    retailer = getattr(runner, "retailer", retailer)
    adapter = get_adapter(retailer)

    payload = runner.search(term)
    product_ids = adapter.extract_product_ids(payload)
    objects = adapter.extract_product_objects(payload)

    storage.write_raw(
        retailer,
        "search",
        f"live:{ingredient_key}:{term}",
        {
            "search_term": term,
            "ingredient_key": ingredient_key,
            "product_ids": product_ids,
            "response": payload,
        },
    )

    # Carry the ingredient's frequency metadata onto new hits so the review
    # list keeps sorting correctly.
    existing = session.scalar(
        select(ProductSearchHit).where(
            ProductSearchHit.retailer == retailer,
            ProductSearchHit.ingredient_key == ingredient_key,
        )
    )
    # Explicit values win (a brand-new ingredient has no prior hit to inherit
    # its frequency metadata from, and without it the review list mis-sorts).
    term_rank = term_rank if term_rank is not None else (existing.term_rank if existing else 0)
    line_count = line_count if line_count is not None else (existing.line_count if existing else 0)

    ordered = [o for o in objects if not product_ids or _sku_of(o) in set(product_ids)] or objects
    added = 0
    for rank, obj in enumerate(ordered, start=1):
        try:
            normalized = adapter.normalize_product(obj)
        except ValueError:
            continue
        storage.write_raw(
            retailer,
            "product",
            normalized.sku,
            {"sku": normalized.sku, "response": obj, "source": "live-search"},
        )
        product = upsert_product(session, normalized)

        hit = session.scalar(
            select(ProductSearchHit).where(
                ProductSearchHit.retailer == retailer,
                ProductSearchHit.ingredient_key == ingredient_key,
                ProductSearchHit.sku == normalized.sku,
            )
        )
        if hit is None:
            session.add(
                ProductSearchHit(
                    product_id=product.id,
                    retailer=retailer,
                    ingredient_key=ingredient_key,
                    search_term=term,
                    term_rank=term_rank,
                    line_count=line_count,
                    sku=normalized.sku,
                    result_rank=rank,
                )
            )
        else:
            hit.product_id = product.id
            hit.search_term = term
            hit.result_rank = rank
        added += 1

    # Remember the term the reviewer last used for this ingredient.
    mapping = session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.ingredient_key == ingredient_key,
        )
    )
    if mapping is not None:
        mapping.search_term = term
        mapping.updated_at = datetime.now(timezone.utc)
    session.commit()
    log.info(
        "live %s search %r for %s -> %d candidates", retailer, term, ingredient_key, added
    )
    return added


def _sku_of(obj: dict) -> str | None:
    for key in ("sku", "productId", "id", "uuid"):
        value = obj.get(key)
        if isinstance(value, str):
            return value.lower()
    return None
