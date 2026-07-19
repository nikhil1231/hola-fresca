"""Source-agnostic intermediate representation (IR).

Every source adapter converts its raw payload into a :class:`NormalizedRecipe`.
Everything downstream of this point — database writes, and later the
canonicalisation and planner layers — depends only on the IR, never on a
particular source's payload shape. Adding a new source (a generic schema.org
adapter, an LLM extractor, a one-off link) means writing a new adapter that
emits this IR, and nothing downstream changes.

Raw ingredient text is always preserved alongside any parsed amount/unit so the
shared canonicalisation layer, not each adapter, owns the interpretation of
messy ingredient lines.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NormalizedIngredient:
    name: str
    raw_text: str | None = None
    source_ingredient_id: str | None = None
    type: str | None = None
    slug: str | None = None
    amount: float | None = None
    unit: str | None = None
    image_path: str | None = None


@dataclass
class NormalizedStep:
    index: int
    instructions_text: str | None = None
    instructions_html: str | None = None


@dataclass
class NormalizedNutrition:
    name: str
    amount: float | None = None
    unit: str | None = None


@dataclass
class NormalizedTag:
    name: str
    type: str | None = None
    slug: str | None = None


@dataclass
class NormalizedAllergen:
    name: str
    slug: str | None = None


@dataclass
class NormalizedRecipe:
    source: str
    source_id: str
    url: str
    name: str

    headline: str | None = None
    slug: str | None = None
    description: str | None = None
    difficulty: int | None = None
    prep_time_min: int | None = None
    total_time_min: int | None = None
    serving_size_g: float | None = None
    base_yield: int | None = None
    image_path: str | None = None

    energy_kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None

    ingredients: list[NormalizedIngredient] = field(default_factory=list)
    steps: list[NormalizedStep] = field(default_factory=list)
    nutrition: list[NormalizedNutrition] = field(default_factory=list)
    tags: list[NormalizedTag] = field(default_factory=list)
    cuisines: list[str] = field(default_factory=list)
    allergens: list[NormalizedAllergen] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """True when the recipe carries the data the app actually needs."""
        return bool(self.ingredients and self.steps and self.nutrition)
