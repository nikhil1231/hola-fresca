"""Pydantic response models for the recipe API.

These are hand-built from ORM rows (rather than ``from_attributes``) because the
card/detail shapes flatten relationships and inject computed fields like the CDN
image URL.
"""
from __future__ import annotations

from pydantic import BaseModel


class RecipeCard(BaseModel):
    id: int
    name: str
    headline: str | None = None
    image_url: str | None = None
    energy_kcal: float | None = None
    protein_g: float | None = None
    protein_energy_ratio: float | None = None
    total_time_min: int | None = None
    difficulty: int | None = None
    avg_rating: float | None = None
    ratings_count: int | None = None
    cuisines: list[str] = []
    tags: list[str] = []


class PaginatedRecipes(BaseModel):
    items: list[RecipeCard]
    total: int
    page: int
    page_size: int
    has_more: bool


class IngredientOut(BaseModel):
    name: str
    amount: float | None = None
    unit: str | None = None
    amount_g: float | None = None
    canonical_unit: str | None = None
    image_url: str | None = None


class StepOut(BaseModel):
    index: int
    text: str | None = None


class NutritionOut(BaseModel):
    name: str
    amount: float | None = None
    unit: str | None = None


class RecipeDetail(BaseModel):
    id: int
    name: str
    headline: str | None = None
    description: str | None = None
    image_url: str | None = None
    source_url: str | None = None

    difficulty: int | None = None
    prep_time_min: int | None = None
    total_time_min: int | None = None
    base_yield: int | None = None
    serving_size_g: float | None = None

    energy_kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None
    protein_energy_ratio: float | None = None

    avg_rating: float | None = None
    ratings_count: int | None = None

    cuisines: list[str] = []
    tags: list[str] = []
    allergens: list[str] = []
    ingredients: list[IngredientOut] = []
    steps: list[StepOut] = []
    nutrition: list[NutritionOut] = []


class FacetCount(BaseModel):
    value: str
    label: str
    count: int


class NumericRange(BaseModel):
    min: float
    max: float


class FacetsOut(BaseModel):
    cuisines: list[FacetCount]
    diets: list[FacetCount]
    attributes: list[FacetCount]
    proteins: list[FacetCount]
    excludes: list[FacetCount]
    ranges: dict[str, NumericRange]
    sorts: list[FacetCount]
