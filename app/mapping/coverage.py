"""Acceptance checks: how much of the recipe library the mapping can price.

``coverage`` reports two things about the library. **Recipes priceable** is the
headline: a recipe counts only when *every* one of its ingredient lines resolves,
because one unmapped line is a hole in the basket and the recipe cannot be
shopped. **Lines resolved** is the work measure underneath it — it moves with
every mapping decision, where the recipe count only moves when a recipe's last
gap closes.

The two answer different questions and diverge sharply once the common
ingredients are done: the line share is dominated by the head of the frequency
distribution (a few dozen keys are half of all lines), so it saturates near 100%
while whole recipes are still held up by one rare ingredient each.

``basket`` is the end-to-end proof: given a few recipes, sum the grams per
ingredient, cover each from its mapped products, and print an itemised, priced
shopping list with leftovers. All of them accept ``--include-proposed`` so the
pipeline can be exercised before human review.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    IngredientMapping,
    IngredientMappingProduct,
    Product,
    Recipe,
    RecipeIngredient,
    in_library,
)
from app.mapping import service
from app.mapping.candidates import load_source_id_index
from app.retailers import DEFAULT_RETAILER

#: Default shop; every public function takes ``retailer`` and falls back to it.
RETAILER = DEFAULT_RETAILER
DEFAULT_STATUSES = ("approved",)


def _mapped_keys(session: Session, statuses: tuple[str, ...], retailer: str) -> set[str]:
    """Ingredient keys that no longer need shopping decisions.

    Three ways to qualify: the mapping has accepted products; it is a pantry
    staple (assumed owned, so it needs none); or it is an alias of something that
    itself qualifies.
    """
    resolved = {
        row[0]
        for row in session.execute(
            select(IngredientMapping.ingredient_key).where(
                IngredientMapping.retailer == retailer,
                IngredientMapping.status.in_(statuses),
                or_(IngredientMapping.products.any(), IngredientMapping.pantry_staple == 1),
            )
        )
    }
    alias_rows = session.execute(
        select(IngredientMapping.ingredient_key).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.alias_of.is_not(None),
        )
    ).all()
    for (key,) in alias_rows:
        if service.resolve_alias(session, key, retailer) in resolved:
            resolved.add(key)
    return resolved


@dataclass
class CoverageReport:
    lines_total: int = 0
    lines_resolved: int = 0
    distinct_keys: int = 0
    resolved_keys: int = 0
    #: Library recipes, and those with no unresolved ingredient line at all.
    recipes_total: int = 0
    recipes_priceable: int = 0
    top_unresolved: list[tuple[str, int]] = field(default_factory=list)

    @property
    def pct(self) -> float:
        return 100 * self.lines_resolved / self.lines_total if self.lines_total else 0.0

    @property
    def recipes_pct(self) -> float:
        return (
            100 * self.recipes_priceable / self.recipes_total if self.recipes_total else 0.0
        )


def coverage_report(
    session_factory: sessionmaker[Session],
    *,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    csv_path: Path | None = None,
    retailer: str = RETAILER,
) -> CoverageReport:
    sid_index = load_source_id_index(csv_path)
    report = CoverageReport()
    unresolved: dict[str, int] = defaultdict(int)

    with session_factory() as session:
        mapped = _mapped_keys(session, statuses, retailer)
        # The library, not ``curated`` — the planner prices exactly these rows,
        # so a recipe admitted by hand has to count here too.
        rows = session.execute(
            select(RecipeIngredient.recipe_id, RecipeIngredient.source_ingredient_id)
            .join(Recipe, RecipeIngredient.recipe_id == Recipe.id)
            .where(*in_library())
        ).all()

    seen_keys: set[str] = set()
    gaps_by_recipe: defaultdict[int, int] = defaultdict(int)
    for recipe_id, sid in rows:
        report.lines_total += 1
        gaps_by_recipe.setdefault(recipe_id, 0)
        key = sid_index.get(sid or "")
        if key:
            seen_keys.add(key)
        if key and key in mapped:
            report.lines_resolved += 1
        else:
            unresolved[key or "(untracked)"] += 1
            gaps_by_recipe[recipe_id] += 1

    report.recipes_total = len(gaps_by_recipe)
    report.recipes_priceable = sum(1 for gaps in gaps_by_recipe.values() if gaps == 0)
    report.distinct_keys = len(seen_keys)
    report.resolved_keys = len(seen_keys & mapped)
    report.top_unresolved = sorted(unresolved.items(), key=lambda kv: kv[1], reverse=True)[:15]
    return report


# --------------------------------------------------------------------------
# Basket demo
# --------------------------------------------------------------------------

@dataclass
class BasketLine:
    ingredient_key: str
    name: str
    need_g: float
    product_name: str | None = None
    pack_size_value: float | None = None
    pack_size_unit: str | None = None
    packs: int = 0
    price: float | None = None
    line_cost: float | None = None
    leftover_g: float | None = None
    note: str | None = None


@dataclass
class Basket:
    lines: list[BasketLine] = field(default_factory=list)
    total: float = 0.0
    unmapped: list[str] = field(default_factory=list)
    # Cupboard staples assumed already owned, omitted from the shopping list.
    staples: list[str] = field(default_factory=list)


def _best_product(
    session: Session, key: str, statuses: tuple[str, ...], retailer: str
) -> tuple[IngredientMapping | None, IngredientMappingProduct | None]:
    mapping = session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.ingredient_key == key,
            IngredientMapping.status.in_(statuses),
        )
    )
    if mapping is None or not mapping.products:
        return mapping, None
    return mapping, sorted(mapping.products, key=lambda p: p.rank)[0]


def _pack_capacity_g(product: Product, mapping: IngredientMapping | None) -> float | None:
    if product.pack_size_unit in ("g", "ml") and product.pack_size_value:
        return product.pack_size_value
    if product.pack_size_unit == "each" and product.pack_size_value and mapping and mapping.each_to_grams:
        return product.pack_size_value * mapping.each_to_grams
    return None


def build_basket(
    session_factory: sessionmaker[Session],
    recipe_ids: list[int],
    *,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    include_staples: bool = False,
    csv_path: Path | None = None,
    retailer: str = RETAILER,
) -> Basket:
    sid_index = load_source_id_index(csv_path)
    need_g: dict[str, float] = defaultdict(float)
    name_by_key: dict[str, str] = {}

    with session_factory() as session:
        for rid in recipe_ids:
            recipe = session.get(Recipe, rid)
            if recipe is None:
                continue
            for ing in recipe.ingredients:
                raw_key = sid_index.get(ing.source_ingredient_id or "")
                if not raw_key:
                    continue
                # Count aliased ingredients ("Fresh Pesto") against their
                # canonical ("Basil Pesto"), so demand for the same thing under
                # different names sums into one pack instead of buying twice.
                mapping = session.scalar(
                    select(IngredientMapping).where(
                        IngredientMapping.retailer == retailer,
                        IngredientMapping.ingredient_key == raw_key,
                    )
                )
                key = service.resolve_alias(session, raw_key) if mapping else raw_key
                name_by_key.setdefault(key, ing.name)
                grams = ing.amount_g
                if grams is None:
                    # Unit-sold items convert via the ingredient's own grams-per-unit,
                    # which belongs to the name the recipe used, not the canonical.
                    if mapping and mapping.each_to_grams and ing.amount:
                        grams = mapping.each_to_grams * ing.amount
                if grams:
                    need_g[key] += grams

        basket = Basket()
        for key, grams in sorted(need_g.items(), key=lambda kv: kv[1], reverse=True):
            # Staples (salt, oil, sugar) are mapped and approved, but assumed
            # already in the cupboard — record them, don't shop for them.
            mapping, best = _best_product(session, key, statuses, retailer)
            if mapping is not None and mapping.pantry_staple and not include_staples:
                basket.staples.append(name_by_key.get(key, key))
                continue
            if best is None:
                basket.unmapped.append(name_by_key.get(key, key))
                continue
            product = best.product or session.scalar(
                select(Product).where(Product.retailer == retailer, Product.sku == best.sku)
            )
            line = BasketLine(ingredient_key=key, name=name_by_key.get(key, key), need_g=round(grams, 1))
            if product is None:
                line.note = "product row missing"
                basket.lines.append(line)
                continue
            line.product_name = product.name
            line.pack_size_value = product.pack_size_value
            line.pack_size_unit = product.pack_size_unit
            line.price = product.price
            capacity_g = _pack_capacity_g(product, mapping)
            if capacity_g:
                packs = max(1, math.ceil(grams / capacity_g))
                line.packs = packs
                if product.price is not None:
                    line.line_cost = round(packs * product.price, 2)
                    basket.total += line.line_cost
                line.leftover_g = round(packs * capacity_g - grams, 1)
            else:
                line.note = f"pack '{product.pack_size_raw}' needs unit handling"
            basket.lines.append(line)
        basket.total = round(basket.total, 2)
        return basket
