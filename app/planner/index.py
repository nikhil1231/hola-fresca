"""In-memory snapshot of everything the planner needs to price a week.

The search asks "what would this basket cost, and what would it waste?" for
thousands of candidate recipe sets, so the database is read exactly once, up
front, into plain frozen dataclasses. After :func:`load_index` returns, basket
building touches no session and no I/O — which is what makes the search
affordable and what makes it trivially testable.

Loading also does the work that must not be repeated per query: resolving each
recipe line's source ingredient id to a canonical ingredient key, following
aliases to their root, summing duplicate lines within a recipe, and pre-computing
each pack's gram capacity and salvage fraction.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.db.models import (
    IngredientMapping,
    IngredientMappingProduct,
    Product,
    Recipe,
    RecipeIngredient,
)
from app.mapping.candidates import load_source_id_index
from app.planner import waste as waste_mod

log = logging.getLogger(__name__)

RETAILER = "ocado"
DEFAULT_STATUSES = ("approved",)


@dataclass(frozen=True, slots=True)
class Pack:
    """One buyable pack size for an ingredient, ready for covering arithmetic."""

    sku: str
    product_name: str
    capacity_g: float
    price: float
    salvage: float
    rank: int
    match_type: str
    pack_size_raw: str | None = None
    url: str | None = None
    # Who sells it. Anything other than the shop being planned for has to be
    # bought separately, so the basket lists it under its own heading.
    retailer: str = RETAILER

    @property
    def cost_per_g(self) -> float:
        return self.price / self.capacity_g

    @property
    def external(self) -> bool:
        return self.retailer != RETAILER


@dataclass(frozen=True, slots=True)
class Ingredient:
    """A canonical ingredient and the pack sizes it may be bought in."""

    key: str
    name: str
    pantry_staple: bool
    packs: tuple[Pack, ...]
    each_to_grams: float | None = None
    # Accepted products that could not be turned into a gram capacity (sold by
    # count with no each_to_grams, or an unparsed pack size). Kept as a count so
    # the basket can say "this is mapped but not priceable" instead of "unmapped".
    unpriceable_products: int = 0

    @property
    def shoppable(self) -> bool:
        return bool(self.packs)


@dataclass(frozen=True, slots=True)
class Need:
    """A recipe's demand for one canonical ingredient, at the recipe's base yield."""

    key: str
    display_name: str
    grams: float


@dataclass(frozen=True, slots=True)
class PlanRecipe:
    id: int
    name: str
    base_yield: int
    needs: tuple[Need, ...]
    total_time_min: int | None = None
    avg_rating: float | None = None
    ratings_count: int | None = None
    energy_kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None
    # Lines that contribute no demand: no canonical key, or no resolvable grams.
    # A recipe with many of these is priced optimistically, so the planner can
    # use this to prefer better-understood recipes.
    untracked_lines: int = 0


@dataclass
class PlanIndex:
    """Everything the planner reads, resolved and immutable."""

    ingredients: dict[str, Ingredient] = field(default_factory=dict)
    recipes: dict[int, PlanRecipe] = field(default_factory=dict)
    statuses: tuple[str, ...] = DEFAULT_STATUSES
    # Memo for the covering search, keyed by (ingredient key, bucketed grams).
    # Lives here because it is valid for exactly as long as the index is.
    cover_cache: dict[tuple[str, int], object] = field(default_factory=dict)

    def ingredient(self, key: str) -> Ingredient | None:
        return self.ingredients.get(key)


def _alias_roots(rows: list[IngredientMapping]) -> dict[str, str]:
    """Resolve every ingredient key to its canonical root, in one pass.

    ``service.resolve_alias`` does this with a query per key, which is fine for
    the review UI and far too slow inside a search loop.
    """
    alias_of = {r.ingredient_key: r.alias_of for r in rows if r.alias_of}
    roots: dict[str, str] = {}
    for key in (r.ingredient_key for r in rows):
        seen: set[str] = set()
        current = key
        while current in alias_of and current not in seen:
            seen.add(current)
            current = alias_of[current]
        roots[key] = current
    return roots


def _pack_capacity_g(
    product: Product, each_to_grams: float | None
) -> float | None:
    """Grams (or millilitres, taken 1:1) a single pack provides.

    Recipe demand is canonicalised to g or ml and the two are treated as
    interchangeable here: the ingredients sold by volume are water-like enough
    (stock, milk, passata) that a density correction would be false precision.
    """
    if not product.pack_size_value:
        return None
    if product.pack_size_unit in ("g", "ml"):
        return float(product.pack_size_value)
    if product.pack_size_unit == "each" and each_to_grams:
        return float(product.pack_size_value) * each_to_grams
    return None


def _build_pack(mp: IngredientMappingProduct, each_to_grams: float | None) -> Pack | None:
    product = mp.product
    if product is None or product.price is None:
        return None
    # An out-of-stock SKU cannot be shopped this week. Mappings are a living
    # table and Ocado delists things, so this is expected attrition, not an error.
    if product.in_stock == 0:
        return None
    capacity = _pack_capacity_g(product, each_to_grams)
    if not capacity or capacity <= 0:
        return None
    return Pack(
        sku=mp.sku,
        product_name=product.name,
        capacity_g=capacity,
        price=float(product.price),
        salvage=waste_mod.salvage_fraction(product.shelf_life_days, product.category),
        rank=mp.rank,
        match_type=mp.match_type,
        pack_size_raw=product.pack_size_raw,
        url=product.url,
        retailer=product.retailer,
    )


def _load_ingredients(
    session: Session, statuses: tuple[str, ...], retailer: str
) -> tuple[dict[str, Ingredient], dict[str, str], dict[str, float]]:
    """Build the canonical ingredient table, plus alias roots and per-key each_to_grams."""
    rows = (
        session.scalars(
            select(IngredientMapping)
            .where(IngredientMapping.retailer == retailer)
            .options(
                selectinload(IngredientMapping.products).selectinload(
                    IngredientMappingProduct.product
                )
            )
        )
        .unique()
        .all()
    )
    rows = list(rows)
    roots = _alias_roots(rows)
    by_key = {r.ingredient_key: r for r in rows}
    # each_to_grams belongs to the name the recipe used, not the canonical one:
    # "2 limes" and "2 lime halves" convert differently even when they shop the same.
    each_by_key = {r.ingredient_key: r.each_to_grams for r in rows if r.each_to_grams}

    ingredients: dict[str, Ingredient] = {}
    for row in rows:
        if row.ingredient_key != roots.get(row.ingredient_key):
            continue  # an alias; it contributes demand to its root, not a mapping
        if row.status not in statuses:
            continue
        accepted = [mp for mp in row.products if mp.accepted]
        packs: list[Pack] = []
        unpriceable = 0
        for mp in accepted:
            pack = _build_pack(mp, row.each_to_grams)
            if pack is None:
                unpriceable += 1
            else:
                packs.append(pack)
        # Cheapest per gram first: the covering search is order-insensitive, but
        # this makes the fallback "just take the first pack" a sane one.
        packs.sort(key=lambda p: (p.cost_per_g, p.rank))
        if not packs and not row.pantry_staple and not unpriceable:
            continue  # approved but nothing buyable: treat as unmapped downstream
        ingredients[row.ingredient_key] = Ingredient(
            key=row.ingredient_key,
            name=row.name,
            pantry_staple=bool(row.pantry_staple),
            packs=tuple(packs),
            each_to_grams=row.each_to_grams,
            unpriceable_products=unpriceable,
        )

    # Aliases of a staple are staples too, and aliases of an unmapped root stay
    # unmapped — both fall out of resolving to the root before lookup.
    _ = by_key
    return ingredients, roots, each_by_key


def _load_recipes(
    session: Session,
    *,
    roots: dict[str, str],
    each_by_key: dict[str, float],
    sid_index: dict[str, str],
    recipe_ids: list[int] | None,
    curated_only: bool,
) -> dict[int, PlanRecipe]:
    stmt = select(Recipe).options(selectinload(Recipe.ingredients))
    # An explicit list means exactly those recipes — including an empty one, for
    # callers that only want the ingredient side of the index.
    if recipe_ids is not None:
        stmt = stmt.where(Recipe.id.in_(recipe_ids))
    elif curated_only:
        stmt = stmt.where(Recipe.curated == 1)

    recipes: dict[int, PlanRecipe] = {}
    for recipe in session.scalars(stmt).unique():
        grams: dict[str, float] = defaultdict(float)
        names: dict[str, str] = {}
        untracked = 0
        for line in recipe.ingredients:
            # HelloFresh uses zero-amount rows both for no-quantity pantry items
            # and for reformulation leftovers. They are source-faithful rows, but
            # not demand: skip them before they can become unmapped, trace, or
            # unit-space basket lines.
            if line.amount is not None and line.amount <= 0:
                continue
            if line.amount_g is not None and line.amount_g <= 0:
                continue

            raw_key = sid_index.get(line.source_ingredient_id or "")
            if not raw_key:
                untracked += 1
                continue
            key = roots.get(raw_key, raw_key)
            amount_g = line.amount_g
            if amount_g is None:
                # Sold by count: convert with the grams-per-unit recorded against
                # the name this recipe used.
                each = each_by_key.get(raw_key)
                if each and line.amount:
                    amount_g = each * line.amount
            if amount_g is None or amount_g <= 0:
                untracked += 1
                continue
            names.setdefault(key, line.name)
            grams[key] += amount_g
        recipes[recipe.id] = PlanRecipe(
            id=recipe.id,
            name=recipe.name,
            base_yield=recipe.base_yield or 2,
            needs=tuple(
                Need(key=k, display_name=names.get(k, k), grams=round(g, 2))
                for k, g in sorted(grams.items(), key=lambda kv: -kv[1])
            ),
            total_time_min=recipe.total_time_min,
            avg_rating=recipe.avg_rating,
            ratings_count=recipe.ratings_count,
            energy_kcal=recipe.energy_kcal,
            protein_g=recipe.protein_g,
            fat_g=recipe.fat_g,
            carbs_g=recipe.carbs_g,
            untracked_lines=untracked,
        )
    return recipes


def load_index(
    session_factory: sessionmaker[Session],
    *,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    recipe_ids: list[int] | None = None,
    curated_only: bool = True,
    retailer: str = RETAILER,
    csv_path: Path | None = None,
) -> PlanIndex:
    """Read the mapping and recipe library into a self-contained planner index."""
    sid_index = load_source_id_index(csv_path)
    with session_factory() as session:
        ingredients, roots, each_by_key = _load_ingredients(session, statuses, retailer)
        recipes = _load_recipes(
            session,
            roots=roots,
            each_by_key=each_by_key,
            sid_index=sid_index,
            recipe_ids=recipe_ids,
            curated_only=curated_only,
        )
    shoppable = sum(1 for i in ingredients.values() if i.shoppable)
    log.info(
        "planner index: %d recipes, %d ingredients (%d shoppable, %d staples)",
        len(recipes),
        len(ingredients),
        shoppable,
        sum(1 for i in ingredients.values() if i.pantry_staple),
    )
    return PlanIndex(ingredients=ingredients, recipes=recipes, statuses=statuses)
