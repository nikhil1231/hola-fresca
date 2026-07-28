"""Check our gram estimates against HelloFresh's own stated serving weight.

`recipes.serving_size_g` is published for 4,481 of the 4,826 curated recipes and
is independent of everything we derive: it is what the dish weighs on the plate.
Summing our per-ingredient grams and dividing by ``serving_size_g × base_yield``
therefore grades the whole reference table at once. It currently lands near 1.0,
which says the model is broadly right — and the residual says where it is not.

What this is not
----------------
It is tempting to solve the whole system: thousands of equations, a few hundred
unknown per-unit weights, fit them all. That does not work, and the failure is
instructive rather than technical. Started from a neutral 50 g, such a fit
converges to an excellent aggregate score while putting garlic clove at 21 g and
ground cumin at 45 g. Ingredients that appear in nearly every recipe and carry
almost no mass behave like an intercept term: they soak up whatever error is
left over, and the arithmetic has no way to tell that from a real weight.

So this module deliberately estimates one ingredient at a time, holding every
other value fixed, and reports rather than decides. The reading is only
trustworthy where an ingredient carries enough of the dish for its weight to
show up in the total — a tomato passata carton at 80% of the recipe, not a
coriander bunch at 2%. Below that floor the number returned is the corpus-wide
bias, which is why the floors below are not tunable niceties but the difference
between a measurement and a mirage.

Spices are permanently out of reach here: a sachet is ~2% of a dish, well under
the noise from liquids absorbed or boiled off. Those live in
``ingredient_spice_doses.json`` and are anchored by weighing packaging instead.
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from app import config

METRIC_UNITS = ("grams", "milliliter(s)")

# Below these an ingredient's weight does not move the recipe total enough to be
# read back out. See the module docstring: the herbs sit at 2% share and return
# the global bias no matter what their real weight is.
MIN_MASS_SHARE = 0.25
MIN_RECIPES = 40
# Ignore ingredients already within this much of what the data wants; the constant
# is not worth a review round.
MIN_DISAGREEMENT = 0.12
# A single line of 30 kg of water is a source typo and would swamp its recipe.
MAX_PLAUSIBLE_LINE_G = 3000.0

EXPORT_PATH = config.ROOT_DIR / "exports" / "gram_suggestions.csv"

FIELDS = (
    "status", "ingredient", "unit", "current_g", "suggested_g", "multiplier",
    "recipes", "mass_share", "note",
)


@dataclass(frozen=True, slots=True)
class Suggestion:
    name: str
    unit: str
    current_g: float
    raw_multiplier: float
    recipes: int
    mass_share: float
    # The corpus-wide ratio, which every ingredient inherits and none causes.
    baseline: float = 1.0

    @property
    def multiplier(self) -> float:
        """What the data wants for *this* ingredient, net of the corpus-wide offset.

        Raw ratios cluster around 0.93 rather than 1.0, because a plated dish
        weighs less than the ingredients that went into it — water boils off, and
        the constraint cannot tell that loss apart from every constant being 7%
        heavy. Dividing it out is the conservative reading: it means an ingredient
        is only flagged when it disagrees with its peers, not when it merely
        shares the offset. Correcting on the raw ratio would quietly shave 7% off
        the entire reference table on the strength of evaporation.
        """
        return self.raw_multiplier / self.baseline if self.baseline else self.raw_multiplier

    @property
    def suggested_g(self) -> float:
        return round(self.current_g * self.multiplier, 1)

    @property
    def disagreement(self) -> float:
        return abs(self.multiplier - 1.0)


def _load(conn: sqlite3.Connection):
    targets = {
        rid: ssg * by
        for rid, ssg, by in conn.execute(
            "SELECT id, serving_size_g, base_yield FROM recipes "
            "WHERE curated = 1 AND serving_size_g IS NOT NULL AND base_yield > 0"
        )
    }
    lines: dict[int, list[tuple[tuple[str, str], float]]] = {}
    anchored: dict[int, float] = {}
    per_unit: dict[tuple[str, str], float] = {}
    rows = conn.execute(
        "SELECT ri.recipe_id, ri.name, ri.unit, ri.amount, ri.amount_g "
        "FROM recipe_ingredients ri JOIN recipes r ON r.id = ri.recipe_id "
        "WHERE r.curated = 1"
    )
    for rid, name, unit, amount, amount_g in rows:
        if rid not in targets or not amount or amount <= 0 or amount_g is None:
            continue
        if amount_g > MAX_PLAUSIBLE_LINE_G:
            continue
        if unit in METRIC_UNITS:
            # The source stated this one, so it is a fixed part of the equation.
            anchored[rid] = anchored.get(rid, 0.0) + amount_g
            continue
        key = (name, unit)
        lines.setdefault(rid, []).append((key, float(amount)))
        per_unit[key] = amount_g / amount
    return targets, lines, anchored, per_unit


def suggestions(db_path: Path | None = None) -> tuple[list[Suggestion], float]:
    """Ranked disagreements between our constants and the stated serving weights."""
    db = db_path or config.DB_PATH
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        targets, lines, anchored, per_unit = _load(conn)

    ratios: dict[int, float] = {}
    for rid, items in lines.items():
        need = targets[rid] - anchored.get(rid, 0.0)
        got = sum(amount * per_unit[key] for key, amount in items)
        if need > 0 and got > 0:
            ratios[rid] = need / got

    appears: dict[tuple[str, str], list[int]] = {}
    for rid, items in lines.items():
        for key, _ in items:
            appears.setdefault(key, []).append(rid)

    baseline = median(ratios.values()) if ratios else 1.0
    out: list[Suggestion] = []
    for key, rids in appears.items():
        scored = [r for r in rids if r in ratios]
        if len(scored) < MIN_RECIPES:
            continue
        shares = [
            (amount * per_unit[key]) / max(targets[r] - anchored.get(r, 0.0), 1.0)
            for r in scored
            for k, amount in lines[r]
            if k == key
        ]
        share = median(shares)
        if share < MIN_MASS_SHARE:
            continue
        candidate = Suggestion(
            name=key[0], unit=key[1], current_g=per_unit[key],
            raw_multiplier=median(ratios[r] for r in scored),
            recipes=len(scored), mass_share=share, baseline=baseline,
        )
        if candidate.disagreement >= MIN_DISAGREEMENT:
            out.append(candidate)

    out.sort(key=lambda s: s.disagreement * s.mass_share, reverse=True)
    return out, baseline


def write_csv(items: list[Suggestion], path: Path | None = None) -> Path:
    """Write the review queue. ``status`` is blank for you to fill in with ``approved``."""
    target = path or EXPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[tuple[str, str], str] = {}
    if target.exists():
        # Keep decisions already made: a re-run must not silently un-approve a row.
        with target.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                existing[(row["ingredient"], row["unit"])] = row.get("status", "")
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(FIELDS)
        for s in items:
            writer.writerow([
                existing.get((s.name, s.unit), ""), s.name, s.unit,
                round(s.current_g, 1), s.suggested_g, round(s.multiplier, 3),
                s.recipes, f"{s.mass_share:.2f}", "",
            ])
    return target
