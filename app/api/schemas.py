"""Pydantic response models for the recipe API.

These are hand-built from ORM rows (rather than ``from_attributes``) because the
card/detail shapes flatten relationships and inject computed fields like the CDN
image URL.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app import protein as protein_mod


class PersonalRatingIn(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)


class WishlistIn(BaseModel):
    wishlisted: bool


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
    course: str = "main"
    personal_rating: int | None = None
    wishlisted: bool = False
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
    personal_rating: int | None = None
    wishlisted: bool = False

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

    # Present only when a line of this recipe is a protein we recognise; a
    # lentil dahl simply has nothing to swap.
    protein: "ProteinProfileOut | None" = None


# --- protein modifiers ------------------------------------------------------

class MacrosOut(BaseModel):
    kcal: float = 0
    protein_g: float = 0
    fat_g: float = 0
    carbs_g: float = 0

    @classmethod
    def of(cls, macros: protein_mod.Macros) -> "MacrosOut":
        rounded = macros.rounded()
        return cls(
            kcal=rounded.kcal,
            protein_g=rounded.protein_g,
            fat_g=rounded.fat_g,
            carbs_g=rounded.carbs_g,
        )


class ProteinTargetOut(BaseModel):
    """A protein this recipe can be swapped to, in the form it would arrive in."""

    id: str
    label: str
    ingredient_key: str
    ingredient_name: str
    cook_note: str
    per_100g: MacrosOut
    #: False when the retailer has nothing shoppable for it today. Still offered,
    #: because the recipe page is not the checkout and a sold-out swap is worth
    #: knowing about rather than silently missing.
    available: bool = True


class ProteinProfileOut(BaseModel):
    """The recipe's own protein, and what may replace it."""

    ingredient_key: str
    name: str
    label: str
    type: str
    form: str
    grams: float
    per_100g: MacrosOut
    targets: list[ProteinTargetOut] = Field(default_factory=list)


class ProteinModifierIn(BaseModel):
    """A swap, a scale, or a per-portion macro target — combinable.

    ``scale`` and a target are alternatives; when both arrive, ``scale`` wins,
    because it is the one the user set by hand.
    """

    swap_to: str | None = None
    scale: float | None = Field(default=None, ge=protein_mod.MIN_FACTOR, le=protein_mod.MAX_FACTOR)
    target_mode: Literal["protein_g", "energy_kcal"] | None = None
    target_value: float | None = Field(default=None, gt=0, le=2000)

    def to_domain(self) -> protein_mod.ProteinModifier:
        return protein_mod.ProteinModifier(
            swap_to=self.swap_to,
            scale=self.scale,
            target_mode=self.target_mode,
            target_value=self.target_value,
        )


class ProteinPreviewIn(ProteinModifierIn):
    pass


class ProteinPreviewOut(BaseModel):
    """The modified recipe, ready to render in place of the stored one.

    The whole ingredient list and every step come back rewritten rather than a
    diff, so the page renders a modified recipe exactly as it renders a plain
    one. Quantities stay at the recipe's base yield for the same reason: portion
    scaling is the client's, and a preview that had already applied it could not
    be re-scaled without compounding.
    """

    factor: float
    swapped: bool
    changed: bool
    swap_id: str | None = None
    swap_label: str | None = None
    cook_note: str | None = None

    protein_name: str
    protein_name_after: str | None = None
    grams_before: float
    grams_after: float

    macros_before: MacrosOut
    macros_after: MacrosOut

    ingredients: list[IngredientOut] = Field(default_factory=list)
    steps: list["StepOut"] = Field(default_factory=list)
    #: Diet flags recomputed over the modified ingredient list, and the ones that
    #: actually changed, phrased for display ("Now vegetarian").
    diet: dict[str, bool] = Field(default_factory=dict)
    diet_changes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


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
    courses: list[FacetCount]
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
    #: This week's protein swap/scale for the dish. Travels with the selection
    #: rather than being stored, so it expires with the week it belongs to.
    protein: ProteinModifierIn | None = None


class PlannerFiltersIn(BaseModel):
    q: str | None = None
    cuisine: list[str] = Field(default_factory=list)
    diet: list[str] = Field(default_factory=list)
    tag: list[str] = Field(default_factory=list)
    protein: list[str] = Field(default_factory=list)
    course: list[str] = Field(default_factory=list)
    max_time: int | None = None
    min_protein: float | None = None
    min_protein_ratio: float | None = None
    max_kcal: float | None = None
    difficulty: int | None = None
    rated: bool = False
    wishlisted: bool = False
    exclude: list[str] = Field(default_factory=list)


class BasketIn(BaseModel):
    selections: list[PlannerSelectionIn] = Field(default_factory=list)
    owned_item_keys: list[str] = Field(default_factory=list)
    account_id: str | None = None
    #: Which week this basket is for. Recorded against the cart ledger so a
    #: stale claim can be read as "that was last week's shop", and ignored
    #: everywhere else.
    week_start: str | None = None
    #: ``{ingredient_key: sku}`` chosen for this week only. Held by the client
    #: alongside the week itself, so it costs no write and expires with it.
    pack_overrides: dict[str, str] = Field(default_factory=dict)


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


class BasketPackOptionOut(BaseModel):
    """One size this ingredient could be bought in, priced against the current pick."""

    sku: str
    product_name: str
    pack_size_raw: str | None = None
    url: str | None = None
    count: int
    cost: float
    capacity: float
    leftover: float
    #: Per kilo (or per unit), which is the only figure that compares sizes.
    unit_cost: float
    cost_delta: float
    leftover_delta: float
    quantity_unit: str = "g"
    keeps: bool = False
    chosen: bool = False
    pinned: bool = False
    this_week: bool = False
    better_value: bool = False
    rating: float | None = None
    ratings_count: int | None = None
    #: Estimated from how often the library cooks this, so it reads as a scale
    #: ("months" against "years") rather than as a promise.
    weeks_of_supply: float | None = None
    #: Which clock ran out first: "expiry" or "consumption".
    supply_limited_by: str | None = None


class PackPreferenceIn(BaseModel):
    ingredient_key: str = Field(min_length=1)
    #: Null clears the standing choice and hands the size back to the planner.
    sku: str | None = None


class PackPreferenceOut(BaseModel):
    ingredient_key: str
    sku: str | None = None


class BasketSubstitutionOut(BaseModel):
    """What a sold-out product cost this line, in money and in match quality."""

    displaced: list[str] = Field(default_factory=list)
    displaced_skus: list[str] = Field(default_factory=list)
    baseline_cost: float
    cost_delta: float
    tier_changed: bool = False


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
    substitution: BasketSubstitutionOut | None = None
    options: list[BasketPackOptionOut] = Field(default_factory=list)
    choices: list[BasketPackChoiceOut] = Field(default_factory=list)
    contributions: list[BasketContributionOut] = Field(default_factory=list)


class BasketOut(BaseModel):
    lines: list[BasketLineOut] = Field(default_factory=list)
    staples: list[str] = Field(default_factory=list)
    unmapped: list[str] = Field(default_factory=list)
    unpriceable: list[str] = Field(default_factory=list)
    sold_out: list[str] = Field(default_factory=list)
    untracked_lines: int = 0
    cost: float
    waste_gbp: float
    score: float
    #: How stale the stock behind this basket is: the oldest live check among the
    #: products it buys, or null if any of them has never been checked.
    stock_checked_at: datetime | None = None


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


# --- Ocado basket/session ----------------------------------------------------

class OcadoAccountOut(BaseModel):
    id: str
    label: str
    email: str | None = None
    status: str


class OcadoAccountsOut(BaseModel):
    items: list[OcadoAccountOut] = Field(default_factory=list)
    default_account_id: str


class OcadoAccountIn(BaseModel):
    account_id: str | None = None


class OcadoLoginOut(BaseModel):
    account_id: str
    status: str
    #: Which rung of the auth ladder is running, for a caller polling /status
    #: while a slow login is in flight. "idle" when nothing is.
    stage: str = "idle"


class OcadoOtpIn(OcadoAccountIn):
    code: str = Field(min_length=1)


class PushLineOut(BaseModel):
    sku: str
    quantity: int
    name: str | None = None
    #: The ingredient this product was bought for - "Sesame seeds", not
    #: "Mitake Irigoma Shiro". A drop is only actionable in these terms.
    ingredient: str | None = None
    ingredient_key: str | None = None
    wanted: int | None = None
    got: int | None = None
    reason: str | None = None


class OcadoSwapOut(BaseModel):
    ingredient: str
    ingredient_key: str
    from_products: list[str] = Field(default_factory=list)
    to_products: list[str] = Field(default_factory=list)
    cost_delta: float = 0.0
    tier_changed: bool = False


class OcadoPushResultOut(BaseModel):
    applied: list[PushLineOut] = Field(default_factory=list)
    dropped: list[PushLineOut] = Field(default_factory=list)
    unmapped: list[str] = Field(default_factory=list)
    deltas: dict[str, int] = Field(default_factory=dict)
    #: Products in the cart the sync attributed to you and left alone.
    yours: list[PushLineOut] = Field(default_factory=list)
    #: HF items you had deleted or cut back, put back to what the week needs.
    #: Reported because a reduction is indistinguishable from a deletion, and
    #: overriding one silently is how a sync loses your trust.
    restored: list[PushLineOut] = Field(default_factory=list)
    #: HF items the week no longer needs, taken back out.
    removed: list[PushLineOut] = Field(default_factory=list)
    swaps: list[OcadoSwapOut] = Field(default_factory=list)
    sold_out: list[str] = Field(default_factory=list)
    stock_checked_at: datetime | None = None


class OcadoPushPlanOut(BaseModel):
    """What a push would do, without doing it."""

    added: list[PushLineOut] = Field(default_factory=list)
    removed: list[PushLineOut] = Field(default_factory=list)
    restored: list[PushLineOut] = Field(default_factory=list)
    yours: list[PushLineOut] = Field(default_factory=list)
    unmapped: list[str] = Field(default_factory=list)
    deltas: dict[str, int] = Field(default_factory=dict)
    #: False before the first sync, when products already in the cart that the
    #: week also wants are adopted rather than bought again.
    synced: bool = False
    #: When the cart was last synced, and which week for - so a plan that wants
    #: to remove half the cart can be read against "that was last week's shop".
    synced_at: datetime | None = None
    synced_week_start: str | None = None


class OcadoStockRefreshOut(BaseModel):
    checked_at: datetime
    checked: int = 0
    available: int = 0
    sold_out: list[str] = Field(default_factory=list)
    restocked: list[str] = Field(default_factory=list)
    repriced: list[str] = Field(default_factory=list)
    changed: int = 0


class OcadoBasketOut(BaseModel):
    raw: dict


class OcadoSlotOut(BaseModel):
    slot_id: str
    start: str | None = None
    end: str | None = None
    day: str | None = None
    available: bool = False
    eco: bool = False
    price: float | None = None
    raw: dict | None = None


class OcadoSlotsOut(BaseModel):
    items: list[OcadoSlotOut] = Field(default_factory=list)


class OcadoReserveIn(BaseModel):
    account_id: str | None = None
    slot_id: str
    ddid: str | None = None
    region: str | None = None


class OcadoReserveOut(BaseModel):
    raw: dict
