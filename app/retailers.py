"""Which shops the app knows about, and what each one can do.

The database has always been able to hold more than one retailer — ``Product``,
``ProductSearchHit``, ``IngredientMapping`` and ``UserPackPreference`` are all
keyed by ``retailer`` — but every module that read those tables pinned the value
to a module-level ``RETAILER = "ocado"``. This module is the single place that
list now lives, so adding a shop is a row here plus an adapter, not a search for
constants.

Two properties are worth telling apart, because they are the whole reason a
second retailer is cheap:

``catalogued``
    Products can be scraped, mapped to ingredients and priced. This is what the
    planner and the basket need, and it asks nothing of the retailer beyond a
    public search endpoint.

``shoppable``
    A basket can be pushed into the retailer's own cart, which needs a login, a
    session, a cart API and a ledger to tell our items from yours — the whole of
    :mod:`app.ocado`. Ocado has that; Sainsbury's does not yet, and a basket
    priced there is a shopping list you take to the shop yourself.

Nothing branches on the *name* of a retailer; code asks for the capability. A
page that offers "push to cart" checks :attr:`Retailer.shoppable`, so Sainsbury's
arriving without it degrades to a list rather than to a broken button.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The pseudo-retailer for products sourced by hand — see :mod:`app.mapping.manual`.
#: It is a value ``Product.retailer`` takes, but never a shop you can select, so
#: it is deliberately absent from :data:`RETAILERS`.
MANUAL_RETAILER = "manual"


@dataclass(frozen=True, slots=True)
class Retailer:
    id: str
    label: str
    #: Products can be scraped, mapped and priced.
    catalogued: bool = True
    #: A basket can be pushed to the retailer's own cart. See the module docstring.
    shoppable: bool = False


RETAILERS: tuple[Retailer, ...] = (
    Retailer(id="ocado", label="Ocado", catalogued=True, shoppable=True),
    Retailer(id="sainsburys", label="Sainsbury's", catalogued=True, shoppable=True),
)

#: The shop a user who has never chosen one gets, and the value every existing
#: row already carries — so the migration that adds the preference has nothing to
#: backfill.
DEFAULT_RETAILER = RETAILERS[0].id

_BY_ID = {retailer.id: retailer for retailer in RETAILERS}

RETAILER_IDS: tuple[str, ...] = tuple(_BY_ID)


def get(retailer_id: str | None) -> Retailer:
    """The retailer with this id, falling back to the default for ``None``.

    Raises ``KeyError`` for an id that is not a shop, which includes
    :data:`MANUAL_RETAILER` — asking to *shop at* 'manual' is a bug, not a
    configuration.
    """
    return _BY_ID[retailer_id or DEFAULT_RETAILER]


def is_known(retailer_id: str | None) -> bool:
    return retailer_id in _BY_ID


def label(retailer_id: str | None) -> str:
    """Display name, tolerant of ids that are not shops.

    Unlike :func:`get`, this never raises: it is used to caption rows that may
    carry ``'manual'`` or a retailer retired since they were written, and a
    shopping list is not the place to discover a stale value.
    """
    known = _BY_ID.get(retailer_id or DEFAULT_RETAILER)
    if known is not None:
        return known.label
    if retailer_id == MANUAL_RETAILER:
        return "Bought by hand"
    return str(retailer_id)


def resolve(retailer_id: str | None) -> str:
    """Normalise a requested retailer id, falling back to the default.

    Used at the edges — a query parameter, a stored preference written before a
    retailer was removed — where an unknown value should quietly mean "the usual
    shop" rather than fail the request.
    """
    return retailer_id if is_known(retailer_id) else DEFAULT_RETAILER
