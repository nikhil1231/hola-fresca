"""SQLAlchemy models for the recipe library and scrape bookkeeping.

Only the scraper's slice of the schema lives here for now: canonical recipes
plus the tables that record the state of the scrape pipeline. The planner,
pantry and basket domains will add their own tables later. Where a future
phase will need a foreign key that does not exist yet (canonical ingredient
resolution, in particular), the column is present but nullable.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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

    # Source quality/recency signals, used for library curation and as inputs
    # to the planner (popularity, freshness). ``is_addon`` marks non-standalone
    # items (extra protein, side veg) rather than full meals.
    avg_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    ratings_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    favorites_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_addon: Mapped[bool] = mapped_column(Integer, default=0)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Curation flag: the active library the app/planner uses. Set by the
    # ``curate`` command; all recipes are retained regardless so curation can be
    # re-run with different rules.
    curated: Mapped[bool] = mapped_column(Integer, default=0, index=True)

    scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    ingredients: Mapped[list["RecipeIngredient"]] = relationship(
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


class RecipeStep(Base):
    __tablename__ = "recipe_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    index: Mapped[int] = mapped_column(Integer)
    instructions_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions_html: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    in_stock: Mapped[bool | None] = mapped_column(Integer, nullable=True)

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
    # True when no candidate is a direct match and a substitution/composite is
    # needed (e.g. stock paste -> stock pot).
    needs_substitution: Mapped[bool] = mapped_column(Integer, default=0)
    # line_count x representative price; orders the review queue by spend impact.
    spend_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)

    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(16), nullable=True)  # llm | human

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    products: Mapped[list["IngredientMappingProduct"]] = relationship(
        back_populates="mapping", cascade="all, delete-orphan"
    )


class IngredientMappingProduct(Base):
    """A candidate product for an ingredient mapping, with the accept decision."""

    __tablename__ = "ingredient_mapping_products"

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
