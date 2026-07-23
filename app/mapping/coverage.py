"""Acceptance checks: how much of the recipe library the mapping can price.

``coverage`` reports the share of curated-recipe ingredient lines that resolve to
a mapped product. ``basket`` is the first end-to-end proof: given a few recipes,
sum the grams per ingredient, cover each from its mapped products, and print an
itemised, priced shopping list with leftovers. Both accept ``--include-proposed``
so the pipeline can be exercised before human review.
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
)
from app.mapping import service
from app.mapping.candidates import load_source_id_index

RETAILER = "ocado"
DEFAULT_STATUSES = ("approved",)


def _mapped_keys(session: Session, statuses: tuple[str, ...]) -> set[str]:
    """Ingredient keys that no longer need shopping decisions.

    Three ways to qualify: the mapping has accepted products; it is a pantry
    staple (assumed owned, so it needs none); or it is an alias of something that
    itself qualifies.
    """
    resolved = {
        row[0]
        for row in session.execute(
            select(IngredientMapping.ingredient_key).where(
                IngredientMapping.retailer == RETAILER,
                IngredientMapping.status.in_(statuses),
                or_(IngredientMapping.products.any(), IngredientMapping.pantry_staple == 1),
            )
        )
    }
    alias_rows = session.execute(
        select(IngredientMapping.ingredient_key).where(
            IngredientMapping.retailer == RETAILER,
            IngredientMapping.alias_of.is_not(None),
        )
    ).all()
    for (key,) in alias_rows:
        if service.resolve_alias(session, key) in resolved:
            resolved.add(key)
    return resolved


@dataclass
class CoverageReport:
    lines_total: int = 0
    lines_resolved: int = 0
    distinct_keys: int = 0
    resolved_keys: int = 0
    top_unresolved: list[tuple[str, int]] = field(default_factory=list)

    @property
    def pct(self) -> float:
        return 100 * self.lines_resolved / self.lines_total if self.lines_total else 0.0


def coverage_report(
    session_factory: sessionmaker[Session],
    *,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    csv_path: Path | None = None,
) -> CoverageReport:
    sid_index = load_source_id_index(csv_path)
    report = CoverageReport()
    unresolved: dict[str, int] = defaultdict(int)

    with session_factory() as session:
        mapped = _mapped_keys(session, statuses)
        rows = session.execute(
            select(RecipeIngredient.source_ingredient_id)
            .join(Recipe, RecipeIngredient.recipe_id == Recipe.id)
            .where(Recipe.curated == 1)
        ).all()

    seen_keys: set[str] = set()
    for (sid,) in rows:
        report.lines_total += 1
        key = sid_index.get(sid or "")
        if key:
            seen_keys.add(key)
        if key and key in mapped:
            report.lines_resolved += 1
        else:
            unresolved[key or "(untracked)"] += 1

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


def _approved_mapping(
    session: Session, key: str, statuses: tuple[str, ...]
) -> IngredientMapping | None:
    return session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == RETAILER,
            IngredientMapping.ingredient_key == key,
            IngredientMapping.status.in_(statuses),
        )
    )


def _best_product(session: Session, key: str, statuses: tuple[str, ...]) -> IngredientMappingProduct | None:
    mapping = session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == RETAILER,
            IngredientMapping.ingredient_key == key,
            IngredientMapping.status.in_(statuses),
        )
    )
    if mapping is None or not mapping.products:
        return None
    return sorted(mapping.products, key=lambda p: p.rank)[0]


def build_basket(
    session_factory: sessionmaker[Session],
    recipe_ids: list[int],
    *,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    include_staples: bool = False,
    csv_path: Path | None = None,
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
                        IngredientMapping.retailer == RETAILER,
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
            mapping = _approved_mapping(session, key, statuses)
            if mapping is not None and mapping.pantry_staple and not include_staples:
                basket.staples.append(name_by_key.get(key, key))
                continue
            best = _best_product(session, key, statuses)
            if best is None:
                basket.unmapped.append(name_by_key.get(key, key))
                continue
            product = best.product or session.scalar(
                select(Product).where(Product.retailer == RETAILER, Product.sku == best.sku)
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
            if product.pack_size_unit in ("g", "ml") and product.pack_size_value:
                packs = max(1, math.ceil(grams / product.pack_size_value))
                line.packs = packs
                if product.price is not None:
                    line.line_cost = round(packs * product.price, 2)
                    basket.total += line.line_cost
                line.leftover_g = round(packs * product.pack_size_value - grams, 1)
            else:
                # Count-sold or unparsed pack: needs the planner's unit logic.
                line.note = f"pack '{product.pack_size_raw}' needs unit handling"
            basket.lines.append(line)
        basket.total = round(basket.total, 2)
        return basket
