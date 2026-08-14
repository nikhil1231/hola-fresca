"""Which module knows how to scrape which retailer.

An adapter is a module, not a class — see :mod:`app.scraper.products.base` for
why. This resolves a retailer id to one, so the pipeline and the live-search
runner are written once against the interface rather than once per shop.

Kept apart from :mod:`app.retailers` on purpose. That module is the domain
question ("which shops can this app price a basket at, and which can it push a
cart to") and is read by the API and the planner; this one is the scraper's
implementation table. A retailer could in principle be listed there while its
adapter is still being written, and the failure should be a clear KeyError here
rather than a silent absence from the shop list.
"""
from __future__ import annotations

from types import ModuleType

from app.scraper.products import ocado, sainsburys

_ADAPTERS: dict[str, ModuleType] = {
    ocado.RETAILER: ocado,
    sainsburys.RETAILER: sainsburys,
}

ADAPTER_IDS: tuple[str, ...] = tuple(_ADAPTERS)


def get_adapter(retailer: str) -> ModuleType:
    try:
        return _ADAPTERS[retailer]
    except KeyError:
        known = ", ".join(sorted(_ADAPTERS))
        raise KeyError(f"no product adapter for retailer {retailer!r}; known: {known}") from None


def has_adapter(retailer: str) -> bool:
    return retailer in _ADAPTERS


def client(retailer: str, *, headless: bool = True):
    """A ready-to-enter client for this retailer's product API.

    Every adapter exports its client as ``Client`` as well as under its own
    name, and the two transports present the same surface — a context manager
    with ``search`` and ``products`` — so callers neither know nor care which
    one they were handed.

    ``headless`` reaches only the shops that actually drive a browser, which is
    what ``USES_BROWSER`` records. A browser-free adapter is not asked to accept
    an argument about a window it will never open.
    """
    adapter = get_adapter(retailer)
    if getattr(adapter, "USES_BROWSER", False):
        return adapter.Client(headless=headless)
    return adapter.Client()
