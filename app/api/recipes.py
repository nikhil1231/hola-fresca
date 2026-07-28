"""Recipe browse API: list (filter/sort/paginate), detail, and facets.

Every endpoint is scoped to the curated active library (``Recipe.curated == 1``).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, func, nullslast, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.api import facets as facet_cfg
from app.api.deps import get_planner_csv_path, get_session, get_session_factory
from app.api.schemas import (
    AuditJobOut,
    FacetCount,
    FacetsOut,
    IngredientOut,
    NumericRange,
    NutritionOut,
    PaginatedRecipes,
    RecipeCard,
    RecipeDetail,
    RecipeEditOut,
    StepOut,
)
from app.db.models import (
    IngredientMapping,
    Recipe,
    RecipeAllergen,
    RecipeCuisine,
    RecipeIngredient,
    RecipeTag,
)
from app import measures
from app.mapping.candidates import load_source_id_index
from app.media import image_url
from app.planner.basket import Selection, basket_gap_count, build_basket
from app.planner.index import RETAILER, load_index


def _ingredient_match(keywords: list[str]):
    """A condition: the recipe has an ingredient whose name contains a keyword."""
    return Recipe.ingredients.any(
        or_(*[RecipeIngredient.name.ilike(f"%{k}%") for k in keywords])
    )

router = APIRouter(prefix="/api", tags=["recipes"])

CARD_WIDTH = 500
HERO_WIDTH = 1200
MAX_PAGE_SIZE = 60
INTRINSIC_PORTIONS = 4

# Attribute tag types that become display chips on a card, with friendly labels.
_CHIP_LABELS = dict(facet_cfg.ATTRIBUTE_TAGS)

_SORT_COLUMNS = {
    "popular": nullslast(Recipe.ratings_count.desc()),
    "rating": nullslast(Recipe.avg_rating.desc()),
    "protein_high": nullslast(Recipe.protein_g.desc()),
    "protein_ratio": nullslast(Recipe.protein_energy_ratio.desc()),
    "kcal_low": nullslast(Recipe.energy_kcal.asc()),
    "time_low": nullslast(Recipe.total_time_min.asc()),
    "newest": nullslast(Recipe.source_created_at.desc()),
}


def _apply_filters(
    stmt: Select,
    *,
    q: str | None,
    cuisine: list[str],
    diet: list[str],
    tag: list[str],
    protein: list[str],
    max_time: int | None,
    min_protein: float | None,
    min_protein_ratio: float | None,
    max_kcal: float | None,
    difficulty: int | None,
    exclude: list[str],
) -> Select:
    stmt = stmt.where(Recipe.curated == 1)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Recipe.name.ilike(like), Recipe.headline.ilike(like)))
    if cuisine:
        stmt = stmt.where(Recipe.cuisines.any(RecipeCuisine.name.in_(cuisine)))
    # Diet filters map to derived boolean columns; ANDed (each must hold).
    for value in diet:
        entry = facet_cfg.DIET_COLUMNS.get(value)
        if entry:
            stmt = stmt.where(getattr(Recipe, entry[0]) == 1)
    # Attribute filters are source tag-types, ANDed.
    for tag_type in tag:
        stmt = stmt.where(Recipe.tags.any(RecipeTag.type == tag_type))
    # Protein include: recipe contains ANY of the selected proteins (OR).
    protein_conds = [
        _ingredient_match(facet_cfg.INGREDIENT_KEYWORDS[p])
        for p in protein
        if p in facet_cfg.INGREDIENT_KEYWORDS
    ]
    if protein_conds:
        stmt = stmt.where(or_(*protein_conds))
    # Exclude: each value is either an ingredient group or an allergen name.
    for value in exclude:
        if value == "unmapped":
            continue
        keywords = facet_cfg.INGREDIENT_KEYWORDS.get(value.lower())
        if keywords:
            stmt = stmt.where(~_ingredient_match(keywords))
        else:
            stmt = stmt.where(~Recipe.allergens.any(RecipeAllergen.name == value))
    if max_time is not None:
        stmt = stmt.where(Recipe.total_time_min.is_not(None), Recipe.total_time_min <= max_time)
    if min_protein is not None:
        stmt = stmt.where(Recipe.protein_g.is_not(None), Recipe.protein_g >= min_protein)
    if min_protein_ratio is not None:
        stmt = stmt.where(
            Recipe.protein_energy_ratio.is_not(None),
            Recipe.protein_energy_ratio >= min_protein_ratio,
        )
    if max_kcal is not None:
        stmt = stmt.where(Recipe.energy_kcal.is_not(None), Recipe.energy_kcal <= max_kcal)
    if difficulty is not None:
        stmt = stmt.where(Recipe.difficulty == difficulty)
    return stmt


def _round_money(value: float) -> float:
    return round(value, 2)


def _to_card(
    r: Recipe,
    *,
    intrinsic_score: float | None = None,
    intrinsic_cost: float | None = None,
    intrinsic_gap_count: int = 0,
) -> RecipeCard:
    # A derived diet chip (most specific first) plus source attribute chips.
    chips: list[str] = []
    if r.is_vegetarian:
        chips.append("Vegetarian")
    elif r.is_pescatarian:
        chips.append("Pescatarian")
    chips += [_CHIP_LABELS[t.type] for t in r.tags if t.type in _CHIP_LABELS]
    return RecipeCard(
        id=r.id,
        name=r.name,
        headline=r.headline,
        image_url=image_url(r.image_path, CARD_WIDTH),
        energy_kcal=r.energy_kcal,
        protein_g=r.protein_g,
        protein_energy_ratio=r.protein_energy_ratio,
        total_time_min=r.total_time_min,
        difficulty=r.difficulty,
        avg_rating=r.avg_rating,
        ratings_count=r.ratings_count,
        cuisines=[facet_cfg.clean_cuisine(c.name) for c in r.cuisines],
        tags=list(dict.fromkeys(chips)),  # dedupe, preserve order
        intrinsic_score=intrinsic_score,
        intrinsic_cost=intrinsic_cost,
        intrinsic_gap_count=intrinsic_gap_count,
    )


def _intrinsic_prices(
    rows: list[Recipe] | list[int],
    factory: sessionmaker[Session],
    csv_path: Path | None,
) -> dict[int, tuple[float, float, int]]:
    recipe_ids = [recipe if isinstance(recipe, int) else recipe.id for recipe in rows]
    if not recipe_ids:
        return {}
    index = load_index(factory, recipe_ids=recipe_ids, curated_only=False, csv_path=csv_path)
    prices: dict[int, tuple[float, float, int]] = {}
    for recipe_id in recipe_ids:
        basket = build_basket(index, [Selection(recipe_id=recipe_id, servings=INTRINSIC_PORTIONS)])
        prices[recipe_id] = (
            _round_money(basket.score),
            _round_money(basket.consumed_cost),
            basket_gap_count(basket),
        )
    return prices


def _alias_roots(rows: list[IngredientMapping]) -> dict[str, str]:
    alias_of = {row.ingredient_key: row.alias_of for row in rows if row.alias_of}
    roots: dict[str, str] = {}
    for row in rows:
        seen: set[str] = set()
        current = row.ingredient_key
        while current in alias_of and current not in seen:
            seen.add(current)
            current = alias_of[current]
        roots[row.ingredient_key] = current
    return roots


def _unmapped_ingredient_ids(
    session: Session,
    ingredients: list[RecipeIngredient],
    csv_path: Path | None,
) -> set[int]:
    sid_index = load_source_id_index(csv_path)
    mapping_rows = list(
        session.scalars(select(IngredientMapping).where(IngredientMapping.retailer == RETAILER))
    )
    roots = _alias_roots(mapping_rows)
    by_key = {row.ingredient_key: row for row in mapping_rows}
    unmapped: set[int] = set()
    for ingredient in ingredients:
        if not _has_display_quantity(ingredient):
            continue
        raw_key = sid_index.get(ingredient.source_ingredient_id or "")
        if raw_key is None:
            unmapped.add(ingredient.id)
            continue
        root = roots.get(raw_key, raw_key)
        row = by_key.get(root)
        if row is None or row.status != "approved":
            unmapped.add(ingredient.id)
    return unmapped


def _has_display_quantity(ingredient: RecipeIngredient) -> bool:
    # Keep the persisted source row intact, but do not present HelloFresh's
    # zero-amount placeholders as recipe ingredients.
    if ingredient.amount is not None and ingredient.amount <= 0:
        return False
    if ingredient.amount_g is not None and ingredient.amount_g <= 0:
        return False
    return ingredient.amount is not None or ingredient.amount_g is not None


def _unmapped_recipe_ids(session: Session, csv_path: Path | None) -> set[int]:
    sid_index = load_source_id_index(csv_path)
    mapping_rows = list(
        session.scalars(select(IngredientMapping).where(IngredientMapping.retailer == RETAILER))
    )
    roots = _alias_roots(mapping_rows)
    approved_roots = {
        row.ingredient_key for row in mapping_rows if row.status == "approved"
    }
    approved_source_ids = {
        source_id
        for source_id, key in sid_index.items()
        if roots.get(key, key) in approved_roots
    }
    return set(
        session.scalars(
            select(RecipeIngredient.recipe_id)
            .join(Recipe, RecipeIngredient.recipe_id == Recipe.id)
            .where(
                Recipe.curated == 1,
                or_(
                    RecipeIngredient.source_ingredient_id.is_(None),
                    RecipeIngredient.source_ingredient_id.not_in(approved_source_ids),
                ),
                or_(RecipeIngredient.amount.is_(None), RecipeIngredient.amount > 0),
                or_(RecipeIngredient.amount_g.is_(None), RecipeIngredient.amount_g > 0),
            )
            .distinct()
        )
    )


@router.get("/recipes", response_model=PaginatedRecipes)
def list_recipes(
    q: str | None = None,
    cuisine: list[str] = Query(default_factory=list),
    diet: list[str] = Query(default_factory=list),
    tag: list[str] = Query(default_factory=list),
    protein: list[str] = Query(default_factory=list),
    max_time: int | None = None,
    min_protein: float | None = None,
    min_protein_ratio: float | None = None,
    max_kcal: float | None = None,
    difficulty: int | None = None,
    exclude: list[str] = Query(default_factory=list),
    exclude_id: list[int] = Query(default_factory=list),
    sort: str = facet_cfg.DEFAULT_SORT,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=MAX_PAGE_SIZE),
    offset: int | None = Query(default=None, ge=0),
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
) -> PaginatedRecipes:
    filters = dict(
        q=q, cuisine=cuisine, diet=diet, tag=tag, protein=protein, max_time=max_time,
        min_protein=min_protein, min_protein_ratio=min_protein_ratio, max_kcal=max_kcal,
        difficulty=difficulty, exclude=exclude,
    )
    exclude_unmapped = "unmapped" in exclude
    unmapped_recipe_ids = _unmapped_recipe_ids(session, csv_path) if exclude_unmapped else set()

    total_stmt = _apply_filters(select(func.count(Recipe.id)), **filters)
    if exclude_id:
        total_stmt = total_stmt.where(Recipe.id.not_in(exclude_id))
    if unmapped_recipe_ids:
        total_stmt = total_stmt.where(Recipe.id.not_in(unmapped_recipe_ids))
    total = session.scalar(total_stmt) or 0

    effective_offset = offset if offset is not None else (page - 1) * page_size
    if sort in {"price_low", "price_high"}:
        id_stmt = _apply_filters(select(Recipe.id), **filters)
        if exclude_id:
            id_stmt = id_stmt.where(Recipe.id.not_in(exclude_id))
        if unmapped_recipe_ids:
            id_stmt = id_stmt.where(Recipe.id.not_in(unmapped_recipe_ids))
        candidate_ids = list(session.scalars(id_stmt).all())
        intrinsic = _intrinsic_prices(candidate_ids, factory, csv_path)
        sorted_ids = sorted(
            candidate_ids,
            key=lambda recipe_id: (
                -intrinsic.get(recipe_id, (float("inf"), float("inf"), 0))[0]
                if sort == "price_high"
                else intrinsic.get(recipe_id, (float("inf"), float("inf"), 0))[0],
                recipe_id,
            ),
        )
        page_ids = sorted_ids[effective_offset:effective_offset + page_size]
        by_id: dict[int, Recipe] = {}
        if page_ids:
            rows_for_page = session.scalars(
                select(Recipe)
                .where(Recipe.id.in_(page_ids))
                .options(selectinload(Recipe.cuisines), selectinload(Recipe.tags))
            ).all()
            by_id = {recipe.id: recipe for recipe in rows_for_page}
        rows = [by_id[recipe_id] for recipe_id in page_ids if recipe_id in by_id]
    else:
        order = _SORT_COLUMNS.get(sort, _SORT_COLUMNS[facet_cfg.DEFAULT_SORT])
        stmt = (
            _apply_filters(select(Recipe), **filters)
            .options(selectinload(Recipe.cuisines), selectinload(Recipe.tags))
        )
        if exclude_id:
            stmt = stmt.where(Recipe.id.not_in(exclude_id))
        if unmapped_recipe_ids:
            stmt = stmt.where(Recipe.id.not_in(unmapped_recipe_ids))
        stmt = (
            stmt
            .order_by(order, Recipe.id)
            .offset(effective_offset)
            .limit(page_size)
        )
        rows = session.scalars(stmt).all()
        intrinsic = _intrinsic_prices(rows, factory, csv_path)
    items = [
        _to_card(
            r,
            intrinsic_score=intrinsic.get(r.id, (None, None, 0))[0],
            intrinsic_cost=intrinsic.get(r.id, (None, None, 0))[1],
            intrinsic_gap_count=intrinsic.get(r.id, (None, None, 0))[2],
        )
        for r in rows
    ]
    next_offset = effective_offset + len(items)
    has_more = next_offset < total
    return PaginatedRecipes(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=has_more,
        next_offset=next_offset if has_more else None,
    )


@router.get("/recipes/{recipe_id}", response_model=RecipeDetail)
def get_recipe(
    recipe_id: int,
    session: Session = Depends(get_session),
    csv_path: Path | None = Depends(get_planner_csv_path),
) -> RecipeDetail:
    recipe = session.get(Recipe, recipe_id)
    if recipe is None or not recipe.curated:
        raise HTTPException(status_code=404, detail="Recipe not found")

    steps = sorted(recipe.steps, key=lambda s: s.index)
    ingredients = [
        ingredient
        for ingredient in sorted(
            recipe.ingredients,
            key=lambda i: (i.position is None, i.position or 0, i.id),
        )
        if _has_display_quantity(ingredient)
    ]
    unmapped_ingredient_ids = _unmapped_ingredient_ids(session, ingredients, csv_path)
    return RecipeDetail(
        id=recipe.id,
        name=recipe.name,
        headline=recipe.headline,
        description=recipe.description,
        image_url=image_url(recipe.image_path, HERO_WIDTH),
        source_url=recipe.url,
        difficulty=recipe.difficulty,
        prep_time_min=recipe.prep_time_min,
        total_time_min=recipe.total_time_min,
        base_yield=recipe.base_yield,
        serving_size_g=recipe.serving_size_g,
        energy_kcal=recipe.energy_kcal,
        protein_g=recipe.protein_g,
        fat_g=recipe.fat_g,
        carbs_g=recipe.carbs_g,
        protein_energy_ratio=recipe.protein_energy_ratio,
        avg_rating=recipe.avg_rating,
        ratings_count=recipe.ratings_count,
        cuisines=[facet_cfg.clean_cuisine(c.name) for c in recipe.cuisines],
        tags=list(dict.fromkeys(
            _CHIP_LABELS[t.type] for t in recipe.tags if t.type in _CHIP_LABELS
        )),
        allergens=[a.name for a in recipe.allergens],
        ingredients=[
            IngredientOut(
                name=i.name,
                amount=i.amount,
                unit=i.unit,
                amount_g=i.amount_g,
                canonical_unit=i.canonical_unit,
                image_url=image_url(i.image_path, 200),
                unmapped=i.id in unmapped_ingredient_ids,
                spoons=measures.spoons_for(i.name, i.amount, i.unit),
                spoon_range=(
                    list(rng)
                    if (rng := measures.spoon_range_for(i.name, i.amount, i.unit))
                    else None
                ),
                amount_g_estimated=measures.amount_g_is_estimated(i.unit),
                potency=measures.potency_for(i.name),
            )
            for i in ingredients
        ],
        steps=[StepOut(index=s.index, text=s.instructions_text) for s in steps],
        nutrition=[
            NutritionOut(name=n.name, amount=n.amount, unit=n.unit)
            for n in recipe.nutrition
        ],
        macros_suspect=bool(recipe.macros_suspect),
        flagged_suspicious=bool(recipe.flagged_suspicious),
        audited_at=recipe.audited_at,
        edits=[
            RecipeEditOut(
                field=e.field,
                old_value=e.old_value,
                new_value=e.new_value,
                status=e.status,
                source=e.source,
                reason=e.reason,
                model=e.model,
                created_at=e.created_at,
            )
            for e in sorted(recipe.edits, key=lambda e: (e.created_at, e.id))
            if e.status == "applied"
        ],
    )


# --------------------------------------------------------------------------
# Macro audit
# --------------------------------------------------------------------------

@router.post("/recipes/{recipe_id}/flag", response_model=AuditJobOut)
def flag_recipe(recipe_id: int, session: Session = Depends(get_session)) -> AuditJobOut:
    """Flag the numbers as suspicious and start a background audit.

    Returns a job handle to poll: the arithmetic checks are instant but the
    composition check is a model call.
    """
    from app.api.deps import _session_factory
    from app import audit as audit_mod

    recipe = session.get(Recipe, recipe_id)
    if recipe is None or not recipe.curated:
        raise HTTPException(status_code=404, detail="Recipe not found")
    audit_mod.flag_recipe(session, recipe_id)
    job = audit_mod.start_background(_session_factory(), recipe_id)
    return AuditJobOut(**job.as_dict())


@router.get("/recipes/audit-jobs/{job_id}", response_model=AuditJobOut)
def get_audit_job(job_id: str) -> AuditJobOut:
    from app import audit as audit_mod

    job = audit_mod.REGISTRY.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return AuditJobOut(**job.as_dict())


@router.post("/recipes/{recipe_id}/revert", response_model=RecipeDetail)
def revert_recipe_edits(
    recipe_id: int,
    session: Session = Depends(get_session),
    csv_path: Path | None = Depends(get_planner_csv_path),
) -> RecipeDetail:
    """Put the source's original numbers back and mark the edits reverted."""
    from app import audit as audit_mod

    try:
        audit_mod.revert_recipe(session, recipe_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return get_recipe(recipe_id, session, csv_path)


@router.get("/facets", response_model=FacetsOut)
def get_facets(
    session: Session = Depends(get_session),
    csv_path: Path | None = Depends(get_planner_csv_path),
) -> FacetsOut:
    curated = Recipe.curated == 1

    # Cuisines above the noise threshold, cleaned for display.
    cuisine_rows = session.execute(
        select(RecipeCuisine.name, func.count(func.distinct(Recipe.id)))
        .join(Recipe, RecipeCuisine.recipe_id == Recipe.id)
        .where(curated)
        .group_by(RecipeCuisine.name)
        .having(func.count(func.distinct(Recipe.id)) >= facet_cfg.CUISINE_MIN_COUNT)
        .order_by(func.count(func.distinct(Recipe.id)).desc())
    ).all()
    cuisines = [
        FacetCount(value=name, label=facet_cfg.clean_cuisine(name), count=count)
        for name, count in cuisine_rows
    ]

    def tag_facets(mapping: dict[str, str]) -> list[FacetCount]:
        out: list[FacetCount] = []
        for tag_type, label in mapping.items():
            count = session.scalar(
                select(func.count(func.distinct(Recipe.id)))
                .select_from(Recipe)
                .join(RecipeTag, RecipeTag.recipe_id == Recipe.id)
                .where(curated, RecipeTag.type == tag_type)
            ) or 0
            if count:
                out.append(FacetCount(value=tag_type, label=label, count=count))
        return sorted(out, key=lambda f: f.count, reverse=True)

    def diet_facets() -> list[FacetCount]:
        out: list[FacetCount] = []
        for value, (column, label) in facet_cfg.DIET_COLUMNS.items():
            count = session.scalar(
                select(func.count()).select_from(Recipe).where(curated, getattr(Recipe, column) == 1)
            ) or 0
            if count:
                out.append(FacetCount(value=value, label=label, count=count))
        return sorted(out, key=lambda f: f.count, reverse=True)

    def ingredient_count(keywords: list[str]) -> int:
        return session.scalar(
            select(func.count()).select_from(Recipe).where(curated, _ingredient_match(keywords))
        ) or 0

    proteins = [
        FacetCount(value=v, label=label, count=ingredient_count(facet_cfg.INGREDIENT_KEYWORDS[v]))
        for v, label in facet_cfg.PROTEIN_FILTERS.items()
    ]
    proteins = sorted([p for p in proteins if p.count], key=lambda f: f.count, reverse=True)

    allergen_rows = session.execute(
        select(RecipeAllergen.name, func.count(func.distinct(Recipe.id)))
        .join(Recipe, RecipeAllergen.recipe_id == Recipe.id)
        .where(curated, RecipeAllergen.name.not_in(["May contain traces of allergens"]))
        .group_by(RecipeAllergen.name)
        .order_by(func.count(func.distinct(Recipe.id)).desc())
        .limit(14)
    ).all()
    # The "exclude" filter offers allergens plus ingredient groups (proteins, coconut).
    excludes = [FacetCount(value=n, label=n, count=c) for n, c in allergen_rows]
    seen_labels = {e.label.lower() for e in excludes}
    for v, label in facet_cfg.EXCLUDE_INGREDIENTS.items():
        if label.lower() in seen_labels:
            continue  # already covered by an allergen (e.g. Fish)
        excludes.append(
            FacetCount(value=v, label=label, count=ingredient_count(facet_cfg.INGREDIENT_KEYWORDS[v]))
        )
    unmapped_count = len(_unmapped_recipe_ids(session, csv_path))
    if unmapped_count:
        excludes.insert(
            0,
            FacetCount(value="unmapped", label="Unmapped ingredients", count=unmapped_count),
        )

    return FacetsOut(
        cuisines=cuisines,
        diets=diet_facets(),
        attributes=tag_facets(facet_cfg.ATTRIBUTE_TAGS),
        proteins=proteins,
        excludes=excludes,
        ranges={
            "kcal": NumericRange(min=0, max=1500),
            "protein": NumericRange(min=0, max=80),
            "protein_ratio": NumericRange(min=0, max=12),
            "time": NumericRange(min=0, max=90),
        },
        sorts=[FacetCount(value=v, label=l, count=0) for v, l in facet_cfg.SORTS.items()],
    )
