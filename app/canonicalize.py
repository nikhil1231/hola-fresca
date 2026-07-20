"""Ingredient unit canonicalisation: resolve amounts to grams/millilitres.

The source omits units on ~56% of ingredient lines and never ships gram
weights. Two steps recover usable quantities:

1. ``backfill_units`` — for lines with an empty unit, adopt the *modal* unit that
   the same ingredient id carries elsewhere in the corpus (e.g. Lentils is always
   ``carton(s)``, Spinach always ``grams``). The amount was already correct; only
   the unit word was missing.
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
    per_unit = _grams_per_unit(name, unit)
    if per_unit is not None:
        return round(amount * per_unit, 1), "g"
    return None, None


def backfill_units(session: Session) -> int:
    """Fill empty ingredient units from the modal unit of the same ingredient id.

    Returns the number of rows updated. Operates on the whole corpus (not just
    curated) so the modal unit is as well-supported as possible.
    """
    rows = session.execute(
        select(RecipeIngredient.source_ingredient_id, RecipeIngredient.unit)
    ).all()
    counts: dict[str, Counter] = defaultdict(Counter)
    for iid, unit in rows:
        if iid and unit:
            counts[iid][unit] += 1
    modal = {iid: c.most_common(1)[0][0] for iid, c in counts.items()}

    updated = 0
    empties = session.scalars(
        select(RecipeIngredient).where(
            RecipeIngredient.source_ingredient_id.is_not(None),
            (RecipeIngredient.unit.is_(None)) | (RecipeIngredient.unit == ""),
        )
    )
    for ing in empties:
        unit = modal.get(ing.source_ingredient_id)
        if unit:
            ing.unit = unit
            updated += 1
    session.flush()
    return updated
