"""The adapter boundary.

A :class:`RecipeSource` has three responsibilities, deliberately split so each
can be tested and re-run independently:

* :meth:`discover` — enumerate the recipe URLs the source offers.
* :meth:`extract` — pull the source-native payload out of a fetched page. This
  is pure (no network) so it can be unit-tested against saved fixtures.
* :meth:`normalize` — map a raw payload to the source-agnostic
  :class:`~app.scraper.models.NormalizedRecipe`.

The HTTP fetching itself is generic and lives in the pipeline, not the adapter;
adapters only need to know how to *read* a page's bytes and how to find and
shape the data inside. ``source_id_from_url`` lets the pipeline key state and
raw files before a page is ever fetched.
"""
from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable

from app.scraper.models import NormalizedRecipe


@runtime_checkable
class RecipeSource(Protocol):
    name: str
    #: Base URL used to resolve relative image paths, if any.
    image_base: str

    def discover(self, http_get: "HttpGet") -> Iterable[tuple[str, str]]:
        """Yield ``(source_id, url)`` pairs for every recipe in the source."""

    def extract(self, page_bytes: bytes, url: str) -> Any:
        """Extract the source-native recipe payload from a fetched page."""

    def normalize(self, payload: Any, url: str) -> NormalizedRecipe:
        """Convert a raw payload into the source-agnostic IR."""


class HttpGet(Protocol):
    """Minimal synchronous GET used by :meth:`RecipeSource.discover`."""

    def __call__(self, url: str) -> bytes: ...
