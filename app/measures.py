"""Cooking-facing quantities, as opposed to shopping-facing ones.

:mod:`app.canonicalize` answers the shopping question — how many grams, so the
planner can compare a week's demand against pack sizes that are sold by weight.
This module answers the cooking one: what does the person at the hob actually
measure out?

They are not the same question, and grams are the wrong answer to the second.
HelloFresh publishes no weight for a sachet or a pot; the source carries only
``1 sachet``, and the gram figure downstream is our own estimate. With a meal kit
that estimate never matters, because "add the sachet" is self-executing. Buying
the same recipe from a supermarket it matters a great deal: the cook is holding a
40 g jar and has to translate. Spoons are what they can act on, and since these
containers are portion-filled by volume rather than weighed, unit→volume holds
far steadier across spices than unit→mass, which needs a density per spice.

So the native unit stays the canonical cooking quantity and spoons are offered as
the actionable translation. Both come from ``ingredient_spice_doses.json`` via
:func:`app.canonicalize.spice_dose`: one container mass, from which teaspoons are
derived. Keeping the two in one record is deliberate — they were once separate
tables and drifted, leaving the page advising half a teaspoon of ground cloves
while the planner bought eight grams of it.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.canonicalize import normalize_name, spice_dose

_POTENCY_PATH = Path(__file__).parent / "data" / "ingredient_potency.json"

# Units that already state a metric quantity, so nothing needs deriving and the
# figure shown to the cook is the source's own.
METRIC_UNITS = frozenset({"grams", "milliliter(s)"})
# Units that are already a spoon measure — no translation to do.
SPOON_UNITS = frozenset({"tsp", "tbsp", "pinch"})

POTENCY_HIGH = "high"
POTENCY_NORMAL = "normal"
POTENCY_FORGIVING = "forgiving"


@lru_cache(maxsize=1)
def _potency() -> dict:
    data = json.loads(_POTENCY_PATH.read_text())
    return {
        level: {
            "by_name": {n.lower() for n in data[level]["by_name"]},
            "by_keyword": [k.lower() for k in data[level]["by_keyword"]],
        }
        for level in (POTENCY_HIGH, POTENCY_FORGIVING)
    }


def amount_g_is_estimated(unit: str | None) -> bool:
    """True when the gram figure was derived by us rather than stated by the source.

    Everything a recipe expresses as a count or a container — the majority of
    lines — is converted to grams through a reference table, so the resulting
    weight is an estimate. Callers use this to decide whether a gram figure has
    earned the right to be the headline quantity.
    """
    return (unit or "") not in METRIC_UNITS


def _tsp_per_container(name: str, unit: str | None) -> tuple[float, dict] | None:
    """Teaspoons in one container, derived from its mass — never stored separately."""
    dose = spice_dose(name, unit)
    if dose is None:
        return None
    per_tsp = float(dose.get("g_per_tsp") or 0)
    if per_tsp <= 0:
        return None
    return float(dose["grams"]) / per_tsp, dose


def spoons_for(name: str, amount: float | None, unit: str | None) -> float | None:
    """Teaspoons for a pre-portioned container of ``name``, or None.

    Only container units get a translation. A metric line already states what to
    measure, a tsp/tbsp line is already a spoon, and a countable thing (a bunch,
    a tin, an onion) is not something anyone spoons out.
    """
    if amount is None or amount <= 0 or not unit:
        return None
    if unit in METRIC_UNITS or unit in SPOON_UNITS:
        return None
    resolved = _tsp_per_container(name, unit)
    if resolved is None:
        return None
    return round(amount * resolved[0], 2)


def spoon_range_for(
    name: str, amount: float | None, unit: str | None
) -> tuple[float, float] | None:
    """The teaspoon span a sensible cook would stay within, scaled to ``amount``.

    Shown next to potent seasonings, where our container mass is an estimate and
    the difference between the low and high end is the difference between an
    under-seasoned dish and an inedible one. Also a guardrail: a mass that implies
    a spoonful outside this span is wrong by construction.
    """
    if amount is None or amount <= 0 or not unit:
        return None
    if unit in METRIC_UNITS or unit in SPOON_UNITS:
        return None
    resolved = _tsp_per_container(name, unit)
    if resolved is None:
        return None
    _, dose = resolved
    lo, hi = dose.get("tsp_min"), dose.get("tsp_max")
    if lo is None or hi is None:
        return None
    return round(amount * float(lo), 2), round(amount * float(hi), 2)


def potency_for(name: str) -> str:
    """How badly a wrong quantity of ``name`` would hurt the dish."""
    norm = normalize_name(name)
    ref = _potency()
    for level in (POTENCY_HIGH, POTENCY_FORGIVING):
        table = ref[level]
        if norm in table["by_name"]:
            return level
        if any(keyword in norm for keyword in table["by_keyword"]):
            return level
    return POTENCY_NORMAL
