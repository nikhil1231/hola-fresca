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
from statistics import median

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import Recipe, RecipeIngredient

_REFERENCE_PATH = Path(__file__).parent / "data" / "ingredient_grams.json"
_SPICE_DOSE_PATH = Path(__file__).parent / "data" / "ingredient_spice_doses.json"

# Sachet/pot contents split into two physical classes: a spoonful of dry powder
# weighs nothing like a squeeze of puree, and one constant cannot serve both.
_WET_WORDS = (
    "puree", "paste", "sauce", "honey", "oil", "vinegar", "ketchup", "mayonnaise",
    "mayo", "syrup", "mustard", "jelly", "butter", "avocado", "nduja", "harissa",
)

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
# Above this a small gram amount reads as a real weight rather than a stand-in
# for "one of them": 5 g of sesame seeds is a garnish, 1 g of kalettes is not a
# vegetable. Only the placeholder end gets the reference-table fallback.
_PLACEHOLDER_MAX = 2.0
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
        "by_name_unit": {
            k.lower(): {u: float(g) for u, g in units.items()}
            for k, units in data.get("by_name_unit", {}).items()
        },
        "by_keyword": [(k.lower(), float(v)) for k, v in data["by_keyword"]],
        "by_unit": {k: float(v) for k, v in data["by_unit"].items()},
        "unit_ceiling": {k: float(v) for k, v in data.get("unit_ceiling", {}).items()},
    }


@lru_cache(maxsize=1)
def _spice_doses() -> dict:
    data = json.loads(_SPICE_DOSE_PATH.read_text())
    return {
        "by_name": {k.lower(): v for k, v in data["by_name"].items()},
        "defaults": data["defaults"],
    }


def spice_dose(name: str, unit: str | None) -> dict | None:
    """The dose record for one pre-portioned container of ``name``, or None.

    The single source of truth for both what the planner buys and what the cook
    measures: ``grams`` is canonical and teaspoons are derived from it, so the two
    numbers cannot drift apart the way two independent tables did.
    """
    if not unit:
        return None
    doses = _spice_doses()
    norm = normalize_name(name)
    entry = doses["by_name"].get(norm)
    if entry is not None:
        return entry
    per_unit = doses["defaults"].get(unit)
    if per_unit is None:
        return None
    kind = "wet" if any(w in norm for w in _WET_WORDS) else "dry"
    return per_unit.get(kind)


def normalize_name(name: str) -> str:
    ascii_name = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    return re.sub(r"\s+", " ", ascii_name).strip()


def _grams_per_unit(name: str, unit: str) -> float | None:
    """Grams for one ``unit`` of ``name``: exact name, then keyword, then the unit.

    The keyword tier matches on substrings and knows nothing about the container,
    which is how "Chicken Stock Powder" came to weigh 160 g a sachet — it contains
    "chicken", and a chicken breast is 160 g. A keyword hit that is impossible for
    the container it arrives in is therefore rejected rather than trusted: a sachet
    holds a spoonful of something, whatever word appears in its name. Exact
    ``by_name`` entries are exempt, being a deliberate statement about that
    ingredient rather than an inference from part of its name.
    """
    # Pre-portioned seasoning containers are governed by the dose table, which is
    # also what the cook is shown, so the shopping weight and the measured spoonful
    # are the same statement rather than two that can disagree.
    dose = spice_dose(name, unit)
    if dose is not None:
        return float(dose["grams"])
    ref = _reference()
    norm = normalize_name(name)
    # A weight authored for one container, e.g. a 400 g tin of cherry tomatoes.
    # Kept apart from the flat table because that one answers a different
    # question — what one of these weighs — and conflating them made a stray
    # "2 cherry tomatoes" worth 800 g.
    scoped = ref["by_name_unit"].get(norm)
    if scoped is not None and unit in scoped:
        return scoped[unit]
    if norm in ref["by_name"]:
        return ref["by_name"][norm]
    ceiling = ref["unit_ceiling"].get(unit)
    for keyword, grams in ref["by_keyword"]:
        if keyword in norm:
            if ceiling is not None and grams > ceiling:
                break
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


def is_mislabelled_weight(unit: str | None, amount: float | None) -> bool:
    """True when a counted unit carries an amount only a weight could be.

    The mirror of :func:`_contradicts_magnitude`, and the more expensive of the
    two. ``to_grams`` already reads "600 unit(s) Butternut Squash" as 600 g, but
    it only returns a number — the unit word stays ``unit(s)``, and everything
    downstream still believes the line is a count. The recipe page then offers
    the cook six hundred squashes, and the planner, covering count ingredients in
    whole units, prices them: 600 × 550 g came to 330 kg and £2,850 of squash in
    a two-person traybake.

    So the label has to be corrected, not just worked around at conversion time.
    """
    return bool(
        unit
        and unit not in _MASS_UNITS
        and unit not in _SPOON
        and amount is not None
        and amount >= _COUNT_MAX
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

    filled = {"by_id": 0, "by_name": 0, "by_magnitude": 0, "count_veto": 0, "weight_veto": 0}
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
        elif is_mislabelled_weight(unit, ing.amount):
            # The modal unit is a count, but nobody cooks 600 of anything.
            unit, tier = "grams", "weight_veto"
        if unit:
            ing.unit = unit
            filled[tier] += 1
    session.flush()
    return filled


# A trace weight is only re-read against a median built from at least this many
# stated weights *that agree with each other*; see _stable_median. A flat count
# threshold was the gate before, set at 20, and it was too blunt in both
# directions: it let a scattered 20 through and turned away Flank Steak, stated
# six times and landing on 150 g a head in five of them.
_MIN_GRAM_EVIDENCE = 4
# How far a stated weight may sit from the median before it stops corroborating
# it. Wide enough for real portion variation, narrow enough that two clusters
# masquerading as one norm fail the test.
_MEDIAN_SPREAD = 0.35
# The largest multiple of a portion a trace amount is allowed to mean. "3" reads
# as three portions; beyond that the number is noise, not a quantity.
_MAX_PORTION_MULTIPLE = 3.0
# What a recipe serves when it does not say. 15,219 of the corpus's 15,982
# recipes are two-serving, so this is the norm rather than a neutral guess.
_DEFAULT_YIELD = 2


def _stable_median(values: list[float], *, min_n: int = _MIN_GRAM_EVIDENCE) -> float | None:
    """The median of ``values``, or None when they are too few or too scattered.

    Thin evidence is usable exactly when it agrees with itself. Six readings that
    cluster are a norm; twenty that disagree are not, however impressive the
    count — which is the failure mode a bare ``len()`` threshold cannot see and
    this one rejects.
    """
    if len(values) < min_n:
        return None
    mid = median(values)
    if not mid:
        return None
    agreeing = sum(1 for v in values if abs(v - mid) <= _MEDIAN_SPREAD * mid)
    return mid if agreeing * 2 >= len(values) else None


def per_serving_norms(rows) -> dict[str, float]:
    """Grams *per serving* each name is stated at, where the corpus agrees on one.

    ``rows`` are ``(name, unit, amount, base_yield)``. Dividing by the recipe's
    own yield is what makes a weight from a four-serving recipe comparable with
    one from a two-serving recipe: Flank Steak is stated at 300 g, 450 g and
    600 g across yields of 2, 3 and 4, which is one figure — 150 g a head — not
    three. Medianing the raw line amounts instead reads that spread as
    disagreement and lands on 300, a number that is only correct for half the
    corpus.
    """
    per_serving: dict[str, list[float]] = defaultdict(list)
    for name, unit, amount, base_yield in rows:
        if not name or unit not in _MASS_UNITS or amount is None:
            continue
        if amount < _GRAMS_THRESHOLD:
            continue
        # A recipe that does not state its yield is read as the corpus norm rather
        # than discarded; 488 of them say nothing, and their weights are still
        # evidence.
        per_serving[normalize_name(name)].append(amount / (base_yield or _DEFAULT_YIELD))
    norms = {}
    for key, values in per_serving.items():
        norm = _stable_median(values)
        if norm is not None:
            norms[key] = norm
    return norms


def expected_grams(norms: dict[str, float], name: str, base_yield: int | None) -> float | None:
    """What a recipe of this size normally uses of ``name``, or None if unknown."""
    norm = norms.get(normalize_name(name))
    return None if norm is None else norm * (base_yield or _DEFAULT_YIELD)


def repair_trace_amounts(session: Session) -> dict[str, int]:
    """Re-read a placeholder gram weight as the quantity a recipe this size uses.

    Some lines survive :func:`repair_contradicted_units` still wrong, because the
    ingredient has no countable unit anywhere to re-read them against: Green Beans
    is ``grams`` on all 1,278 of its lines, so an unlabelled "1" stays 1 g, and
    Flank Steak is ``grams`` on all 25 of its, so "2" stays 2 g and reaches the
    recipe page as "4g of steak". But the corpus does say what each of them
    weighs a head, and that — scaled to this recipe's yield — is the answer.

    Two guards keep a real weight intact. The amount must be small enough to be a
    stand-in rather than a quantity (``_MAX_PORTION_MULTIPLE``), which is what
    leaves 5 g of sesame seeds alone; and the norm must come from stated weights
    that agree with each other (:func:`_stable_median`), so an ingredient the
    corpus cannot speak for is left as it is rather than guessed about.

    Idempotent: a repaired line is no longer a trace amount, so a second run skips
    it. The raw payload remains the source of record if it ever needs rebuilding.
    """
    rows = session.execute(
        select(
            RecipeIngredient.name,
            RecipeIngredient.unit,
            RecipeIngredient.amount,
            Recipe.base_yield,
        ).join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
    ).all()
    norms = per_serving_norms(rows)

    suspects = session.execute(
        select(RecipeIngredient, Recipe.base_yield)
        .join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
        .where(
            RecipeIngredient.unit.in_(sorted(_MASS_UNITS)),
            RecipeIngredient.amount > 0,
            RecipeIngredient.amount < _GRAMS_THRESHOLD,
        )
    ).all()
    stats = {"examined": 0, "repaired": 0}
    for ing, base_yield in suspects:
        stats["examined"] += 1
        if ing.amount is None or ing.amount > _MAX_PORTION_MULTIPLE:
            continue
        expected = expected_grams(norms, ing.name, base_yield)
        if expected is None:
            # The corpus cannot vouch for this ingredient — Kalettes appears five
            # times in total, so there is no median to trust. Fall back to what
            # the reference says one whole item weighs, which is the same claim
            # the placeholder is making, and read the amount as a count of them.
            # Restricted to amounts of one or two, because past that a small
            # number is more likely a real weight than a stand-in.
            if ing.amount > _PLACEHOLDER_MAX:
                continue
            per_item = _grams_per_unit(ing.name, _COUNT_UNIT)
            if per_item is None:
                continue
            ing.amount = round(ing.amount * per_item, 1)
            stats["repaired"] += 1
            continue
        ing.amount = round(_placeholder_quantity(ing.amount, expected), 1)
        stats["repaired"] += 1
    session.flush()
    return stats


def _placeholder_quantity(amount: float, expected: float) -> float:
    """Read a placeholder against what a recipe this size normally uses.

    A whole number is a count of what the source ships — two steaks, one bag of
    beans — and not a number of recipes' worth. Two steaks in a two-person recipe
    is still one recipe's worth of steak, so the corpus norm answers it outright
    and the count adds no multiple: reading "2" as two portions is what turned
    300 g of flank steak into 600 g. The source is not consistent enough to say
    otherwise, writing "1" for the same ingredient at both two and four servings.

    A fraction is the one reading that unambiguously scales, because nothing is
    shipped in halves: "0.5" against a 25 g norm really is half a portion.
    """
    if amount >= 1 and float(amount).is_integer():
        return expected
    return amount * expected


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

    # Already-stored counts carrying a weight, the mirror of the case below. Left
    # alone these keep their unit(s) label for ever, since nothing else revisits it.
    mislabelled = session.scalars(
        select(RecipeIngredient).where(
            RecipeIngredient.unit.not_in(sorted(_MASS_UNITS) + sorted(_SPOON)),
            RecipeIngredient.unit.is_not(None),
            RecipeIngredient.amount >= _COUNT_MAX,
        )
    )
    stats = {"examined": 0, "repaired": 0}
    for ing in mislabelled:
        stats["examined"] += 1
        if is_mislabelled_weight(ing.unit, ing.amount):
            ing.unit = "grams"
            stats["repaired"] += 1

    suspects = session.scalars(
        select(RecipeIngredient).where(
            RecipeIngredient.unit.in_(sorted(_MASS_UNITS)),
            RecipeIngredient.amount > 0,
            RecipeIngredient.amount < _GRAMS_THRESHOLD,
        )
    )
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
