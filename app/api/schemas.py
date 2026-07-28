"""Pydantic response models for the recipe API.

These are hand-built from ORM rows (rather than ``from_attributes``) because the
card/detail shapes flatten relationships and inject computed fields like the CDN
image URL.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


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
    cuisines: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    intrinsic_score: float | None = None
    intrinsic_cost: float | None = None
    intrinsic_gap_count: int = 0


class PaginatedRecipes(BaseModel):
    items: list[RecipeCard]
    total: int
    page: int
    page_size: int
    has_more: bool
    next_offset: int | None = None


class IngredientOut(BaseModel):
    ingredient_key: str | None = None
    name: str
    amount: float | None = None
    unit: str | None = None
    amount_g: float | None = None
    canonical_unit: str | None = None
    image_url: str | None = None
    unmapped: bool = False
    # Teaspoons for one pre-portioned container (sachet, pot), where that is the
    # measure the cook can actually act on. None when the line already states a
    # metric amount or a spoon, or when the ingredient is not spoonable.
    spoons: float | None = None
    # The teaspoon span a sensible cook would stay within, as [min, max]. Shown
    # beside potent seasonings, where our container mass is an estimate and the
    # span matters more than the midpoint.
    spoon_range: list[float] | None = None
    # True when amount_g was derived by us from a count/container rather than
    # stated by the source. The UI must not lead with an estimated weight.
    amount_g_estimated: bool = False
    # How much a wrong quantity would hurt the dish: high | normal | forgiving.
    potency: str = "normal"


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

    cuisines: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(default_factory=list)
    ingredients: list[IngredientOut] = Field(default_factory=list)
    steps: list[StepOut] = Field(default_factory=list)
    nutrition: list[NutritionOut] = Field(default_factory=list)

    # Audit state. `macros_suspect` is the computed heuristic; `flagged_suspicious`
    # is a person having asked for a second look. Kept apart on purpose.
    macros_suspect: bool = False
    flagged_suspicious: bool = False
    audited_at: datetime | None = None
    edits: list["RecipeEditOut"] = Field(default_factory=list)


class RecipeEditOut(BaseModel):
    """A corrected number, and the one it replaced."""

    field: str
    old_value: float | None = None
    new_value: float | None = None
    status: str
    source: str
    reason: str | None = None
    model: str | None = None
    created_at: datetime | None = None


class AuditFindingOut(BaseModel):
    field: str
    old_value: float | None = None
    new_value: float | None = None
    reason: str
    source: str


class AuditResultOut(BaseModel):
    recipe_id: int
    verdict: str
    used_llm: bool = False
    checked: list[str] = Field(default_factory=list)
    findings: list[AuditFindingOut] = Field(default_factory=list)
    # Missing or placeholder ingredient quantities, reported whatever the verdict.
    ingredient_gaps: list[str] = Field(default_factory=list)


class AuditJobOut(BaseModel):
    job_id: str
    recipe_id: int
    status: str
    error: str | None = None
    result: AuditResultOut | None = None


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


# --- Planner basket + suggestions ------------------------------------------

class PlannerSelectionIn(BaseModel):
    recipe_id: int
    portions: int = Field(ge=1, le=20)


class PlannerFiltersIn(BaseModel):
    q: str | None = None
    cuisine: list[str] = Field(default_factory=list)
    diet: list[str] = Field(default_factory=list)
    tag: list[str] = Field(default_factory=list)
    protein: list[str] = Field(default_factory=list)
    max_time: int | None = None
    min_protein: float | None = None
    min_protein_ratio: float | None = None
    max_kcal: float | None = None
    difficulty: int | None = None
    exclude: list[str] = Field(default_factory=list)


class BasketIn(BaseModel):
    selections: list[PlannerSelectionIn] = Field(default_factory=list)


class BasketPackChoiceOut(BaseModel):
    sku: str
    product_name: str
    pack_size_raw: str | None = None
    url: str | None = None
    capacity_g: float
    capacity_qty: float | None = None
    quantity_unit: str = "g"
    price: float
    count: int
    cost: float
    retailer: str
    external: bool = False


class BasketContributionOut(BaseModel):
    recipe_id: int
    recipe_name: str
    grams: float
    quantity: float | None = None
    quantity_unit: str = "g"


class BasketLineOut(BaseModel):
    key: str
    name: str
    need_g: float
    need_qty: float | None = None
    quantity_unit: str = "g"
    capacity_g: float | None = None
    capacity_qty: float | None = None
    leftover_g: float | None = None
    leftover_qty: float | None = None
    cost: float
    waste_gbp: float
    score: float
    packs: int
    trace: bool = False
    external: bool = False
    note: str | None = None
    choices: list[BasketPackChoiceOut] = Field(default_factory=list)
    contributions: list[BasketContributionOut] = Field(default_factory=list)


class BasketOut(BaseModel):
    lines: list[BasketLineOut] = Field(default_factory=list)
    staples: list[str] = Field(default_factory=list)
    unmapped: list[str] = Field(default_factory=list)
    unpriceable: list[str] = Field(default_factory=list)
    untracked_lines: int = 0
    cost: float
    waste_gbp: float
    score: float


class SuggestionsIn(BasketIn):
    candidate_portions: int = Field(default=4, ge=1, le=20)
    filters: PlannerFiltersIn = Field(default_factory=PlannerFiltersIn)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=24, ge=1, le=60)
    offset: int | None = Field(default=None, ge=0)


class RecipeSuggestionCard(RecipeCard):
    marginal_score: float | None = None
    standalone_score: float | None = None
    ranking_score: float | None = None
    marginal_cost: float | None = None
    standalone_cost: float | None = None
    unpriced_gap_count: int = 0
    shared_ingredient_count: int
    basket_available: bool = True


class PlannerSuggestionsOut(BaseModel):
    items: list[RecipeSuggestionCard]
    total: int
    page: int
    page_size: int
    has_more: bool
    next_offset: int | None = None


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
    top_product_rating: float | None = None
    top_product_ratings_count: int | None = None


class MappingListOut(BaseModel):
    items: list[MappingListItem]
    counts: dict[str, int]
    total: int = 0
    page: int = 1
    page_size: int = 100
    has_more: bool = False


class AliasOptionOut(BaseModel):
    ingredient_key: str
    name: str


class AliasOptionsOut(BaseModel):
    items: list[AliasOptionOut] = Field(default_factory=list)


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
    # 'ocado' or 'manual'; the review UI tabs on this.
    retailer: str = "ocado"
    # Decision overlay
    accepted: bool = False
    rank: int | None = None
    match_type: str | None = None
    reason: str | None = None


class MappingDetailOut(BaseModel):
    ingredient_key: str
    name: str
    ingredient_icon_url: str | None = None
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
    usage: dict = Field(default_factory=dict)
    example_recipes: list[RecipeCard] = Field(default_factory=list)
    candidates: list[MappingCandidateOut] = Field(default_factory=list)


class AcceptedIn(BaseModel):
    sku: str
    rank: int = 1
    match_type: str = "exact"
    reason: str | None = None


class DecisionIn(BaseModel):
    status: str
    accepted: list[AcceptedIn] = Field(default_factory=list)
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
    items: list[AliasOut] = Field(default_factory=list)


class GenerateIn(BaseModel):
    count: int = 10


class MappingStatsOut(BaseModel):
    # Coverage measured by ingredient *uses* across the curated library, which is
    # what actually matters: resolving one common ingredient beats ten rare ones.
    lines_total: int = 0
    lines_resolved: int = 0
    lines_pct: float = 0.0
    distinct_keys: int = 0
    resolved_keys: int = 0
    mappings_total: int = 0
    approved: int = 0
    remaining_to_add: int = 0


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
    keys: list[str] = Field(default_factory=list)


class ManualProductIn(BaseModel):
    """A product sourced by hand, for an ingredient no retailer sells."""

    name: str
    price: float
    pack_size_value: float
    pack_size_unit: str = "g"
    brand: str | None = None
    # Left unset, the model assumes it keeps — see manual.DEFAULT_SHELF_LIFE_DAYS.
    shelf_life_days: int | None = None
    source_note: str | None = None
    url: str | None = None


class ManualResolveIn(ManualProductIn):
    """Create the product and approve it as this ingredient's mapping in one go."""

    match_type: str = "exact"
    each_to_grams: float | None = None
    reviewer_notes: str | None = None


class ManualProductUsageOut(BaseModel):
    ingredient_key: str
    name: str


class ManualProductOut(BaseModel):
    sku: str
    name: str
    brand: str | None = None
    pack_size_raw: str | None = None
    pack_size_value: float | None = None
    pack_size_unit: str | None = None
    price: float | None = None
    shelf_life_days: int | None = None
    source_note: str | None = None
    url: str | None = None
    used_by: list[ManualProductUsageOut] = Field(default_factory=list)


class ManualProductListOut(BaseModel):
    items: list[ManualProductOut] = Field(default_factory=list)
