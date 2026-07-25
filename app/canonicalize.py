"""Ingredient unit canonicalisation: resolve amounts to grams/millilitres.

The source omits units on ~56% of ingredient lines and never ships gram
weights. Two steps recover usable quantities:

1. ``backfill_units`` — for lines with an empty unit, adopt the *modal* unit that
   the same ingredient carries elsewhere in the corpus (e.g. Lentils is always
   ``carton(s)``, Spinach always ``grams``), matching first on ingredient id and
   then on name; failing both, infer from the amount's magnitude. The amount was
   already correct; only the unit word was missing.
2. ``to_grams`` — convert (name, amount, unit) to a metric amount: metric units
   pass through; tsp/tbsp/pinch use standard conversions; count/container units
   (unit(s)/carton(s)/…) use a hand-authored gram reference
   (``app/data/ingredient_grams.json``) keyed by ingredient, then by unit.

This is a first pass of ingredient canonicalisation, not the full model.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import RecipeIngredient

_REFERENCE_PATH = Path(__file__).parent / "data" / "ingredient_grams.json"

_METRIC = {"grams": "g", "milliliter(s)": "ml"}
# Standard kitchen conversions (approx; density ~1 so treated as g/ml).
_SPOON = {"tbsp": 15.0, "tsp": 5.0, "pinch": 0.5}

# Amount at or above this reads as a gram weight rather than a count; see
# backfill_units tier 3. The observed corpus gap is wide: counts top out around
# 6 (tomatoes, buns) and gram weights start around 10 (spices, nuts, cheese).
_GRAMS_THRESHOLD = 10.0
_COUNT_UNIT = "unit(s)"
# No dish calls for this many of a countable thing (the corpus tops out at 10
# gyozas). Above it the unit word is wrong and the amount is really grams —
# "Lamb Shank, 670 unit(s)" is 670g, not 100kg of shank.
_COUNT_MAX = 50.0


@lru_cache(maxsize=1)
def _reference() -> dict:
    data = json.loads(_REFERENCE_PATH.read_text())
    return {
        "by_name": {k.lower(): float(v) for k, v in data["by_name"].items()},
        "by_keyword": [(k.lower(), float(v)) for k, v in data["by_keyword"]],
        "by_unit": {k: float(v) for k, v in data["by_unit"].items()},
    }


def _normalize(name: str) -> str:
    ascii_name = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    return re.sub(r"\s+", " ", ascii_name).strip()


def _grams_per_unit(name: str, unit: str) -> float | None:
    ref = _reference()
    norm = _normalize(name)
    if norm in ref["by_name"]:
        return ref["by_name"][norm]
    for keyword, grams in ref["by_keyword"]:
        if keyword in norm:
            return grams
    return ref["by_unit"].get(unit)


def to_grams(name: str, amount: float | None, unit: str | None) -> tuple[float | None, str | None]:
    """Return (canonical_amount, 'g'|'ml') or (None, None) if unresolved."""
    if amount is None or not unit:
        return None, None
    if unit in _METRIC:
        return amount, _METRIC[unit]
    if unit in _SPOON:
        return round(amount * _SPOON[unit], 1), "g"
    if amount >= _COUNT_MAX:
        return amount, "g"
    per_unit = _grams_per_unit(name, unit)
    if per_unit is not None:
        return round(amount * per_unit, 1), "g"
    return None, None


def _modal_units(pairs) -> dict[str, str]:
    counts: dict[str, Counter] = defaultdict(Counter)
    for key, unit in pairs:
        if key and unit:
            counts[key][unit] += 1
    return {key: c.most_common(1)[0][0] for key, c in counts.items()}


def backfill_units(session: Session) -> dict[str, int]:
    """Fill empty ingredient units from the corpus, in descending confidence.

    Returns rows updated per tier. Operates on the whole corpus (not just
    curated) so each modal unit is as well-supported as possible.

    1. ``by_id`` — modal unit of the same source ingredient id.
    2. ``by_name`` — modal unit of the same ingredient *name*. Ingredient ids are
       versioned per recipe, so an ingredient frequently carries a unit only
       under a sibling id (Smoked Paprika is ``sachet(s)`` under one id and
       unitless under another).
    3. ``by_magnitude`` — for names that carry no unit anywhere, read the amount:
       at or above ``_GRAMS_THRESHOLD`` it is a gram weight, below it a count or
       container. No recipe calls for 15 peppers, and none for 1g of chicken.
    """
    rows = session.execute(
        select(
            RecipeIngredient.source_ingredient_id,
            RecipeIngredient.name,
            RecipeIngredient.unit,
        )
    ).all()
    by_id = _modal_units((iid, unit) for iid, _, unit in rows)
    by_name = _modal_units((_normalize(name), unit) for _, name, unit in rows if name)

    filled = {"by_id": 0, "by_name": 0, "by_magnitude": 0}
    empties = session.scalars(
        select(RecipeIngredient).where(
            (RecipeIngredient.unit.is_(None)) | (RecipeIngredient.unit == ""),
        )
    )
    for ing in empties:
        unit, tier = None, ""
        if ing.source_ingredient_id:
            unit, tier = by_id.get(ing.source_ingredient_id), "by_id"
        if unit is None:
            unit, tier = by_name.get(_normalize(ing.name)), "by_name"
        if unit is None and ing.amount is not None:
            unit = "grams" if ing.amount >= _GRAMS_THRESHOLD else _COUNT_UNIT
            tier = "by_magnitude"
        if unit:
            ing.unit = unit
            filled[tier] += 1
    session.flush()
    return filled
