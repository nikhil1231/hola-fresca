"""Protein modifiers: swap the protein of a recipe, or scale it.

A modifier is a *hypothetical*, and that is the whole design. Nothing here writes
to the recipe: a modifier is resolved against the stored recipe on the way past,
producing a different set of ingredient demands and a different set of macros for
this week only. Contrast :class:`~app.db.models.RecipeEdit`, which materialises
onto the row precisely because a correction is a claim that the stored number is
wrong. A swap makes no such claim, so browse filters and facet counts keep
reading the library as published and only the recipe page and the basket see the
modified version.

Three things are computed, all deterministic:

- **Which line is the protein.** The heaviest line that the reference table
  recognises, above a floor that keeps a garnish of chorizo from outranking the
  chicken it is sprinkled on.
- **What the modifier does to the demand.** One factor on that line's grams, and
  optionally a different ingredient key to buy instead. Portions scaling stays
  the planner's job and multiplies on top, which is why the factor is expressed
  per portion and never mentions the week.
- **What it does to the macros.** The line's own contribution is subtracted from
  the recipe's stated per-portion figures, leaving a fixed "rest of the dish"
  residual, and the new protein's contribution is added back. The residual is
  also what makes a *target* ("50 g of protein a portion") solvable in closed
  form rather than by search.

The arithmetic is only as good as ``app/data/protein_reference.json``, and that
file holds one typical figure per cut. So every macro this module returns is an
estimate and is labelled as one; what it is emphatically not is a
recalculation of HelloFresh's own laboratory numbers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.canonicalize import normalize_name

_REFERENCE_PATH = Path(__file__).parent / "data" / "protein_reference.json"

KEY_PREFIX = "name:"

#: Below this the line is a seasoning, not the dish's protein: chorizo appears at
#: 15 g as a garnish and at 200 g as the main event, and only weight tells them
#: apart. Stated for the recipe's whole base yield, so ~30 g a portion.
MIN_PROTEIN_GRAMS = 60.0

#: How far a modifier may take the protein line. Beyond quadruple the recipe is
#: not the recipe any more, and below a quarter the covering arithmetic is
#: pricing a rounding error.
MIN_FACTOR = 0.25
MAX_FACTOR = 4.0

#: Swapping to one of these makes the dish meat-free, which the meat stock in it
#: would quietly undo. See :func:`companion_swaps`.
MEAT_FREE_TYPES = frozenset({"tofu", "halloumi"})

_MEAT_STOCKS = {
    "name:chicken stock paste": "name:vegetable stock paste",
    "name:beef stock paste": "name:vegetable stock paste",
    "name:chicken broth paste": "name:vegetable stock paste",
    "name:knorr chicken stock": "name:vegetable stock paste",
    "name:chicken stock powder": "name:vegetable stock powder",
    "name:beef stock powder": "name:vegetable stock powder",
    "name:chicken stock pot": "name:vegetable stock pot",
    "name:beef stock pot": "name:vegetable stock pot",
}

_STOCK_NAMES = {
    "name:vegetable stock paste": "Vegetable Stock Paste",
    "name:vegetable stock powder": "Vegetable Stock Powder",
    "name:vegetable stock pot": "Vegetable Stock Pot",
}

TARGET_MODES = ("protein_g", "energy_kcal")


@dataclass(frozen=True, slots=True)
class Macros:
    kcal: float = 0.0
    protein_g: float = 0.0
    fat_g: float = 0.0
    carbs_g: float = 0.0

    def scaled(self, factor: float) -> "Macros":
        return Macros(
            kcal=self.kcal * factor,
            protein_g=self.protein_g * factor,
            fat_g=self.fat_g * factor,
            carbs_g=self.carbs_g * factor,
        )

    def __add__(self, other: "Macros") -> "Macros":
        return Macros(
            kcal=self.kcal + other.kcal,
            protein_g=self.protein_g + other.protein_g,
            fat_g=self.fat_g + other.fat_g,
            carbs_g=self.carbs_g + other.carbs_g,
        )

    def __sub__(self, other: "Macros") -> "Macros":
        return Macros(
            kcal=self.kcal - other.kcal,
            protein_g=self.protein_g - other.protein_g,
            fat_g=self.fat_g - other.fat_g,
            carbs_g=self.carbs_g - other.carbs_g,
        )

    def clamped(self) -> "Macros":
        """No macro may go negative, whatever the reference table says.

        A residual is a real quantity of food, so a negative one is arithmetic
        telling on itself — either the stated macros or our per-100 g figure is
        off. Clamping keeps the answer usable; :attr:`Resolution.warnings` is
        where the caller learns not to trust it too far.
        """
        return Macros(
            kcal=max(0.0, self.kcal),
            protein_g=max(0.0, self.protein_g),
            fat_g=max(0.0, self.fat_g),
            carbs_g=max(0.0, self.carbs_g),
        )

    def rounded(self) -> "Macros":
        return Macros(
            kcal=round(self.kcal),
            protein_g=round(self.protein_g, 1),
            fat_g=round(self.fat_g, 1),
            carbs_g=round(self.carbs_g, 1),
        )

    @property
    def negative(self) -> bool:
        return min(self.kcal, self.protein_g, self.fat_g, self.carbs_g) < -0.5


@dataclass(frozen=True, slots=True)
class ProteinIngredient:
    """One recognised protein ingredient and what it is made of."""

    key: str
    type: str
    form: str
    noun: str
    per_100g: Macros


@dataclass(frozen=True, slots=True)
class SwapTarget:
    """A protein you may swap *to*, and the ingredient key it buys per form.

    A target with no key for a form is not offered in that form. That is the
    whole of the type-by-form table: pork exists as mince and the library has no
    whole pork cut we map, so a chicken-breast recipe is never offered pork.
    """

    id: str
    label: str
    noun: str
    keys: dict[str, str]
    cook_note: str

    def key_for(self, form: str) -> str | None:
        return self.keys.get(form)


@dataclass(frozen=True, slots=True)
class ProteinModifier:
    """What the user asked for. Frozen so it can key a cache."""

    swap_to: str | None = None
    scale: float | None = None
    target_mode: str | None = None
    target_value: float | None = None

    @property
    def empty(self) -> bool:
        return (
            self.swap_to is None
            and self.scale is None
            and (self.target_mode is None or self.target_value is None)
        )


@dataclass(frozen=True, slots=True)
class ProteinLine:
    """The recipe's protein, as the recipe states it."""

    key: str
    name: str
    grams: float
    units: float | None
    ingredient: ProteinIngredient

    @property
    def type(self) -> str:
        return self.ingredient.type

    @property
    def form(self) -> str:
        return self.ingredient.form


@dataclass(frozen=True, slots=True)
class Resolution:
    """A resolved modifier: what to buy, how much, and what it does to a portion."""

    source: ProteinLine
    target: SwapTarget | None
    target_key: str | None
    target_name: str | None
    target_noun: str | None
    factor: float
    grams_before: float
    grams_after: float
    macros_before: Macros
    macros_after: Macros
    residual: Macros
    cook_note: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def swapped(self) -> bool:
        return self.target_key is not None and self.target_key != self.source.key

    @property
    def changed(self) -> bool:
        return self.swapped or abs(self.factor - 1.0) > 1e-6


# --- reference table --------------------------------------------------------


@lru_cache(maxsize=1)
def _reference() -> tuple[dict[str, ProteinIngredient], dict[str, SwapTarget]]:
    data = json.loads(_REFERENCE_PATH.read_text())
    ingredients = {
        name: ProteinIngredient(
            key=f"{KEY_PREFIX}{name}",
            type=entry["type"],
            form=entry["form"],
            noun=entry["noun"],
            per_100g=Macros(**entry["per_100g"]),
        )
        for name, entry in data["ingredients"].items()
    }
    targets = {
        t["id"]: SwapTarget(
            id=t["id"],
            label=t["label"],
            noun=t["noun"],
            keys={form: f"{KEY_PREFIX}{name}" for form, name in t["keys"].items()},
            cook_note=t["cook_note"],
        )
        for t in data["targets"]
    }
    return ingredients, targets


def _normalized(name_or_key: str) -> str:
    if name_or_key.startswith(KEY_PREFIX):
        return name_or_key[len(KEY_PREFIX) :]
    return normalize_name(name_or_key)


def lookup(name_or_key: str | None) -> ProteinIngredient | None:
    """The reference entry for an ingredient key or a raw recipe line name.

    Both forms resolve through the same table because an ``ingredient_key`` is
    ``"name:" + normalize_name(name)`` — the mapping's own convention — so a line
    the planner never keyed still finds its entry by name.
    """
    if not name_or_key:
        return None
    ingredients, _ = _reference()
    return ingredients.get(_normalized(name_or_key))


def target(target_id: str | None) -> SwapTarget | None:
    if not target_id:
        return None
    _, targets = _reference()
    return targets.get(target_id)


def targets_for_form(form: str) -> list[SwapTarget]:
    """Every target that can be bought in this form, in reference order."""
    _, targets = _reference()
    return [t for t in targets.values() if t.key_for(form)]


def display_name(key: str) -> str:
    """A human name for an ingredient key we may never have shown before."""
    if key in _STOCK_NAMES:
        return _STOCK_NAMES[key]
    return _normalized(key).title()


# --- finding the protein ----------------------------------------------------


def find_protein_line(
    lines: list[tuple[str, str, float, float | None]],
) -> ProteinLine | None:
    """The dish's main protein from ``(key, display name, grams, units)`` lines.

    Heaviest recognised line wins. Weight is the right discriminator because the
    alternative — position, or the name of the dish — gets Chicken & Chorizo
    Paella wrong in both directions, while 250 g of chicken against 60 g of
    chorizo is not a close call.
    """
    best: ProteinLine | None = None
    for key, name, grams, units in lines:
        ingredient = lookup(key) or lookup(name)
        if ingredient is None or grams < MIN_PROTEIN_GRAMS:
            continue
        if best is None or grams > best.grams:
            best = ProteinLine(
                key=key, name=name, grams=grams, units=units, ingredient=ingredient
            )
    return best


# --- resolving a modifier ---------------------------------------------------


def resolve(
    line: ProteinLine,
    modifier: ProteinModifier,
    *,
    base_yield: int,
    recipe_macros: Macros,
) -> Resolution:
    """Turn a request into a factor, a key to buy, and a portion's macros.

    ``recipe_macros`` are per portion, as the library stores them, and everything
    returned is per portion too. The week's portion count multiplies on top of
    this in the planner and must not appear here: a modifier is a statement about
    what a portion contains, not about how many are being cooked.
    """
    warnings: list[str] = []
    servings = max(1, base_yield)

    source_ref = line.ingredient
    swap = target(modifier.swap_to)
    target_key: str | None = None
    target_ref = source_ref
    if swap is not None:
        target_key = swap.key_for(line.form)
        if target_key is None:
            warnings.append(
                f"{swap.label} is not sold in a form that suits this recipe; "
                f"kept {line.name}."
            )
            swap = None
        else:
            target_ref = lookup(target_key) or source_ref

    per_portion_before = line.grams / servings
    contribution_before = target_contribution(source_ref, per_portion_before)
    residual = (recipe_macros - contribution_before)
    if residual.negative:
        warnings.append(
            "The recipe's published macros do not fully account for its protein, "
            "so the figures below are rougher than usual."
        )
    residual = residual.clamped()

    factor = 1.0
    if modifier.scale is not None:
        factor = float(modifier.scale)
    elif modifier.target_mode and modifier.target_value is not None:
        factor, target_warning = _factor_for_target(
            mode=modifier.target_mode,
            value=float(modifier.target_value),
            residual=residual,
            per_100g=target_ref.per_100g,
            grams_before=per_portion_before,
        )
        if target_warning:
            warnings.append(target_warning)

    clamped = min(MAX_FACTOR, max(MIN_FACTOR, factor))
    if abs(clamped - factor) > 1e-6:
        warnings.append(
            f"Capped at {_fmt_factor(clamped)} the protein — "
            f"{_fmt_factor(factor)} is past what this recipe can carry."
        )
    factor = clamped

    grams_after = line.grams * factor
    macros_after = residual + target_contribution(target_ref, grams_after / servings)

    return Resolution(
        source=line,
        target=swap,
        target_key=target_key,
        target_name=display_name(target_key) if target_key else None,
        target_noun=swap.noun if swap else None,
        factor=factor,
        grams_before=line.grams,
        grams_after=grams_after,
        macros_before=recipe_macros,
        macros_after=macros_after,
        residual=residual,
        cook_note=swap.cook_note if swap is not None else None,
        warnings=tuple(warnings),
    )


def target_contribution(ingredient: ProteinIngredient, grams: float) -> Macros:
    return ingredient.per_100g.scaled(grams / 100.0)


def _factor_for_target(
    *,
    mode: str,
    value: float,
    residual: Macros,
    per_100g: Macros,
    grams_before: float,
) -> tuple[float, str | None]:
    """Solve for the protein weight that hits a per-portion macro target.

    Closed form, because the rest of the dish is fixed: the protein is the only
    term that moves, so ``(target - residual) / per-gram`` is the answer and no
    search is needed.
    """
    if grams_before <= 0:
        return 1.0, None
    if mode == "protein_g":
        available = residual.protein_g
        per_gram = per_100g.protein_g / 100.0
        label = f"{value:g} g of protein"
    elif mode == "energy_kcal":
        available = residual.kcal
        per_gram = per_100g.kcal / 100.0
        label = f"{value:g} kcal"
    else:
        return 1.0, None

    if per_gram <= 0:
        return 1.0, "This protein has no reference figure to solve against."
    needed = (value - available) / per_gram
    if needed <= 0:
        return (
            MIN_FACTOR,
            f"The rest of the dish already provides {label} a portion, "
            "so the protein is only at its floor here.",
        )
    return needed / grams_before, None


def _fmt_factor(factor: float) -> str:
    return f"{factor:.2f}".rstrip("0").rstrip(".") + "x"


# --- quantities -------------------------------------------------------------


def snap_units(units: float) -> float:
    """Half a fillet is a real thing to cook; a third of one is not."""
    return max(0.5, round(units * 2) / 2)


def swapped_quantity(
    grams: float, *, unit_kind: str, each_to_grams: float | None
) -> tuple[float, float | None]:
    """``(grams, units)`` for a demand landing on a possibly counted ingredient.

    Equal weight is the rule — the swap keeps the dish the same size — but an
    ingredient the planner buys by the piece has to arrive at a number of pieces,
    and the grams then follow from the count rather than the other way round, so
    the basket and the ingredient list cannot disagree about what is being
    cooked. Mirrors what ``index._load_recipes`` does to a recipe's own lines.
    """
    if unit_kind != "count" or not each_to_grams:
        return grams, None
    units = snap_units(grams / each_to_grams)
    return units * each_to_grams, units


# --- companions -------------------------------------------------------------


def companion_swaps(
    keys: list[str], resolution: Resolution
) -> dict[str, str]:
    """``{old key: new key}`` for lines the protein swap drags with it.

    Only one class of these is deterministic enough to apply without asking:
    a meat stock in a dish whose meat has just been swapped out for tofu. Left
    alone it makes a "vegetarian" swap that is nothing of the kind, and it is a
    real basket line, not a label. Everything subtler — the rub sized to the
    chicken, the sauce built around pork fat — is left exactly as the recipe
    states it, which is the honest default and the one a cook can predict.
    """
    if resolution.target is None or resolution.target.id not in MEAT_FREE_TYPES:
        return {}
    return {key: _MEAT_STOCKS[key] for key in keys if key in _MEAT_STOCKS}


# --- step text --------------------------------------------------------------

# Words that turn a protein noun into something else entirely. "Chicken stock"
# must survive a swap to tofu untouched, and would not if the noun alone decided.
_NOT_THE_PROTEIN = (
    r"(?!\s*(?:stock|broth|bouillon|gravy|seasoning|salt|paste|powder|pot|"
    r"stock\s+pot|style)\b)"
)

# Qualifiers HelloFresh puts in front of an ingredient name that a cook drops
# when writing the step: "Diced British Chicken Breast" is "the chicken".
_QUALIFIERS = re.compile(
    r"^(?:21\s+day\s+aged\s+|british\s+|diced\s+|skin[-\s]on\s+|slow\s+cooked\s+|"
    r"cooked\s+|large\s+|firm\s+|smoked\s+|hot\s+smoked\s+|whole\s+british\s+|"
    r"free\s+range\s+)+",
    re.I,
)


def _variants(name: str, noun: str) -> list[str]:
    """Every phrase in a step that means this ingredient, longest first."""
    base = name.strip()
    forms = {base, _QUALIFIERS.sub("", base).strip(), noun}
    forms |= {f[:-1] for f in list(forms) if f.lower().endswith("s") and len(f) > 3}
    # Only single words get a plural: "beef minces" is not a phrase any step
    # contains, and every pattern that cannot match still costs a pass over it.
    forms |= {f"{f}s" for f in list(forms) if " " not in f and not f.lower().endswith("s")}
    return sorted({f for f in forms if len(f) > 2}, key=len, reverse=True)


def _match_case(source: str, replacement: str) -> str:
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def rewrite_text(text: str | None, resolution: Resolution) -> str | None:
    """Rename the protein wherever a step names it.

    A deterministic rename rather than a rewrite: HelloFresh's steps name
    ingredients exactly as the ingredient list does, so substitution reads
    naturally far more often than it has any right to, and — unlike a generated
    rewrite — it can never invent a step that was not there. What it cannot do is
    re-time the cooking, which is what :attr:`SwapTarget.cook_note` is for.
    """
    if not text or not resolution.swapped or resolution.target is None:
        return text
    # Always the bare noun, never the shopping name: a step says "add the
    # chicken", so "add the tofu" is what belongs there, and "add the Firm Tofu
    # 280g" is not a sentence anyone wrote.
    replacement = resolution.target.noun
    result = text
    for phrase in _variants(resolution.source.name, resolution.source.ingredient.noun):
        pattern = re.compile(rf"\b{re.escape(phrase)}\b{_NOT_THE_PROTEIN}", re.I)
        result = pattern.sub(lambda m: _match_case(m.group(0), replacement), result)
    return result


def rename_companion(name: str, resolution: Resolution) -> str:
    """Relabel lines named after the protein: "Oil for the Chicken".

    Pure display. These lines are oil and water — nothing about them changes when
    the chicken does — but leaving the old name on the ingredient list makes the
    page contradict itself.
    """
    if not resolution.swapped or resolution.target is None:
        return name
    if not re.search(r"\bfor the\b", name, re.I):
        return name
    return rewrite_text(name, resolution) or name
