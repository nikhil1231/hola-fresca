"""Recipe browse API: list (filter/sort/paginate), detail, and facets.

Every endpoint is scoped to the curated active library (``Recipe.curated == 1``).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, func, nullslast, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api import facets as facet_cfg
from app.api.deps import get_session
from app.api.schemas import (
    FacetCount,
    FacetsOut,
    IngredientOut,
    NumericRange,
    NutritionOut,
    PaginatedRecipes,
    RecipeCard,
    RecipeDetail,
    StepOut,
)
from app.db.models import Recipe, RecipeAllergen, RecipeCuisine, RecipeIngredient, RecipeTag
from app.media import image_url


def _ingredient_match(keywords: list[str]):
    """A condition: the recipe has an ingredient whose name contains a keyword."""
    return Recipe.ingredients.any(
        or_(*[RecipeIngredient.name.ilike(f"%{k}%") for k in keywords])
    )

router = APIRouter(prefix="/api", tags=["recipes"])

CARD_WIDTH = 500
HERO_WIDTH = 1200
MAX_PAGE_SIZE = 60

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


def _to_card(r: Recipe) -> RecipeCard:
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
    sort: str = facet_cfg.DEFAULT_SORT,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=MAX_PAGE_SIZE),
    session: Session = Depends(get_session),
) -> PaginatedRecipes:
    filters = dict(
        q=q, cuisine=cuisine, diet=diet, tag=tag, protein=protein, max_time=max_time,
        min_protein=min_protein, min_protein_ratio=min_protein_ratio, max_kcal=max_kcal,
        difficulty=difficulty, exclude=exclude,
    )

    total = session.scalar(
        _apply_filters(select(func.count(Recipe.id)), **filters)
    ) or 0

    order = _SORT_COLUMNS.get(sort, _SORT_COLUMNS[facet_cfg.DEFAULT_SORT])
    stmt = (
        _apply_filters(select(Recipe), **filters)
        .options(selectinload(Recipe.cuisines), selectinload(Recipe.tags))
        .order_by(order, Recipe.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = session.scalars(stmt).all()
    items = [_to_card(r) for r in rows]
    return PaginatedRecipes(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page - 1) * page_size + len(items) < total,
    )


@router.get("/recipes/{recipe_id}", response_model=RecipeDetail)
def get_recipe(
    recipe_id: int, session: Session = Depends(get_session)
) -> RecipeDetail:
    recipe = session.get(Recipe, recipe_id)
    if recipe is None or not recipe.curated:
        raise HTTPException(status_code=404, detail="Recipe not found")

    steps = sorted(recipe.steps, key=lambda s: s.index)
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
            )
            for i in recipe.ingredients
        ],
        steps=[StepOut(index=s.index, text=s.instructions_text) for s in steps],
        nutrition=[
            NutritionOut(name=n.name, amount=n.amount, unit=n.unit)
            for n in recipe.nutrition
        ],
    )


@router.get("/facets", response_model=FacetsOut)
def get_facets(session: Session = Depends(get_session)) -> FacetsOut:
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
