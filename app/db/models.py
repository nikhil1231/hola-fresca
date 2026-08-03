"""SQLAlchemy models for the recipe library and scrape bookkeeping.

Only the scraper's slice of the schema lives here for now: canonical recipes
plus the tables that record the state of the scrape pipeline. The planner,
pantry and basket domains will add their own tables later. Where a future
phase will need a foreign key that does not exist yet (canonical ingredient
resolution, in particular), the column is present but nullable.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScrapeState(Base):
    """One row per recipe URL discovered from a source.

    Tracks the recipe through discover -> fetch -> normalize so the pipeline is
    restartable and incremental. ``source_id`` is the id parsed from the
    discovered URL; the payload's own id may differ after a redirect and is
    recorded separately on :class:`Recipe`.
    """

    __tablename__ = "scrape_state"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_scrape_source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(Text)

    # discovered -> fetched -> normalized, plus terminal states error / empty.
    status: Mapped[str] = mapped_column(String(32), default="discovered", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Recipe(Base):
    __tablename__ = "recipes"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_recipe_source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(Text)

    name: Mapped[str] = mapped_column(Text)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prep_time_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_time_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    serving_size_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_yield: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Denormalised per-portion macros for fast filtering by the planner. The
    # full nutrition breakdown lives in ``nutrition``.
    energy_kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    carbs_g: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Protein density (g protein per 100 kcal), a first-class browse/plan metric.
    protein_energy_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True when the stated macros don't reconcile with the stated energy (Atwater
    # check) — a source data error; excluded from the curated library.
    macros_suspect: Mapped[bool] = mapped_column(Integer, default=0, index=True)

    # Derived dietary suitability, computed from ingredients + allergens + macros
    # (the source's own tags are incomplete). These back the diet filters.
    is_vegetarian: Mapped[bool] = mapped_column(Integer, default=0, index=True)
    is_pescatarian: Mapped[bool] = mapped_column(Integer, default=0, index=True)
    is_dairy_free: Mapped[bool] = mapped_column(Integer, default=0, index=True)
    is_gluten_free: Mapped[bool] = mapped_column(Integer, default=0, index=True)
    is_low_carb: Mapped[bool] = mapped_column(Integer, default=0, index=True)

    # A recipe is "complete" when it has ingredients, steps and nutrition.
    # Deprecated stub recipes from the source are stored but flagged False.
    is_complete: Mapped[bool] = mapped_column(Integer, default=0, index=True)

    # main | side | dessert | product. The library carries accompaniments and
    # bought items alongside dinners; they are worth keeping and worth telling
    # apart, since a £2 garlic bread otherwise outranks every meal on price.
    # Derived — see app.classify.course.
    course: Mapped[str] = mapped_column(String(16), default="main", index=True)

    # Source quality/recency signals, used for library curation and as inputs
    # to the planner (popularity, freshness). ``is_addon`` marks non-standalone
    # items (extra protein, side veg) rather than full meals.
    avg_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    ratings_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    favorites_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_addon: Mapped[bool] = mapped_column(Integer, default=0)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # The same dish's rating across every revision of it — what the source's own
    # page shows. ``avg_rating``/``ratings_count`` above count only this exact
    # revision, and sit at zero for versions that never ran long on the menu.
    aggregate_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    aggregate_ratings_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Whichever of the two is the broader sample; the number the app displays and
    # curation judges by. See app.classify.effective_ratings.
    effective_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective_ratings_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )

    # Revision identity. ``family_code`` is shared by every version of a dish and
    # is the key curation deduplicates on; ``source_active`` marks the version
    # the source currently serves, which decides which one survives.
    unique_recipe_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    family_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cloned_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_active: Mapped[bool] = mapped_column(Integer, default=0, index=True)
    source_published: Mapped[bool] = mapped_column(Integer, default=0)

    # Curation flag: the active library the app/planner uses. Set by the
    # ``curate`` command; all recipes are retained regardless so curation can be
    # re-run with different rules.
    curated: Mapped[bool] = mapped_column(Integer, default=0, index=True)

    # Manual override for broken source rows that survive automated curation.
    # Kept separate from ``curated`` because curation is regenerated from source
    # data, while this records a local human decision.
    manually_excluded: Mapped[bool] = mapped_column(Integer, default=0, index=True)

    # Raised by hand from the recipe page ("these macros look wrong"), distinct
    # from the computed ``macros_suspect`` heuristic: this one records that a
    # person asked for a second look, and survives the audit that answers it.
    flagged_suspicious: Mapped[bool] = mapped_column(Integer, default=0, index=True)
    audited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    ingredients: Mapped[list["RecipeIngredient"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    edits: Mapped[list["RecipeEdit"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    steps: Mapped[list["RecipeStep"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    nutrition: Mapped[list["RecipeNutrition"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    tags: Mapped[list["RecipeTag"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    cuisines: Mapped[list["RecipeCuisine"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    allergens: Mapped[list["RecipeAllergen"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    personal_rating: Mapped["PersonalRecipeRating | None"] = relationship(
        back_populates="recipe", cascade="all, delete-orphan", uselist=False
    )
    wishlist_entry: Mapped["PersonalRecipeWishlist | None"] = relationship(
        back_populates="recipe", cascade="all, delete-orphan", uselist=False
    )


class PersonalRecipeRating(Base):
    __tablename__ = "personal_recipe_ratings"
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_personal_rating_range"),
    )

    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"), primary_key=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    recipe: Mapped[Recipe] = relationship(back_populates="personal_rating")


class PersonalRecipeWishlist(Base):
    __tablename__ = "personal_recipe_wishlist"

    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    recipe: Mapped[Recipe] = relationship(back_populates="wishlist_entry")


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )

    # Source-native ingredient identity. Amounts are for the recipe's base
    # (lowest) yield; larger yields are recomputed by the planner.
    source_ingredient_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 1-based source order, used for deterministic recipe display and scaling.
    position: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    type: Mapped[str | None] = mapped_column(Text, nullable=True)
    slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Canonical quantity in a metric unit, from the gram-conversion stack. Null
    # when the ingredient's unit could not be resolved to grams/ml.
    amount_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    canonical_unit: Mapped[str | None] = mapped_column(String(4), nullable=True)  # 'g' | 'ml'

    # Reserved for the canonicalisation phase; unused by the scraper.
    canonical_ingredient_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    recipe: Mapped[Recipe] = relationship(back_populates="ingredients")


class RecipeEdit(Base):
    """One corrected value on a recipe, with the value it replaced.

    The source's own numbers are sometimes wrong (macros that fail Atwater, an
    ingredient amount that reads as 2 g of noodles), and an audit pass fixes them.
    Each correction is recorded here rather than only being applied, so nothing is
    ever lost: ``old_value`` on the earliest applied edit for a field is the
    pristine source value, which is what :func:`app.audit.revert_recipe` restores.

    Applied edits are also projected onto the ``recipes`` row. That is deliberate.
    Leaving the row untouched and merging on read would mean every consumer — the
    browse filters, the facet counts, the planner's macro scoring — had to
    remember to merge, and any that forgot would silently keep using the bad
    number. Here the row is a cache of "source + applied edits" and this table is
    the audit trail.

    ``field`` is a recipe column ('energy_kcal') or an ingredient-scoped path
    ('ingredient:<id>.amount_g'). All audited values are numeric.
    """

    __tablename__ = "recipe_edits"
    __table_args__ = (
        CheckConstraint(
            "status in ('applied', 'proposed', 'rejected', 'reverted')",
            name="ck_recipe_edit_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    field: Mapped[str] = mapped_column(String(64), index=True)
    old_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_value: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="applied", index=True)
    source: Mapped[str] = mapped_column(String(16), default="llm")  # llm | human | check
    # Which check or model produced it, and why — shown in the UI next to the
    # corrected number so a surprising figure can be traced.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    recipe: Mapped[Recipe] = relationship(back_populates="edits")


class RecipeStep(Base):
    __tablename__ = "recipe_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    index: Mapped[int] = mapped_column(Integer)
    instructions_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    recipe: Mapped[Recipe] = relationship(back_populates="steps")


class RecipeNutrition(Base):
    __tablename__ = "recipe_nutrition"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)

    recipe: Mapped[Recipe] = relationship(back_populates="nutrition")


class RecipeTag(Base):
    __tablename__ = "recipe_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(Text, nullable=True)
    slug: Mapped[str | None] = mapped_column(Text, nullable=True)

    recipe: Mapped[Recipe] = relationship(back_populates="tags")


class RecipeCuisine(Base):
    __tablename__ = "recipe_cuisines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)

    recipe: Mapped[Recipe] = relationship(back_populates="cuisines")


class RecipeAllergen(Base):
    __tablename__ = "recipe_allergens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(Text, nullable=True)

    recipe: Mapped[Recipe] = relationship(back_populates="allergens")


class ProductScrapeState(Base):
    """Restartable scrape bookkeeping for retailer product caches."""

    __tablename__ = "product_scrape_state"
    __table_args__ = (
        UniqueConstraint("retailer", "kind", "key", name="uq_product_scrape_retailer_kind_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    retailer: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # search | product
    key: Mapped[str] = mapped_column(String(256), index=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="discovered", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Product(Base):
    """Retailer grocery product candidate for recipe-ingredient mapping."""

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("retailer", "sku", name="uq_product_retailer_sku"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    retailer: Mapped[str] = mapped_column(String(64), index=True)
    sku: Mapped[str] = mapped_column(String(128), index=True)

    name: Mapped[str] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(Text, nullable=True)
    pack_size_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    pack_size_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    pack_size_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)

    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)

    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stock is a cache of what the retailer said, so it is only as good as its
    # timestamp: NULL means never checked live, and the planner treats an old
    # reading as a reason to re-ask rather than as fact. See app.ocado.availability.
    in_stock: Mapped[bool | None] = mapped_column(Integer, nullable=True)
    stock_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Ocado's guaranteed minimum life on delivery, kept both verbatim ("2 WEEK")
    # and as days for ordering/filtering. NULL means the retailer states no life
    # for this product — typically ambient or non-food — not a short life.
    shelf_life_raw: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shelf_life_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Customer rating signals (from the retailer payload); a tie-break between
    # comparable products and a junk filter during ingredient mapping.
    avg_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    ratings_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    search_hits: Mapped[list["ProductSearchHit"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductSearchHit(Base):
    """Links an ingredient worklist term to every product candidate returned."""

    __tablename__ = "product_search_hits"
    __table_args__ = (
        UniqueConstraint("retailer", "ingredient_key", "sku", name="uq_product_hit_term_sku"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=True, index=True
    )
    retailer: Mapped[str] = mapped_column(String(64), index=True)
    ingredient_key: Mapped[str] = mapped_column(Text, index=True)
    search_term: Mapped[str] = mapped_column(Text)
    term_rank: Mapped[int] = mapped_column(Integer)
    line_count: Mapped[int] = mapped_column(Integer)
    sku: Mapped[str] = mapped_column(String(128), index=True)
    result_rank: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    product: Mapped[Product | None] = relationship(back_populates="search_hits")


class IngredientMapping(Base):
    """A canonical recipe ingredient resolved to acceptable retailer products.

    One row per ``ingredient_key`` (the merged group from the frequency
    analysis). Fixes product *identity*, not the pack chosen for a given week —
    that is the planner's job. Populated as ``proposed`` by the offline LLM pass
    and moved to ``approved`` by the human review UI; nothing downstream trusts a
    mapping until it is approved, so the proposal pass is safe to re-run.
    """

    __tablename__ = "ingredient_mappings"
    __table_args__ = (
        UniqueConstraint("retailer", "ingredient_key", name="uq_ingredient_map_retailer_key"),
        CheckConstraint(
            "status in ('proposed', 'approved', 'rejected', 'needs_review', 'no_match', 'alias')",
            name="ck_ingredient_mapping_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    retailer: Mapped[str] = mapped_column(String(64), index=True)
    ingredient_key: Mapped[str] = mapped_column(Text, index=True)
    name: Mapped[str] = mapped_column(Text)
    line_count: Mapped[int] = mapped_column(Integer, default=0)

    # proposed -> approved | rejected | needs_review | no_match
    status: Mapped[str] = mapped_column(String(32), default="proposed", index=True)

    # Grams per single unit, for ingredients the retailer sells by count
    # (e.g. 1 lime ~= 67 g). Null when the ingredient is sold/used by weight.
    each_to_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Planner quantity space: mass/volume continue through grams; count is
    # covered as whole units and converted to grams only for display/context.
    unit_kind: Mapped[str] = mapped_column(String(16), default="mass")
    # True when no candidate is a direct match and a substitution/composite is
    # needed (e.g. stock paste -> stock pot).
    needs_substitution: Mapped[bool] = mapped_column(Integer, default=0)
    # A cupboard staple assumed already owned (salt, oil, sugar). The mapping is
    # still real and approved — identity is worth recording — but the basket
    # builder skips it by default rather than buying it every week. Pantry
    # tracking will later decide when a staple actually needs restocking.
    pantry_staple: Mapped[bool] = mapped_column(Integer, default=0, index=True)
    # The retailer search term that produced the current candidate pool. Seeded
    # from the recipe ingredient name, overridable by the reviewer when better
    # wording finds better products ("vegetable stock" vs "vegetable stock paste").
    search_term: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when this ingredient is just another name for a different one ("Fresh
    # Pesto" -> "Basil Pesto"): it inherits that mapping's products, and the
    # basket sums their demand together instead of buying the same thing twice.
    # Always points at a root (never another alias); status becomes 'alias'.
    alias_of: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    # line_count x representative price; orders the review queue by spend impact.
    spend_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    # A standing "always buy this size" decision, e.g. the 1 kg bag of rice
    # rather than the 500 g one. The planner minimises this week's spend, which
    # is right for a lettuce and wrong for a cupboard staple that keeps and comes
    # back every week — the cheaper £/kg only pays off across weeks the planner
    # cannot see. So the trade-off is offered rather than guessed, and recorded
    # here once made. Ignored while the product is out of stock.
    preferred_sku: Mapped[str | None] = mapped_column(String(128), nullable=True)

    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(16), nullable=True)  # llm | human

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    products: Mapped[list["IngredientMappingProduct"]] = relationship(
        back_populates="mapping", cascade="all, delete-orphan"
    )


class OcadoCartSync(Base):
    """One row, recording that a sync has happened at all.

    Not redundant with an empty :class:`OcadoCartLedger`: no ledger lines means
    "HF owns nothing in the cart", which is true after a checkout empties it and
    false before the first sync ever ran. Only the second case may assume the
    packs already sitting in the cart are its own from a pre-ledger push.
    """

    __tablename__ = "ocado_cart_sync"
    __table_args__ = (UniqueConstraint("account_id", name="uq_ocado_cart_sync_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    week_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class OcadoCartLedger(Base):
    """What the last sync put in the Ocado cart, one row per product.

    The cart is shared with the rest of the week's shopping, so a sync has to
    know its own contributions from yours - see :mod:`app.ocado.sync`. Rows are
    written from the cart as re-read after the push, never from what was asked
    for, so a refusal or a partial fill cannot leave the ledger over-claiming.

    ``ingredient_name`` is carried purely so a removal can be reported in terms
    that mean something: "Chorizo, which you dropped with the paella", not a
    bare product id.
    """

    __tablename__ = "ocado_cart_ledger"
    __table_args__ = (UniqueConstraint("account_id", "sku", name="uq_ocado_cart_ledger_account_sku"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    sku: Mapped[str] = mapped_column(String(128), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)

    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingredient_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ingredient_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    week_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PlanSettings(Base):
    """The shopping rhythm, as one row (single-user app, one household).

    Only the *shape* of the schedule lives here — how often a shop happens, when
    its recipe list has to be settled, and whether the whole thing is paused. The
    recipes chosen for a given week stay client-side with the rest of the plan;
    this table is what a future unattended job would need in order to know which
    week it is buying for and when it is too late to change it.
    """

    __tablename__ = "plan_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # How many weeks between shops. 1 = every week, 2 = fortnightly.
    cadence_weeks: Mapped[int] = mapped_column(Integer, default=1)
    # The week the cadence counts from, so a fortnightly rhythm keeps its phase
    # instead of re-basing on whatever week the settings were last opened in.
    # Always a week-start date (Monday).
    anchor_week_start: Mapped[str] = mapped_column(String(16))

    # The deadline for settling a week's recipes, as an offset back from the week
    # itself: 2 days before at 18:00 is "the Saturday evening before". Stored
    # relative rather than absolute so it applies to every week without a row.
    cutoff_days_before: Mapped[int] = mapped_column(Integer, default=2)
    cutoff_time: Mapped[str] = mapped_column(String(5), default="18:00")

    # Stops the whole schedule: no week is active, nothing is due.
    paused: Mapped[bool] = mapped_column(Integer, default=0)

    # How many upcoming shops the planner shows at once.
    horizon_weeks: Mapped[int] = mapped_column(Integer, default=6)
    # Targets for a single week, used by the UI rather than enforced here.
    recipes_per_week: Mapped[int] = mapped_column(Integer, default=5)
    default_portions: Mapped[int] = mapped_column(Integer, default=4)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class PlanWeek(Base):
    """A per-week departure from the rhythm — currently only "skip this one".

    Absent means "as the cadence says", so an untouched schedule stores nothing.
    """

    __tablename__ = "plan_weeks"
    __table_args__ = (UniqueConstraint("week_start", name="uq_plan_week_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_start: Mapped[str] = mapped_column(String(16), index=True)
    skipped: Mapped[bool] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class IngredientMappingProduct(Base):
    """A candidate product for an ingredient mapping, with the accept decision."""

    __tablename__ = "ingredient_mapping_products"
    __table_args__ = (
        UniqueConstraint("mapping_id", "sku", name="uq_mapping_product_sku"),
        UniqueConstraint("mapping_id", "rank", name="uq_mapping_product_rank"),
        CheckConstraint(
            "match_type in ('exact', 'substitute', 'form_differs')",
            name="ck_mapping_product_match_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("ingredient_mappings.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sku: Mapped[str] = mapped_column(String(128), index=True)

    rank: Mapped[int] = mapped_column(Integer, default=0)
    match_type: Mapped[str] = mapped_column(String(16), default="exact")  # exact|substitute|form_differs
    accepted: Mapped[bool] = mapped_column(Integer, default=0, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="llm")  # llm | human

    mapping: Mapped[IngredientMapping] = relationship(back_populates="products")
    product: Mapped[Product | None] = relationship()
