"""In-memory snapshot of everything the planner needs to price a week."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.canonicalize import is_mislabelled_weight
from app.db.models import (
    IngredientMapping,
    IngredientMappingProduct,
    Product,
    Recipe,
    RecipeIngredient,
)
from app.mapping.candidates import load_recipe_pct_index, load_source_id_index
from app.planner import waste as waste_mod
from app import protein as protein_mod
from app.protein import ProteinLine

log = logging.getLogger(__name__)

RETAILER = "ocado"
DEFAULT_STATUSES = ("approved",)
COUNT_UNITS = {"unit(s)", "unit", "units"}
COUNT_PACK_RE = re.compile(
    r"\b(?P<count>\d+(?:\.\d+)?)\s*(?:x\s*)?"
    r"(?P<word>fillets?|breasts?|buns?|rolls?|burgers?|patties|onions?|limes?|lemons?|"
    r"peppers?|tomatoes|items?|pieces?|pack)\b",
    re.I,
)


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
    retailer: str = RETAILER
    capacity_qty: float | None = None
    quantity_unit: str = "g"
    #: Whether the retailer will currently sell it. Out-of-stock packs are kept
    #: in the index rather than dropped, so a cover can say what it *would* have
    #: bought and price the substitution it had to make instead.
    available: bool = True
    stock_checked_at: datetime | None = None
    #: Customer rating and how many people gave it. Cheapest is not best when the
    #: cheap one is a two-star product, so this is a term in the pack choice.
    rating: float | None = None
    ratings_count: int | None = None
    #: Guaranteed minimum life on delivery. A bigger pack is only a longer supply
    #: until this runs out - four months of mozzarella is two weeks of mozzarella.
    shelf_life_days: int | None = None

    @property
    def cost_per_g(self) -> float:
        return self.price / self.capacity_g

    @property
    def cost_per_qty(self) -> float:
        return self.price / self.capacity_qty if self.capacity_qty else self.cost_per_g

    @property
    def external(self) -> bool:
        return self.retailer != RETAILER


@dataclass(frozen=True, slots=True)
class Ingredient:
    """A canonical ingredient and the pack sizes it may be bought in.

    Everything here is true of the ingredient for everybody, which is what makes
    the index shareable across users. A standing "always buy the kilo bag" choice
    used to live on this dataclass and no longer does — it is one person's
    preference, and baking it in would put it in a snapshot every user reads. It
    now arrives per request; see :func:`app.planner.basket.build_basket`.
    """

    key: str
    name: str
    pantry_staple: bool
    packs: tuple[Pack, ...]
    each_to_grams: float | None = None
    unit_kind: str = "mass"
    unpriceable_products: int = 0
    #: Share of the curated library this ingredient appears in. The planner never
    #: prices future weeks, so this is how it knows a big bag will get eaten.
    recipe_pct: float = 0.0

    @property
    def available_packs(self) -> tuple[Pack, ...]:
        return tuple(pack for pack in self.packs if pack.available)

    @property
    def shoppable(self) -> bool:
        return bool(self.available_packs)

    @property
    def sold_out(self) -> bool:
        """Mapped and priced, but nothing on the list can be bought today."""
        return bool(self.packs) and not self.available_packs


@dataclass(frozen=True, slots=True)
class Need:
    """A recipe's demand for one canonical ingredient, at the recipe's base yield."""

    key: str
    display_name: str
    grams: float
    units: float | None = None


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
    untracked_lines: int = 0
    #: Which of ``needs`` is the dish's protein, when one is recognised. Derived
    #: here rather than per request because it is fixed by the library and a
    #: modifier has to find it on every candidate the ranking prices.
    protein: ProteinLine | None = None

    @property
    def macros(self) -> protein_mod.Macros:
        """Per-portion macros, as the library states them."""
        return protein_mod.Macros(
            kcal=self.energy_kcal or 0.0,
            protein_g=self.protein_g or 0.0,
            fat_g=self.fat_g or 0.0,
            carbs_g=self.carbs_g or 0.0,
        )


@dataclass
class PlanIndex:
    """Everything the planner reads, resolved and immutable."""

    ingredients: dict[str, Ingredient] = field(default_factory=dict)
    recipes: dict[int, PlanRecipe] = field(default_factory=dict)
    statuses: tuple[str, ...] = DEFAULT_STATUSES
    # (ingredient, unit, rounded demand, this-week pack override)
    cover_cache: dict[tuple[str, str, int, str | None], object] = field(default_factory=dict)
    # (recipe, protein modifier) -> the needs that modifier produces. A pinned
    # week's modified recipes are re-priced once per ranked candidate, so this
    # keeps a swap from being resolved thousands of times over.
    needs_cache: dict[tuple[int, protein_mod.ProteinModifier], tuple[Need, ...]] = field(
        default_factory=dict
    )

    def ingredient(self, key: str) -> Ingredient | None:
        return self.ingredients.get(key)


@dataclass(frozen=True, slots=True)
class PlannerCatalogue:
    """Ingredient-side state shared by every targeted recipe view."""

    ingredients: dict[str, Ingredient]
    roots: dict[str, str]
    each_by_key: dict[str, float]
    unit_kind_by_key: dict[str, str]
    sid_index: dict[str, str]
    statuses: tuple[str, ...]


def resolve_protein(
    recipe: PlanRecipe, modifier: protein_mod.ProteinModifier | None
) -> protein_mod.Resolution | None:
    """What ``modifier`` does to ``recipe``, or None if it does nothing."""
    if modifier is None or modifier.empty or recipe.protein is None:
        return None
    return protein_mod.resolve(
        recipe.protein,
        modifier,
        base_yield=recipe.base_yield,
        recipe_macros=recipe.macros,
    )


def modified_needs(
    index: PlanIndex, recipe: PlanRecipe, modifier: protein_mod.ProteinModifier | None
) -> tuple[Need, ...]:
    """``recipe.needs`` with a protein modifier applied — the only place it lands.

    Applied here, over an already-built index, rather than baked into the index
    itself: the index is a process-wide cache shared by every request, and a
    modifier belongs to one week held in one browser. Demands are re-accumulated
    by key on the way out because a swap can land on an ingredient the recipe
    already uses, and two lines for the same key would be bought twice.
    """
    if modifier is None or modifier.empty or recipe.protein is None:
        return recipe.needs
    cache_key = (recipe.id, modifier)
    cached = index.needs_cache.get(cache_key)
    if cached is not None:
        return cached

    resolution = resolve_protein(recipe, modifier)
    if resolution is None or not resolution.changed:
        index.needs_cache[cache_key] = recipe.needs
        return recipe.needs

    companions = protein_mod.companion_swaps([n.key for n in recipe.needs], resolution)
    grams: dict[str, float] = defaultdict(float)
    units: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}

    for need in recipe.needs:
        key, name, need_g, need_u = need.key, need.display_name, need.grams, need.units
        if need.key == resolution.source.key:
            need_g = need.grams * resolution.factor
            if resolution.target_key and index.ingredient(resolution.target_key):
                key = resolution.target_key
                name = resolution.target_name or name
        elif need.key in companions and index.ingredient(companions[need.key]) is not None:
            key = companions[need.key]
            name = protein_mod.display_name(key)

        if key != need.key or need_g != need.grams:
            ingredient = index.ingredient(key)
            need_g, need_u = protein_mod.swapped_quantity(
                need_g,
                unit_kind=ingredient.unit_kind if ingredient else "mass",
                each_to_grams=ingredient.each_to_grams if ingredient else None,
            )
        grams[key] += need_g
        if need_u is not None:
            units[key] += need_u
        names.setdefault(key, name)

    result = tuple(
        Need(
            key=k,
            display_name=names.get(k, k),
            grams=round(g, 2),
            units=units[k] if units.get(k) else None,
        )
        for k, g in sorted(grams.items(), key=lambda kv: -kv[1])
    )
    index.needs_cache[cache_key] = result
    return result


def _alias_roots(rows: list[IngredientMapping]) -> dict[str, str]:
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


def _is_count_unit(unit: str | None) -> bool:
    return (unit or "").strip().lower() in COUNT_UNITS


def _snap_count(value: float) -> float:
    return round(value * 12) / 12


def _round_each(value: float) -> float:
    return round(value, 1) if value >= 10 else round(value, 2)


def _stable_each(ratios: list[float]) -> float | None:
    clean = [r for r in ratios if r > 0]
    if not clean:
        return None
    med = median(clean)
    if med <= 0:
        return None
    return _round_each(float(med))


# An ingredient is only shopped as whole units if the library really counts it.
# Rosemary carries five stray "unit(s)" lines against 245 "bunch(es)" ones, and on
# the strength of those five it was being covered by the sprig — so a count claim
# has to win the vote, not merely appear. The share does the discriminating; the
# line floor is only there so a single stray line cannot decide it alone, and is
# kept low because a genuinely countable ingredient can be rare (Celeriac and
# Lamb Shank are counted in every line they appear in, but appear four times).
_COUNT_MIN_LINES = 3
_COUNT_MIN_SHARE = 0.5


def _derive_count_metadata(
    session: Session, rows: list[IngredientMapping], roots: dict[str, str], sid_index: dict[str, str]
) -> int:
    """Set ``each_to_grams`` and ``unit_kind`` from how the library states amounts.

    Only curated recipes are consulted: they are the only ones the planner will
    ever put in a basket, and a classification driven by lines from abandoned 2013
    stubs is not evidence about anything that can be cooked.

    Returns the number of mapping rows it changed.
    """
    by_key = {row.ingredient_key: row for row in rows}
    ratios: dict[str, list[float]] = defaultdict(list)
    count_lines: dict[str, int] = defaultdict(int)
    total_lines: dict[str, int] = defaultdict(int)

    stmt = (
        select(RecipeIngredient)
        .join(Recipe, RecipeIngredient.recipe_id == Recipe.id)
        .where(Recipe.curated == 1, Recipe.manually_excluded == 0)
    )
    for line in session.scalars(stmt):
        raw_key = sid_index.get(line.source_ingredient_id or "")
        if not raw_key:
            continue
        root = roots.get(raw_key, raw_key)
        total_lines[root] += 1
        if not _is_count_unit(line.unit):
            continue
        # "200 unit(s)" is a mislabelled gram weight, not two hundred of anything:
        # to_grams passes such amounts straight through, which would otherwise
        # contribute a per-unit weight of exactly 1 g and drag the median with it
        # (Lamb Shank was deriving 1 g apiece this way). The same predicate the
        # repair pass uses, so a line it relabels is a line this skips.
        if is_mislabelled_weight(line.unit, line.amount):
            continue
        count_lines[root] += 1
        if not line.amount or line.amount <= 0 or not line.amount_g or line.amount_g <= 0:
            continue
        ratios[root].append(float(line.amount_g) / float(line.amount))

    changed = 0
    for row in rows:
        key = row.ingredient_key
        counted = count_lines.get(key, 0)
        total = total_lines.get(key, 0)
        each = _stable_each(ratios.get(key, []))
        is_count = (
            each is not None
            and counted >= _COUNT_MIN_LINES
            and total > 0
            and counted / total >= _COUNT_MIN_SHARE
        )
        # A per-unit weight is worth keeping even for a mass ingredient — it is how
        # an "each"-priced pack gets a capacity — so it is recorded whenever the
        # count lines support one, independently of the classification.
        if each is not None and row.each_to_grams is None:
            row.each_to_grams = each
            changed += 1
        kind = "count" if is_count else "mass"
        # Reassigned in both directions: the old code only ever promoted to count,
        # so a bad classification could never be undone by better data.
        if getattr(row, "unit_kind", None) != kind:
            row.unit_kind = kind
            changed += 1
    if changed:
        session.commit()
    return changed


def derive_count_metadata(
    session_factory: sessionmaker[Session],
    *,
    retailer: str = RETAILER,
    csv_path: Path | None = None,
) -> int:
    """Reclassify every mapped ingredient as counted or weighed, in the database.

    This is a build step, not part of serving a request. It reads every curated
    recipe line — tens of thousands of rows — and writes its verdict back onto the
    mapping rows, so ``load_index`` can simply believe ``unit_kind`` and
    ``each_to_grams``. Re-run it whenever new ingredients enter the mapping, an
    alias is changed, or the recipe library is re-normalised:

        python -m app.planner derive-counts
    """
    sid_index = load_source_id_index(csv_path)
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(IngredientMapping).where(IngredientMapping.retailer == retailer)
            )
        )
        return _derive_count_metadata(session, rows, _alias_roots(rows), sid_index)


def _pack_capacity_g(product: Product, each_to_grams: float | None) -> float | None:
    """Grams (or millilitres, taken 1:1) a single pack provides."""
    if not product.pack_size_value:
        return None
    if product.pack_size_unit in ("g", "ml"):
        return float(product.pack_size_value)
    if product.pack_size_unit == "each" and each_to_grams:
        return float(product.pack_size_value) * each_to_grams
    return None


def _explicit_pack_count(product: Product) -> float | None:
    text = " ".join(part for part in (product.pack_size_raw, product.name) if part)
    for match in COUNT_PACK_RE.finditer(text):
        word = match.group("word").lower()
        if word == "pack":
            continue
        count = float(match.group("count"))
        if count > 0:
            return count
    return None


def _pack_capacity_qty(
    product: Product, each_to_grams: float | None, unit_kind: str, capacity_g: float | None
) -> float | None:
    if unit_kind != "count":
        return capacity_g
    if product.pack_size_unit == "each" and product.pack_size_value:
        return float(product.pack_size_value)
    explicit = _explicit_pack_count(product)
    if explicit:
        return explicit
    if capacity_g and each_to_grams:
        return max(1.0, float(round(capacity_g / each_to_grams)))
    return None


def _build_pack(
    mp: IngredientMappingProduct, each_to_grams: float | None, unit_kind: str
) -> Pack | None:
    product = mp.product
    if product is None or product.price is None:
        return None
    capacity_g = _pack_capacity_g(product, each_to_grams)
    capacity_qty = _pack_capacity_qty(product, each_to_grams, unit_kind, capacity_g)
    if unit_kind == "count" and capacity_g is None and capacity_qty and each_to_grams:
        capacity_g = capacity_qty * each_to_grams
    if not capacity_g or capacity_g <= 0 or not capacity_qty or capacity_qty <= 0:
        return None
    return Pack(
        sku=mp.sku,
        product_name=product.name,
        capacity_g=capacity_g,
        capacity_qty=capacity_qty,
        quantity_unit="unit" if unit_kind == "count" else "g",
        price=float(product.price),
        salvage=waste_mod.salvage_fraction(product.shelf_life_days, product.category),
        rank=mp.rank,
        match_type=mp.match_type,
        pack_size_raw=product.pack_size_raw,
        url=product.url,
        retailer=product.retailer,
        # NULL means never checked, which is not the same as sold out - the
        # catalogue simply has nothing to say yet, so the pack stays buyable.
        available=product.in_stock != 0,
        stock_checked_at=product.stock_checked_at,
        rating=product.avg_rating,
        ratings_count=product.ratings_count,
        shelf_life_days=product.shelf_life_days,
    )


def _load_ingredients(
    session: Session,
    statuses: tuple[str, ...],
    retailer: str,
    recipe_pct: dict[str, float] | None = None,
) -> tuple[dict[str, Ingredient], dict[str, str], dict[str, float], dict[str, str]]:
    """Build the canonical ingredient table, alias roots, unit metadata.

    ``unit_kind`` and ``each_to_grams`` are read as given: deriving them is
    :func:`derive_count_metadata`'s job and runs as a build step, because it costs
    a scan of every curated recipe line and it writes.
    """
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
    each_by_key = {r.ingredient_key: r.each_to_grams for r in rows if r.each_to_grams}
    unit_kind_by_key = {r.ingredient_key: (r.unit_kind or "mass") for r in rows}

    # An alias's recipes are the root's recipes, so its share counts towards how
    # often the root is cooked - which is what decides whether bulk pays.
    pct_by_root: dict[str, float] = defaultdict(float)
    for key, pct in (recipe_pct or {}).items():
        pct_by_root[roots.get(key, key)] += pct

    ingredients: dict[str, Ingredient] = {}
    for row in rows:
        if row.ingredient_key != roots.get(row.ingredient_key):
            continue
        if row.status not in statuses:
            continue
        unit_kind = row.unit_kind or "mass"
        accepted = [mp for mp in row.products if mp.accepted]
        packs: list[Pack] = []
        unpriceable = 0
        for mp in accepted:
            pack = _build_pack(mp, row.each_to_grams, unit_kind)
            if pack is None:
                unpriceable += 1
            else:
                packs.append(pack)
        packs.sort(key=lambda p: (p.cost_per_qty, p.rank))
        if not packs and not row.pantry_staple and not unpriceable:
            continue
        ingredients[row.ingredient_key] = Ingredient(
            key=row.ingredient_key,
            name=row.name,
            pantry_staple=bool(row.pantry_staple),
            packs=tuple(packs),
            each_to_grams=row.each_to_grams,
            unit_kind=unit_kind,
            unpriceable_products=unpriceable,
            recipe_pct=min(100.0, pct_by_root.get(row.ingredient_key, 0.0)),
        )

    _ = by_key
    return ingredients, roots, each_by_key, unit_kind_by_key


def _load_recipes(
    session: Session,
    *,
    roots: dict[str, str],
    each_by_key: dict[str, float],
    unit_kind_by_key: dict[str, str],
    sid_index: dict[str, str],
    recipe_ids: list[int] | None,
    curated_only: bool,
) -> dict[int, PlanRecipe]:
    stmt = select(Recipe).options(selectinload(Recipe.ingredients))
    if recipe_ids is not None:
        stmt = stmt.where(Recipe.id.in_(recipe_ids))
    elif curated_only:
        stmt = stmt.where(Recipe.curated == 1, Recipe.manually_excluded == 0)

    recipes: dict[int, PlanRecipe] = {}
    for recipe in session.scalars(stmt).unique():
        grams: dict[str, float] = defaultdict(float)
        units: dict[str, float] = defaultdict(float)
        names: dict[str, str] = {}
        untracked = 0
        for line in recipe.ingredients:
            if line.amount is not None and line.amount <= 0:
                continue
            if line.amount_g is not None and line.amount_g <= 0:
                continue

            raw_key = sid_index.get(line.source_ingredient_id or "")
            if not raw_key:
                untracked += 1
                continue
            key = roots.get(raw_key, raw_key)
            unit_kind = unit_kind_by_key.get(key, "mass")
            each = each_by_key.get(raw_key) or each_by_key.get(key)
            amount_g = line.amount_g
            amount_units: float | None = None

            if unit_kind == "count":
                if line.amount and _is_count_unit(line.unit):
                    amount_units = _snap_count(float(line.amount))
                elif amount_g is not None and each:
                    amount_units = _snap_count(float(amount_g) / each)
                if amount_units is not None and each:
                    amount_g = amount_units * each

            if amount_g is None:
                if each and line.amount:
                    amount_g = each * line.amount
            if amount_g is None or amount_g <= 0:
                untracked += 1
                continue
            names.setdefault(key, line.name)
            grams[key] += amount_g
            if amount_units is not None:
                units[key] += amount_units
        resolved_needs = tuple(
            Need(
                key=k,
                display_name=names.get(k, k),
                grams=round(g, 2),
                units=units[k] if units.get(k) else None,
            )
            for k, g in sorted(grams.items(), key=lambda kv: -kv[1])
        )
        recipes[recipe.id] = PlanRecipe(
            id=recipe.id,
            name=recipe.name,
            base_yield=recipe.base_yield or 2,
            needs=resolved_needs,
            protein=protein_mod.find_protein_line(
                [(n.key, n.display_name, n.grams, n.units) for n in resolved_needs]
            ),
            total_time_min=recipe.total_time_min,
            avg_rating=(
                recipe.effective_rating
                if recipe.effective_rating is not None
                else recipe.avg_rating
            ),
            ratings_count=(
                recipe.effective_ratings_count
                if recipe.effective_ratings_count is not None
                else recipe.ratings_count
            ),
            energy_kcal=recipe.energy_kcal,
            protein_g=recipe.protein_g,
            fat_g=recipe.fat_g,
            carbs_g=recipe.carbs_g,
            untracked_lines=untracked,
        )
    return recipes


def load_catalogue(
    session_factory: sessionmaker[Session],
    *,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    retailer: str = RETAILER,
    csv_path: Path | None = None,
) -> PlannerCatalogue:
    """Load the ingredient/product half of the planner snapshot once."""
    sid_index = load_source_id_index(csv_path)
    recipe_pct = load_recipe_pct_index(csv_path)
    with session_factory() as session:
        ingredients, roots, each_by_key, unit_kind_by_key = _load_ingredients(
            session, statuses, retailer, recipe_pct
        )
    return PlannerCatalogue(
        ingredients=ingredients,
        roots=roots,
        each_by_key=each_by_key,
        unit_kind_by_key=unit_kind_by_key,
        sid_index=sid_index,
        statuses=statuses,
    )


def hydrate_recipes(
    session_factory: sessionmaker[Session],
    catalogue: PlannerCatalogue,
    *,
    recipe_ids: Collection[int] | None,
    curated_only: bool = True,
) -> dict[int, PlanRecipe]:
    """Hydrate requested recipe graphs against an already-loaded catalogue."""
    requested = None if recipe_ids is None else list(dict.fromkeys(recipe_ids))
    with session_factory() as session:
        return _load_recipes(
            session,
            roots=catalogue.roots,
            each_by_key=catalogue.each_by_key,
            unit_kind_by_key=catalogue.unit_kind_by_key,
            sid_index=catalogue.sid_index,
            recipe_ids=requested,
            curated_only=curated_only,
        )


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
    catalogue = load_catalogue(
        session_factory, statuses=statuses, retailer=retailer, csv_path=csv_path
    )
    recipes = hydrate_recipes(
        session_factory, catalogue, recipe_ids=recipe_ids, curated_only=curated_only
    )
    shoppable = sum(1 for i in catalogue.ingredients.values() if i.shoppable)
    log.info(
        "planner index: %d recipes, %d ingredients (%d shoppable, %d staples)",
        len(recipes),
        len(catalogue.ingredients),
        shoppable,
        sum(1 for i in catalogue.ingredients.values() if i.pantry_staple),
    )
    return PlanIndex(
        ingredients=catalogue.ingredients, recipes=recipes, statuses=statuses
    )
