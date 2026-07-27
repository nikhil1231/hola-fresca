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
# Units that state a weight or volume outright. A tiny amount under one of these
# is only believable for things really used by the gram; see _contradicts_magnitude.
_MASS_UNITS = frozenset(_METRIC)

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


def normalize_name(name: str) -> str:
    ascii_name = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    return re.sub(r"\s+", " ", ascii_name).strip()


def _grams_per_unit(name: str, unit: str) -> float | None:
    ref = _reference()
    norm = normalize_name(name)
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


def _contradicts_magnitude(unit: str | None, amount: float | None) -> bool:
    """True when a weight/volume unit is implausible for how small the amount is.

    "2 grams" of a thing sold in nests is not two grams of it.
    """
    return bool(
        unit in _MASS_UNITS and amount is not None and 0 < amount < _GRAMS_THRESHOLD
    )


# How far a re-read amount may stray from the ingredient's typical weight before
# it is rejected. Wide, because one ingredient legitimately varies with serving
# size, but narrow enough to catch "1 balsamic vinegar" becoming a 250 ml bottle.
_PLAUSIBLE_LOW, _PLAUSIBLE_HIGH = 0.25, 3.0


def _countable_units_by_name(rows) -> dict[str, list[str]]:
    """Every countable unit each name is attested with, most common first."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for name, unit, _ in rows:
        if name and unit and unit not in _MASS_UNITS:
            counts[normalize_name(name)][unit] += 1
    return {key: [u for u, _ in c.most_common()] for key, c in counts.items()}


def _typical_grams_by_name(rows) -> dict[str, float]:
    """Median weight each name is given when the source states one outright.

    This is the yardstick for re-reading a unit: whatever "2" means for Egg
    Noodle Nest, the answer should land near the 187 g the corpus reports when it
    bothers to say grams.
    """
    amounts: dict[str, list[float]] = defaultdict(list)
    for name, unit, amount in rows:
        if name and unit in _MASS_UNITS and amount is not None and amount >= _GRAMS_THRESHOLD:
            amounts[normalize_name(name)].append(amount)
    return {key: sorted(v)[len(v) // 2] for key, v in amounts.items()}


def _reread_unit(
    name: str,
    amount: float | None,
    candidates: list[str],
    typical_g: float | None,
) -> str | None:
    """Pick the countable unit that best explains ``amount`` for ``name``.

    Returns None unless some candidate converts to a weight near what the corpus
    says this ingredient usually weighs — so an ingredient with no trustworthy
    reference, or whose only countable unit is a whole bottle, is left alone.
    """
    if amount is None or typical_g is None:
        return None
    low, high = typical_g * _PLAUSIBLE_LOW, typical_g * _PLAUSIBLE_HIGH
    best, best_gap = None, None
    for unit in candidates:
        grams, _ = to_grams(name, amount, unit)
        if grams is None or not low <= grams <= high:
            continue
        gap = abs(grams - typical_g)
        if best_gap is None or gap < best_gap:
            best, best_gap = unit, gap
    return best


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

    A modal unit that the amount's own magnitude contradicts is then overruled by
    ``count_veto``: the source mixes units for one ingredient, so the mode can be
    ``grams`` while this particular line is a count. See
    :func:`repair_contradicted_units` for the reasoning and the guard.
    """
    rows = session.execute(
        select(
            RecipeIngredient.source_ingredient_id,
            RecipeIngredient.name,
            RecipeIngredient.unit,
            RecipeIngredient.amount,
        )
    ).all()
    by_id = _modal_units((iid, unit) for iid, _, unit, _ in rows)
    by_name = _modal_units((normalize_name(name), unit) for _, name, unit, _ in rows if name)
    name_rows = [(name, unit, amount) for _, name, unit, amount in rows]
    countable_by_name = _countable_units_by_name(name_rows)
    typical_by_name = _typical_grams_by_name(name_rows)

    filled = {"by_id": 0, "by_name": 0, "by_magnitude": 0, "count_veto": 0}
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
            unit, tier = by_name.get(normalize_name(ing.name)), "by_name"
        if unit is None and ing.amount is not None:
            unit = "grams" if ing.amount >= _GRAMS_THRESHOLD else _COUNT_UNIT
            tier = "by_magnitude"
        if _contradicts_magnitude(unit, ing.amount):
            key = normalize_name(ing.name)
            reread = _reread_unit(
                ing.name, ing.amount, countable_by_name.get(key, []), typical_by_name.get(key)
            )
            if reread:
                unit, tier = reread, "count_veto"
        if unit:
            ing.unit = unit
            filled[tier] += 1
    session.flush()
    return filled


# A trace weight is only re-read when the corpus states a real weight for that
# ingredient this many times, so the median stands on actual evidence.
_MIN_GRAM_EVIDENCE = 20
# The largest multiple of a portion a trace amount is allowed to mean. "3" reads
# as three portions; beyond that the number is noise, not a quantity.
_MAX_PORTION_MULTIPLE = 3.0


def repair_trace_amounts(session: Session) -> dict[str, int]:
    """Re-read a placeholder gram weight as a multiple of a typical portion.

    Some lines survive :func:`repair_contradicted_units` still wrong, because the
    ingredient has no countable unit anywhere to re-read them against: Green Beans
    is ``grams`` on all 1,278 of its lines, so an unlabelled "1" stays 1 g. But the
    corpus does say what green beans usually weigh — 150 g — and "1" plainly means
    one portion of them.

    So the amount is rewritten as ``multiplier x median``. Only ingredients with
    ``_MIN_GRAM_EVIDENCE`` real stated weights qualify, which is what keeps a
    genuine 5 g of sesame seeds intact, and the multiplier is capped so a stray
    large number cannot invent a kilogram.

    Idempotent: a repaired line is no longer a trace amount, so a second run skips
    it. The raw payload remains the source of record if it ever needs rebuilding.
    """
    rows = session.execute(
        select(RecipeIngredient.name, RecipeIngredient.unit, RecipeIngredient.amount)
    ).all()
    typical = _typical_grams_by_name(rows)
    support: dict[str, int] = defaultdict(int)
    for name, unit, amount in rows:
        if name and unit in _MASS_UNITS and amount is not None and amount >= _GRAMS_THRESHOLD:
            support[normalize_name(name)] += 1

    suspects = session.scalars(
        select(RecipeIngredient).where(
            RecipeIngredient.unit.in_(sorted(_MASS_UNITS)),
            RecipeIngredient.amount > 0,
            RecipeIngredient.amount < _GRAMS_THRESHOLD,
        )
    )
    stats = {"examined": 0, "repaired": 0}
    for ing in suspects:
        stats["examined"] += 1
        key = normalize_name(ing.name)
        portion = typical.get(key)
        if portion is None or support.get(key, 0) < _MIN_GRAM_EVIDENCE:
            continue
        if ing.amount is None or ing.amount > _MAX_PORTION_MULTIPLE:
            continue
        ing.amount = round(ing.amount * portion, 1)
        stats["repaired"] += 1
    session.flush()
    return stats


def repair_contradicted_units(session: Session) -> dict[str, int]:
    """Correct already-stored units that the amount's own magnitude contradicts.

    ``backfill_units`` adopts the modal unit for an ingredient, which goes wrong
    when the source mixes units for the same one: Egg Noodle Nest is ``grams`` on
    608 lines and ``nest(s)`` on 10, so an unlabelled "2" became 2 g rather than 2
    nests — and then a whole pack gets bought to satisfy 2 g of demand.

    A replacement unit is only accepted when it converts to roughly what the
    corpus says the ingredient usually weighs. Two guards fall out of that, and
    both matter: 5 g of sesame seeds is left alone because sesame seeds are never
    counted, and "1 Balsamic Vinegar" is left alone because the only countable
    unit it has is ``pack(s)`` and no recipe uses a 250 ml bottle of it.

    Callers must recompute ``amount_g`` afterwards; :func:`app.scraper.enrich`
    does this for the whole corpus on every run.
    """
    rows = session.execute(
        select(RecipeIngredient.name, RecipeIngredient.unit, RecipeIngredient.amount)
    ).all()
    countable_by_name = _countable_units_by_name(rows)
    typical_by_name = _typical_grams_by_name(rows)

    suspects = session.scalars(
        select(RecipeIngredient).where(
            RecipeIngredient.unit.in_(sorted(_MASS_UNITS)),
            RecipeIngredient.amount > 0,
            RecipeIngredient.amount < _GRAMS_THRESHOLD,
        )
    )
    stats = {"examined": 0, "repaired": 0}
    for ing in suspects:
        stats["examined"] += 1
        if not _contradicts_magnitude(ing.unit, ing.amount):
            continue
        key = normalize_name(ing.name)
        reread = _reread_unit(
            ing.name, ing.amount, countable_by_name.get(key, []), typical_by_name.get(key)
        )
        if reread:
            ing.unit = reread
            stats["repaired"] += 1
    session.flush()
    return stats
