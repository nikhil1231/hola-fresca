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


# --- Ingredient → product mapping review -----------------------------------

class MappingListItem(BaseModel):
    ingredient_key: str
    name: str
    status: str
    line_count: int
    spend_score: float | None = None
    num_candidates: int
    num_accepted: int
    needs_substitution: bool
    pantry_staple: bool = False
    alias_of: str | None = None
    each_to_grams: float | None = None
    top_product_name: str | None = None


class MappingListOut(BaseModel):
    items: list[MappingListItem]
    counts: dict[str, int]


class MappingCandidateOut(BaseModel):
    product_id: int
    sku: str
    name: str
    brand: str | None = None
    pack_size_raw: str | None = None
    pack_size_value: float | None = None
    pack_size_unit: str | None = None
    price: float | None = None
    unit_price: float | None = None
    unit_price_basis: str | None = None
    avg_rating: float | None = None
    ratings_count: int | None = None
    url: str | None = None
    result_rank: int
    search_term: str | None = None
    # Decision overlay
    accepted: bool = False
    rank: int | None = None
    match_type: str | None = None
    reason: str | None = None


class MappingDetailOut(BaseModel):
    ingredient_key: str
    name: str
    status: str | None = None
    line_count: int
    spend_score: float | None = None
    each_to_grams: float | None = None
    needs_substitution: bool = False
    pantry_staple: bool = False
    search_term: str | None = None
    alias_of: str | None = None
    alias_of_name: str | None = None
    decided_by: str | None = None
    model: str | None = None
    llm_notes: str | None = None
    reviewer_notes: str | None = None
    usage: dict = {}
    candidates: list[MappingCandidateOut] = []


class AcceptedIn(BaseModel):
    sku: str
    rank: int = 1
    match_type: str = "exact"
    reason: str | None = None


class DecisionIn(BaseModel):
    status: str
    accepted: list[AcceptedIn] = []
    each_to_grams: float | None = None
    needs_substitution: bool = False
    pantry_staple: bool = False
    reviewer_notes: str | None = None


class SearchIn(BaseModel):
    term: str


class AliasIn(BaseModel):
    # None clears the alias and returns the ingredient to the review queue.
    alias_of: str | None = None


class AliasOut(BaseModel):
    ingredient_key: str
    name: str
    alias_of: str
    alias_of_name: str


class AliasListOut(BaseModel):
    items: list[AliasOut] = []


class GenerateIn(BaseModel):
    count: int = 10


class JobOut(BaseModel):
    job_id: str
    status: str
    processed: int = 0
    total: int = 0
    added: int = 0
    staples: int = 0
    no_match: int = 0
    errors: int = 0
    error: str | None = None
    current: str | None = None


class BulkApproveIn(BaseModel):
    keys: list[str]
