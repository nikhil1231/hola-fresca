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


def browser_client(retailer: str, *, headless: bool = False):
    """A ready-to-enter browser client for this retailer's product API.

    Every adapter exports its client as ``BrowserClient`` as well as under its
    own name, so this needs no per-retailer branch.
    """
    return get_adapter(retailer).BrowserClient(headless=headless)
