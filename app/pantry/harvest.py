"""Turn a pushed basket into the lots its shop leaves behind.

The bridge between the planner's world and the pantry's: a
:class:`app.planner.basket.Basket` knows packs, covers and demands, and a
:class:`app.pantry.model.Lot` knows what sits on a shelf. Nothing here touches
the database — :mod:`app.pantry.store` does the writing — so the translation
can be tested against a basket built in memory.

A lot's ``available`` is the cupboard *after* the shop: whatever the build drew
from earlier lots, plus the capacity actually bought. The week's recipe demands
ride along as contributions, to be subtracted as recipes are cooked — which is
how the leftover the waste model priced becomes, without being computed again,
what the next shop need not buy.
"""
from __future__ import annotations

from app.pantry import model
from app.pantry.model import Lot, Quantity
from app.planner.basket import Basket, BasketLine


def _available(line: BasketLine, carried: Quantity | None) -> Quantity:
    bought_g = line.cover.capacity_g if line.cover else 0.0
    bought_qty = line.cover.capacity_qty if line.cover else None
    if line.unit_kind == "count":
        return Quantity(
            grams=(carried.grams if carried else 0.0) + bought_g,
            units=((carried.units or 0.0) if carried else 0.0) + (bought_qty or 0.0),
        )
    return Quantity(grams=(carried.grams if carried else 0.0) + bought_g)


def _salvage(line: BasketLine, prior: dict[str, float]) -> float | None:
    """What was bought decides; food already on the shelf keeps its old figure."""
    if line.cover is not None:
        return line.cover.salvage
    return prior.get(line.key)


def lots_from_basket(
    basket: Basket,
    *,
    held: dict[str, Quantity],
    prior_salvage: dict[str, float],
    owned_item_keys: frozenset[str] | set[str] = frozenset(),
) -> list[Lot]:
    """The lots a push of ``basket`` should deposit.

    ``held`` is the same cupboard read the basket was built against, and
    ``prior_salvage`` its per-key salvage — see
    :func:`app.pantry.store.live_salvages`.

    Skipped, deliberately:

    * **Owned lines.** "I already have it" says nothing about how much, and a
      quantity the model cannot state is one it must not carry.
    * **Lines below the salvage threshold** — the chiller and the bakery, which
      would drift faster than they would save. :func:`app.pantry.model.admits`
      is the single gate.
    * **Lines that bought nothing and carried nothing**, which have no shelf to
      describe.
    """
    lots: list[Lot] = []
    for line in basket.lines:
        if line.key in owned_item_keys:
            continue
        carried = held.get(line.key)
        if line.cover is None and carried is None:
            continue
        salvage = _salvage(line, prior_salvage)
        if salvage is None or not model.admits(salvage):
            continue
        available = _available(line, carried)
        if not available:
            continue
        lots.append(
            Lot(
                ingredient_key=line.key,
                ingredient_name=line.name,
                week_start="",  # assigned by the deposit, which knows the week
                available=available,
                salvage=salvage,
                contributions={
                    c.recipe_id: Quantity(grams=c.grams, units=c.quantity)
                    for c in line.contributions
                },
                unit_kind=line.unit_kind,
            )
        )
    return lots
