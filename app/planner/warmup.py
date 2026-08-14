"""Build each shop's planner snapshot before a request needs it.

The snapshot is what every priced read is answered from — the browse list, the
best-fit ranking, the basket — and it is built from the catalogue on first use,
which takes roughly three seconds for Ocado's 8k products. Built lazily, that
cost lands inside whichever request happens to arrive first after the process
starts, and browse is usually it: the page opens, the recipe list holds a
skeleton grid, and the delay looks like a slow query rather than a one-off.

So it is built here instead, on a background thread at start-up, for every
catalogued retailer rather than only the one the user currently shops at.
Switching shop in settings re-prices everything, and paying three seconds for
that switch is the same bad first request in a different place.

Nothing waits on this. A request that arrives mid-warm builds the snapshot it
needs itself and both are reconciled under the cache's lock, so the worst case
is the behaviour we had before.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from app import retailers
from app.planner.cache import get_index

log = logging.getLogger(__name__)

_THREAD: threading.Thread | None = None


def warm(factory: sessionmaker[Session]) -> None:
    """Load the snapshot for every catalogued shop, one at a time.

    Sequential on purpose: the retailers share nothing but the database file,
    and running them together would put two full catalogue loads in memory at
    once to save time nobody is waiting on.
    """
    for retailer in retailers.RETAILER_IDS:
        if not retailers.get(retailer).catalogued:
            continue
        started = time.monotonic()
        try:
            index = get_index(factory, retailer=retailer)
        except Exception:  # noqa: BLE001 - a cold cache is not a failed start-up
            log.warning("planner warm-up failed for %s", retailer, exc_info=True)
            continue
        log.info(
            "planner warm-up: %s ready in %.1fs (%d recipes, %d ingredients)",
            retailer,
            time.monotonic() - started,
            len(index.recipes),
            len(index.ingredients),
        )


def start(get_factory: Callable[[], sessionmaker[Session]]) -> threading.Thread | None:
    """Warm the snapshots on a daemon thread, unless this process should not.

    Takes a callable rather than a factory so the database is opened on the
    thread: resolving it here would put the engine's first connection — and the
    schema check behind it — in the start-up path, which is the opposite of the
    point.

    Skipped under pytest: the API tests point the app at a temporary database
    through dependency overrides, which this thread has no way of seeing, so
    warming would quietly read the developer's real library instead — several
    seconds of it, in the background, during every test that starts the app.
    """
    global _THREAD
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    if not _env_flag("HOLAFRESCA_PLANNER_WARM", True):
        log.info("planner warm-up: disabled (HOLAFRESCA_PLANNER_WARM=0)")
        return None
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD
    _THREAD = threading.Thread(
        target=lambda: warm(get_factory()), name="planner-warmup", daemon=True
    )
    _THREAD.start()
    return _THREAD


def stop(timeout: float = 5.0) -> None:
    """Wait for an in-flight warm-up, so shutdown does not race the cache.

    There is nothing to cancel — a catalogue load is one long query and a lot of
    object building — so this only joins. The thread is a daemon, so a warm-up
    that outlives the timeout cannot keep the process alive.
    """
    global _THREAD
    thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    _THREAD = None


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}
