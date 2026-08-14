"""Recipe browse API: list (filter/sort/paginate), detail, and facets.

Every endpoint is scoped to the curated active library (``Recipe.curated == 1``).
"""
from __future__ import annotations

from pathlib import Path
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from rapidfuzz import fuzz, utils
from sqlalchemy import Select, and_, func, nullslast, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.api import facets as facet_cfg
from app.api.deps import (
    get_active_retailer,
    get_current_user,
    get_planner_csv_path,
    get_session,
    get_session_factory,
    require_admin,
)
from app.api.schemas import (
    AuditJobOut,
    FacetCount,
    FacetsOut,
    IngredientOut,
    MacrosOut,
    NumericRange,
    NutritionOut,
    PaginatedRecipes,
    PersonalRatingIn,
    ProteinPreviewIn,
    ProteinPreviewOut,
    ProteinProfileOut,
    ProteinTargetOut,
    RecipeCard,
    RecipeDetail,
    RecipeEditOut,
    StepOut,
    WishlistIn,
)
from app.db.models import (
    IngredientMapping,
    PersonalRecipeRating,
    PersonalRecipeWishlist,
    Recipe,
    RecipeAllergen,
    RecipeCuisine,
    RecipeIngredient,
    RecipeTag,
    User,
    UserRecipeHide,
)
from app import classify, measures
from app import protein as protein_mod
from app.mapping.candidates import load_source_id_index
from app.media import image_url
from app.planner.cache import get_index, get_standalone_prices
from app.retailers import DEFAULT_RETAILER
from app.planner.index import RETAILER, PlanIndex, PlanRecipe, resolve_protein


def _ingredient_match(keywords: list[str]):
    """A condition: the recipe has an ingredient whose name contains a keyword."""
    return Recipe.ingredients.any(
        or_(*[RecipeIngredient.name.ilike(f"%{k}%") for k in keywords])
    )


def _main_protein_match(keywords: list[str]):
    """A condition: the recipe has a main protein ingredient matching a keyword."""
    protein_names = or_(*[RecipeIngredient.name.ilike(f"%{k}%") for k in keywords])
    non_main_names = or_(
        *[
            RecipeIngredient.name.ilike(f"%{k}%")
            for k in facet_cfg.PROTEIN_NON_MAIN_KEYWORDS
        ]
    )
    return Recipe.ingredients.any(and_(protein_names, ~non_main_names))


def _library_condition():
    """The shared library: curated by rules, and not a broken source row.

    This is what exists for everybody. It deliberately ignores personal hides —
    a recipe you have hidden is still in the library, still priced, and still
    valid in a plan you already made.
    """
    return Recipe.curated == 1, Recipe.manually_excluded == 0


def _visible_recipe_condition(user_id: int):
    """The library as one user sees it: shared, minus what they have hidden.

    Applied here rather than in the planner index because the index is built once
    and shared by every user; a hide is the sort of thing that has to be a
    condition on the query, not a property of the snapshot.
    """
    return (
        *_library_condition(),
        ~select(UserRecipeHide.recipe_id)
        .where(
            UserRecipeHide.user_id == user_id,
            UserRecipeHide.recipe_id == Recipe.id,
        )
        .exists(),
    )

def _require_library_recipe(session: Session, recipe_id: int) -> Recipe:
    """Load a recipe that is in the shared library, or 404.

    Personal hides are not consulted: hiding a recipe takes it out of your
    browse, not out of existence, and a plan or a rating that already refers to
    it has to keep working.
    """
    recipe = session.get(Recipe, recipe_id)
    if recipe is None or not recipe.curated or recipe.manually_excluded:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return recipe


router = APIRouter(prefix="/api", tags=["recipes"])

CARD_WIDTH = 500
HERO_WIDTH = 1200
MAX_PAGE_SIZE = 60
INTRINSIC_PORTIONS = 4
# Minimum score a query token must reach against some token of a recipe before
# that recipe counts as a match at all. 85 is where typo tolerance stops and
# noise starts: it still reads "koren" as korean and "chikcen" as chicken, while
# 88 loses both and 80 starts admitting unrelated words.
FUZZY_SEARCH_CUTOFF = 85
_EXACT_TOKEN = 100
_PREFIX_TOKEN = 95
_SUBSTRING_TOKEN = 90
# Below this length a token is too short to correct safely; see _token_score.
_MIN_FUZZY_TOKEN_LEN = 4
# How much matching in the title outranks matching only in the headline.
_TITLE_BONUS = 12.0

# Attribute tag types that become display chips on a card, with friendly labels.
_CHIP_LABELS = dict(facet_cfg.ATTRIBUTE_TAGS)

# Popularity sorts and the rating shown on a card use the effective figures —
# the dish's whole lineage, matching what the source's own page shows — falling
# back to the per-revision columns on a database that predates the backfill.
_EFFECTIVE_RATING = func.coalesce(Recipe.effective_rating, Recipe.avg_rating)
_EFFECTIVE_RATINGS_COUNT = func.coalesce(
    Recipe.effective_ratings_count, Recipe.ratings_count
)

_SORT_COLUMNS = {
    "popular": nullslast(_EFFECTIVE_RATINGS_COUNT.desc()),
    "rating": nullslast(_EFFECTIVE_RATING.desc()),
    "protein_high": nullslast(Recipe.protein_g.desc()),
    "protein_ratio": nullslast(Recipe.protein_energy_ratio.desc()),
    "kcal_low": nullslast(Recipe.energy_kcal.asc()),
    "time_low": nullslast(Recipe.total_time_min.asc()),
    "newest": nullslast(Recipe.source_created_at.desc()),
}


def _search_text(name: str | None, headline: str | None) -> str:
    return " ".join(part for part in (name, headline) if part)


def _tokens(text: str | None) -> list[str]:
    processed = utils.default_process(text or "")
    return re.findall(r"\w+", processed or "")


def _token_score(query_token: str, haystack: list[str]) -> int:
    """How well one query token is answered by a recipe's tokens, 0-100.

    Graded rather than boolean so the ranking has something to work with: an
    exact word beats a prefix, a prefix beats a word that merely contains it,
    and anything else has to survive a typo check.
    """
    best = 0
    for token in haystack:
        if token == query_token:
            return _EXACT_TOKEN
        if token.startswith(query_token):
            best = max(best, _PREFIX_TOKEN)
        elif query_token in token:
            best = max(best, _SUBSTRING_TOKEN)
        elif len(query_token) >= _MIN_FUZZY_TOKEN_LEN:
            # Only long tokens get typo tolerance. "bbq" is three characters and
            # within edit distance of half the corpus; "korean" is not.
            best = max(best, int(fuzz.ratio(query_token, token)))
    return best


def _relevance(query: str, name: str | None, headline: str | None) -> float | None:
    """Relevance of one recipe to a query, or None when it is not a match.

    Every query token must be answered by the title or the headline. That AND is
    the whole point: scoring the query against the recipe as one blob is what let
    "korean bbq noodles" return 344 recipes, because a single shared word —
    "noodles" — was enough to carry the whole phrase, and enough of them tied at
    a perfect score that the ordering was meaningless too.
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return None
    name_tokens = _tokens(name)
    headline_tokens = _tokens(headline)

    total = 0
    in_title = 0
    for query_token in query_tokens:
        title_score = _token_score(query_token, name_tokens)
        best = max(title_score, _token_score(query_token, headline_tokens))
        if best < FUZZY_SEARCH_CUTOFF:
            return None
        total += best
        if title_score >= FUZZY_SEARCH_CUTOFF:
            in_title += 1
    # A dish whose *name* carries the words beats one that only mentions them in
    # its headline, so "chicken curry" leads with curries rather than with sides
    # served alongside one.
    return total / len(query_tokens) + (in_title / len(query_tokens)) * _TITLE_BONUS


def _fuzzy_search_match(query: str, name: str | None, headline: str | None) -> bool:
    return _relevance(query, name, headline) is not None


def _ranked_recipe_ids(session: Session, filters: dict, user_id: int) -> list[int]:
    """Ids matching the filters, most relevant first when a query is given."""
    q = (filters.get("q") or "").strip()
    stmt = _apply_filters(
        select(
            Recipe.id,
            Recipe.name,
            Recipe.headline,
            func.coalesce(Recipe.effective_ratings_count, Recipe.ratings_count),
        ),
        **filters,
        user_id=user_id,
    )
    rows = session.execute(stmt).all()
    if not q:
        return [row[0] for row in rows]
    scored = []
    for recipe_id, name, headline, ratings in rows:
        score = _relevance(q, name, headline)
        if score is not None:
            scored.append((score, ratings or 0, recipe_id))
    # Popularity breaks ties between equally good text matches, so the better
    # known of two identically named dishes comes first.
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return [recipe_id for _, _, recipe_id in scored]


def _filtered_recipe_ids(session: Session, filters: dict, user_id: int) -> list[int]:
    return _ranked_recipe_ids(session, filters, user_id)


def _rows_in_id_order(session: Session, page_ids: list[int]) -> list[Recipe]:
    """Load a page of recipes, preserving the order the ids were given in.

    ``IN`` returns rows in whatever order it likes, which would throw away a
    ranking computed in Python.
    """
    if not page_ids:
        return []
    rows = session.scalars(
        select(Recipe)
        .where(Recipe.id.in_(page_ids))
        .options(selectinload(Recipe.cuisines), selectinload(Recipe.tags))
    ).all()
    by_id = {recipe.id: recipe for recipe in rows}
    return [by_id[recipe_id] for recipe_id in page_ids if recipe_id in by_id]


def _shown_rating(r: Recipe) -> float | None:
    return r.effective_rating if r.effective_rating is not None else r.avg_rating


def _shown_ratings_count(r: Recipe) -> int | None:
    if r.effective_ratings_count is not None:
        return r.effective_ratings_count
    return r.ratings_count


def _apply_course(stmt: Select, course: list[str] | None) -> Select:
    """Restrict to the requested courses; mains only when nothing is asked for.

    Sides and ready-made items are worth having in the library — they are real
    things you can add to a week — but they are not what someone browsing for
    dinner means, and being cheap they win every price-ordered list. So they are
    opt-in, the same way unmapped recipes already are.
    """
    wanted = [c for c in (course or facet_cfg.DEFAULT_COURSES) if c in facet_cfg.COURSES]
    if not wanted or set(wanted) == set(facet_cfg.COURSES):
        return stmt
    condition = Recipe.course.in_(wanted)
    if facet_cfg.MAIN in wanted:
        # A database that predates the column reads NULL, which is a main until
        # the next enrich pass says otherwise.
        condition = or_(condition, Recipe.course.is_(None))
    return stmt.where(condition)


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
    rated: bool = False,
    wishlisted: bool = False,
    course: list[str] | None = None,
    user_id: int,
) -> Select:
    stmt = stmt.where(*_visible_recipe_condition(user_id))
    stmt = _apply_course(stmt, course)
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
        _main_protein_match(facet_cfg.INGREDIENT_KEYWORDS[p])
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
    if rated:
        stmt = stmt.where(
            Recipe.personal_ratings.any(PersonalRecipeRating.user_id == user_id)
        )
    if wishlisted:
        stmt = stmt.where(
            Recipe.wishlist_entries.any(PersonalRecipeWishlist.user_id == user_id)
        )
    return stmt


def _personal_rating_map(
    session: Session, user_id: int, recipe_ids: list[int]
) -> dict[int, int]:
    if not recipe_ids:
        return {}
    rows = session.scalars(
        select(PersonalRecipeRating).where(
            PersonalRecipeRating.user_id == user_id,
            PersonalRecipeRating.recipe_id.in_(recipe_ids),
        )
    ).all()
    return {row.recipe_id: row.rating for row in rows}


def _wishlist_map(session: Session, user_id: int, recipe_ids: list[int]) -> dict[int, bool]:
    if not recipe_ids:
        return {}
    rows = session.scalars(
        select(PersonalRecipeWishlist.recipe_id).where(
            PersonalRecipeWishlist.user_id == user_id,
            PersonalRecipeWishlist.recipe_id.in_(recipe_ids),
        )
    ).all()
    return {recipe_id: True for recipe_id in rows}


def _round_money(value: float) -> float:
    return round(value, 2)


def _to_card(
    r: Recipe,
    *,
    intrinsic_score: float | None = None,
    intrinsic_cost: float | None = None,
    intrinsic_gap_count: int = 0,
    personal_rating: int | None = None,
    wishlisted: bool | None = None,
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
        avg_rating=_shown_rating(r),
        ratings_count=_shown_ratings_count(r),
        course=r.course or facet_cfg.MAIN,
        personal_rating=personal_rating,
        wishlisted=bool(wishlisted),
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
    retailer: str = DEFAULT_RETAILER,
) -> dict[int, tuple[float, float, int]]:
    recipe_ids = [recipe if isinstance(recipe, int) else recipe.id for recipe in rows]
    if not recipe_ids:
        return {}
    prices = get_standalone_prices(
        factory, servings=INTRINSIC_PORTIONS, csv_path=csv_path, retailer=retailer
    )
    return {
        recipe_id: (
            _round_money(price.score),
            _round_money(price.consumed_cost),
            price.gap_count,
        )
        for recipe_id in recipe_ids
        if (price := prices.get(recipe_id)) is not None
    }


def _recipe_ids_with_pricing_gaps(
    recipe_ids: list[int],
    factory: sessionmaker[Session],
    csv_path: Path | None,
    retailer: str = DEFAULT_RETAILER,
) -> set[int]:
    """Recipes carrying an ingredient the basket cannot price.

    A gap is exactly what stops the standalone price from being the whole story —
    an unmapped line, a mapping with nothing buyable behind it, or a line the
    library never tracked — so it is read off the same precomputed table rather
    than re-walking each recipe's needs.
    """
    if not recipe_ids:
        return set()
    prices = get_standalone_prices(
        factory, servings=INTRINSIC_PORTIONS, csv_path=csv_path, retailer=retailer
    )
    return {
        recipe_id
        for recipe_id in recipe_ids
        if (price := prices.get(recipe_id)) is not None and price.has_gap
    }


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


def _ingredient_keys(
    session: Session,
    ingredients: list[RecipeIngredient],
    csv_path: Path | None,
    retailer: str = DEFAULT_RETAILER,
) -> dict[int, str]:
    sid_index = load_source_id_index(csv_path)
    mapping_rows = list(
        session.scalars(select(IngredientMapping).where(IngredientMapping.retailer == retailer))
    )
    roots = _alias_roots(mapping_rows)
    keys: dict[int, str] = {}
    for ingredient in ingredients:
        if not _has_display_quantity(ingredient):
            continue
        raw_key = sid_index.get(ingredient.source_ingredient_id or "")
        if raw_key is not None:
            keys[ingredient.id] = roots.get(raw_key, raw_key)
    return keys


def _unmapped_ingredient_ids(
    session: Session,
    ingredients: list[RecipeIngredient],
    csv_path: Path | None,
    retailer: str = DEFAULT_RETAILER,
) -> set[int]:
    ingredient_keys = _ingredient_keys(session, ingredients, csv_path, retailer)
    mapping_rows = list(
        session.scalars(select(IngredientMapping).where(IngredientMapping.retailer == retailer))
    )
    by_key = {row.ingredient_key: row for row in mapping_rows}
    unmapped: set[int] = set()
    for ingredient in ingredients:
        if not _has_display_quantity(ingredient):
            continue
        key = ingredient_keys.get(ingredient.id)
        row = by_key.get(key) if key else None
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


def _unmapped_recipe_ids(
    session: Session, csv_path: Path | None, retailer: str = DEFAULT_RETAILER) -> set[int]:
    sid_index = load_source_id_index(csv_path)
    mapping_rows = list(
        session.scalars(select(IngredientMapping).where(IngredientMapping.retailer == retailer))
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
                *_library_condition(),
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
    rated: bool = False,
    wishlisted: bool = False,
    exclude_id: list[int] = Query(default_factory=list),
    course: list[str] = Query(default_factory=list),
    sort: str = facet_cfg.DEFAULT_SORT,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=MAX_PAGE_SIZE),
    offset: int | None = Query(default=None, ge=0),
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> PaginatedRecipes:
    filters = dict(
        q=q, cuisine=cuisine, diet=diet, tag=tag, protein=protein, max_time=max_time,
        min_protein=min_protein, min_protein_ratio=min_protein_ratio, max_kcal=max_kcal,
        difficulty=difficulty, exclude=exclude, rated=rated, wishlisted=wishlisted,
        course=course,
    )
    exclude_unmapped = "unmapped" in exclude
    excluded_recipe_ids = set(exclude_id)
    candidate_ids: list[int] | None = None
    if q or exclude_unmapped:
        filtered_candidate_ids = _filtered_recipe_ids(session, filters, user.id)
        if exclude_unmapped:
            excluded_recipe_ids.update(
                _recipe_ids_with_pricing_gaps(
                    filtered_candidate_ids, factory, csv_path, retailer
                )
            )
        candidate_ids = [
            recipe_id
            for recipe_id in filtered_candidate_ids
            if recipe_id not in excluded_recipe_ids
        ]
        total = len(candidate_ids)
    else:
        total_stmt = _apply_filters(select(func.count(Recipe.id)), **filters, user_id=user.id)
        if excluded_recipe_ids:
            total_stmt = total_stmt.where(Recipe.id.not_in(excluded_recipe_ids))
        total = session.scalar(total_stmt) or 0

    effective_offset = offset if offset is not None else (page - 1) * page_size
    # A search is ordered by how well it matches unless the reader asked for a
    # specific order. Sorting the hits by popularity instead is what made the
    # ranking invisible: the best match for "korean bbq noodles" could sit pages
    # below a loosely related dish that happened to be better known.
    if q and sort == facet_cfg.DEFAULT_SORT and candidate_ids is not None:
        page_ids = candidate_ids[effective_offset:effective_offset + page_size]
        rows = _rows_in_id_order(session, page_ids)
        intrinsic = _intrinsic_prices(rows, factory, csv_path, retailer)
    elif sort in {"price_low", "price_high"}:
        if candidate_ids is None:
            id_stmt = _apply_filters(select(Recipe.id), **filters, user_id=user.id)
            if excluded_recipe_ids:
                id_stmt = id_stmt.where(Recipe.id.not_in(excluded_recipe_ids))
            candidate_ids = list(session.scalars(id_stmt).all())
        intrinsic = _intrinsic_prices(candidate_ids, factory, csv_path, retailer)
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
        rows = _rows_in_id_order(session, page_ids)
    else:
        order = _SORT_COLUMNS.get(sort, _SORT_COLUMNS[facet_cfg.DEFAULT_SORT])
        if candidate_ids is not None:
            if candidate_ids:
                stmt = (
                    select(Recipe)
                    .where(Recipe.id.in_(candidate_ids))
                    .options(selectinload(Recipe.cuisines), selectinload(Recipe.tags))
                    .order_by(order, Recipe.id)
                    .offset(effective_offset)
                    .limit(page_size)
                )
                rows = session.scalars(stmt).all()
            else:
                rows = []
        else:
            stmt = (
                _apply_filters(select(Recipe), **filters, user_id=user.id)
                .options(selectinload(Recipe.cuisines), selectinload(Recipe.tags))
            )
            if excluded_recipe_ids:
                stmt = stmt.where(Recipe.id.not_in(excluded_recipe_ids))
            stmt = (
                stmt
                .order_by(order, Recipe.id)
                .offset(effective_offset)
                .limit(page_size)
            )
            rows = session.scalars(stmt).all()
        intrinsic = _intrinsic_prices(rows, factory, csv_path, retailer)
    page_ids = [r.id for r in rows]
    personal_ratings = _personal_rating_map(session, user.id, page_ids)
    wishlist = _wishlist_map(session, user.id, page_ids)
    items = [
        _to_card(
            r,
            personal_rating=personal_ratings.get(r.id),
            wishlisted=wishlist.get(r.id, False),
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
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> RecipeDetail:
    recipe = _require_library_recipe(session, recipe_id)

    steps = sorted(recipe.steps, key=lambda s: s.index)
    ingredients = [
        ingredient
        for ingredient in sorted(
            recipe.ingredients,
            key=lambda i: (i.position is None, i.position or 0, i.id),
        )
        if _has_display_quantity(ingredient)
    ]
    ingredient_keys = _ingredient_keys(session, ingredients, csv_path, retailer)
    unmapped_ingredient_ids = _unmapped_ingredient_ids(
        session, ingredients, csv_path, retailer
    )
    index, plan_recipe = _plan_recipe(factory, recipe_id, csv_path, retailer)
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
        avg_rating=_shown_rating(recipe),
        ratings_count=_shown_ratings_count(recipe),
        personal_rating=_personal_rating_map(session, user.id, [recipe_id]).get(recipe_id),
        wishlisted=_wishlist_map(session, user.id, [recipe_id]).get(recipe_id, False),
        cuisines=[facet_cfg.clean_cuisine(c.name) for c in recipe.cuisines],
        tags=list(dict.fromkeys(
            _CHIP_LABELS[t.type] for t in recipe.tags if t.type in _CHIP_LABELS
        )),
        allergens=[a.name for a in recipe.allergens],
        ingredients=[
            IngredientOut(
                ingredient_key=ingredient_keys.get(i.id),
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
        steps=[
            StepOut(index=s.index, text=s.instructions_text, image_url=image_url(s.image_path, 1200))
            for s in steps
        ],
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
        protein=_protein_profile(index, plan_recipe),
    )


# --- protein modifiers ------------------------------------------------------


def _plan_recipe(
    factory: sessionmaker[Session],
    recipe_id: int,
    csv_path: Path | None,
    retailer: str = DEFAULT_RETAILER,
) -> tuple[PlanIndex, PlanRecipe | None]:
    index = get_index(factory, csv_path=csv_path, retailer=retailer)
    return index, index.recipes.get(recipe_id)


def _protein_profile(index: PlanIndex, recipe: PlanRecipe | None) -> ProteinProfileOut | None:
    """The swap panel's whole input: this dish's protein and its alternatives."""
    if recipe is None or recipe.protein is None:
        return None
    line = recipe.protein
    targets = []
    for target in protein_mod.targets_for_form(line.form):
        key = target.key_for(line.form)
        if key is None:
            continue
        ingredient = index.ingredient(key)
        if ingredient is None:
            # Nothing approved to buy it with: offering the swap would produce a
            # basket that silently cannot be shopped.
            continue
        reference = protein_mod.lookup(key)
        targets.append(
            ProteinTargetOut(
                id=target.id,
                label=target.label,
                ingredient_key=key,
                ingredient_name=ingredient.name or protein_mod.display_name(key),
                cook_note=target.cook_note,
                per_100g=MacrosOut.of(reference.per_100g if reference else protein_mod.Macros()),
                available=ingredient.shoppable or ingredient.pantry_staple,
            )
        )
    return ProteinProfileOut(
        ingredient_key=line.key,
        name=line.name,
        label=line.ingredient.noun,
        type=line.type,
        form=line.form,
        grams=round(line.grams, 1),
        per_100g=MacrosOut.of(line.ingredient.per_100g),
        targets=targets,
    )


_DIET_LABELS = {
    "is_vegetarian": "vegetarian",
    "is_pescatarian": "pescatarian",
    "is_dairy_free": "dairy free",
    "is_gluten_free": "gluten free",
}


def _diet_changes(before: dict[str, bool], after: dict[str, bool]) -> list[str]:
    changes = []
    for flag, label in _DIET_LABELS.items():
        # Every vegetarian dish is also pescatarian, so a swap to tofu earns both
        # flags and only one of them is news.
        if flag == "is_pescatarian" and after.get("is_vegetarian") != before.get("is_vegetarian"):
            continue
        if after.get(flag) and not before.get(flag):
            changes.append(f"Now {label}")
        elif before.get(flag) and not after.get(flag):
            changes.append(f"No longer {label}")
    return changes


def _swapped_line_quantity(
    index: PlanIndex, key: str, grams: float
) -> tuple[float | None, str | None, float]:
    """``(amount, unit, amount_g)`` for a line the modifier rewrote.

    Deliberately the same call the basket makes, so the count on the page is the
    count in the order. An ingredient bought by the piece has to land on a piece.
    """
    ingredient = index.ingredient(key)
    grams, units = protein_mod.swapped_quantity(
        grams,
        unit_kind=ingredient.unit_kind if ingredient else "mass",
        each_to_grams=ingredient.each_to_grams if ingredient else None,
    )
    if units is not None:
        return units, "unit(s)", grams
    return round(grams, 1), "grams", grams


def _ingredient_image(session: Session, name: str) -> str | None:
    """Reuse the library's own picture of an ingredient this recipe never had."""
    path = session.scalars(
        select(RecipeIngredient.image_path)
        .where(
            func.lower(RecipeIngredient.name) == name.lower(),
            RecipeIngredient.image_path.is_not(None),
        )
        .limit(1)
    ).first()
    return image_url(path, 200) if path else None


@router.post("/recipes/{recipe_id}/protein/preview", response_model=ProteinPreviewOut)
def preview_protein(
    recipe_id: int,
    body: ProteinPreviewIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    _user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> ProteinPreviewOut:
    """The recipe as it would be with this protein modifier applied.

    Read-only by construction: it takes the stored recipe, the modifier and the
    planner index, and returns a rendering. Nothing about the recipe changes,
    which is why this is a POST that writes nothing — the request body is a
    description of a hypothetical, not an edit.
    """
    recipe = _require_library_recipe(session, recipe_id)

    index, plan_recipe = _plan_recipe(factory, recipe_id, csv_path, retailer)
    if plan_recipe is None or plan_recipe.protein is None:
        raise HTTPException(
            status_code=400, detail="This recipe has no protein that can be swapped or scaled."
        )
    resolution = resolve_protein(plan_recipe, body.to_domain())
    if resolution is None:
        resolution = protein_mod.resolve(
            plan_recipe.protein,
            protein_mod.ProteinModifier(),
            base_yield=plan_recipe.base_yield,
            recipe_macros=plan_recipe.macros,
        )

    ingredients = [
        ingredient
        for ingredient in sorted(
            recipe.ingredients, key=lambda i: (i.position is None, i.position or 0, i.id)
        )
        if _has_display_quantity(ingredient)
    ]
    ingredient_keys = _ingredient_keys(session, ingredients, csv_path, retailer)
    unmapped_ingredient_ids = _unmapped_ingredient_ids(
        session, ingredients, csv_path, retailer
    )
    companions = protein_mod.companion_swaps(list(ingredient_keys.values()), resolution)

    out_ingredients: list[IngredientOut] = []
    names_before: list[str] = []
    names_after: list[str] = []
    for line in ingredients:
        key = ingredient_keys.get(line.id)
        names_before.append(line.name)
        name, unit, amount, amount_g = line.name, line.unit, line.amount, line.amount_g
        image = image_url(line.image_path, 200)
        unmapped = line.id in unmapped_ingredient_ids
        new_key = key

        if key is not None and key == resolution.source.key:
            new_key = resolution.target_key or key
            name = resolution.target_name if resolution.swapped else line.name
            amount, unit, amount_g = _swapped_line_quantity(
                index, new_key, resolution.grams_after
            )
            if resolution.swapped:
                image = _ingredient_image(session, name) or image
                unmapped = index.ingredient(new_key) is None
        elif key is not None and key in companions:
            new_key = companions[key]
            name = protein_mod.display_name(new_key)
            image = _ingredient_image(session, name) or image
        else:
            name = protein_mod.rename_companion(line.name, resolution)

        names_after.append(name)
        out_ingredients.append(
            IngredientOut(
                ingredient_key=new_key,
                name=name,
                amount=amount,
                unit=unit,
                amount_g=amount_g,
                canonical_unit=line.canonical_unit,
                image_url=image,
                unmapped=unmapped,
                spoons=measures.spoons_for(name, amount, unit),
                spoon_range=(
                    list(rng) if (rng := measures.spoon_range_for(name, amount, unit)) else None
                ),
                amount_g_estimated=measures.amount_g_is_estimated(unit),
                potency=measures.potency_for(name),
            )
        )

    allergens = [a.name for a in recipe.allergens]
    diet_before = classify.diet_flags(
        names_before, allergens, recipe.carbs_g, recipe.energy_kcal
    )
    diet_after = classify.diet_flags(
        names_after, allergens, resolution.macros_after.carbs_g, resolution.macros_after.kcal
    )

    return ProteinPreviewOut(
        factor=round(resolution.factor, 3),
        swapped=resolution.swapped,
        changed=resolution.changed,
        swap_id=resolution.target.id if resolution.target else None,
        swap_label=resolution.target.label if resolution.target else None,
        cook_note=resolution.cook_note if resolution.swapped else None,
        protein_name=resolution.source.name,
        protein_name_after=resolution.target_name if resolution.swapped else None,
        grams_before=round(resolution.grams_before, 1),
        grams_after=round(resolution.grams_after, 1),
        macros_before=MacrosOut.of(resolution.macros_before),
        macros_after=MacrosOut.of(resolution.macros_after),
        ingredients=out_ingredients,
        steps=[
            StepOut(
                index=s.index,
                text=protein_mod.rewrite_text(s.instructions_text, resolution),
                # A modifier rewrites the words, not the photos. The preview
                # stands in for the whole recipe wherever it renders, so a step
                # that dropped its image would show the placeholder mid-cook.
                image_url=image_url(s.image_path, 1200),
            )
            for s in sorted(recipe.steps, key=lambda s: s.index)
        ],
        diet=diet_after,
        diet_changes=_diet_changes(diet_before, diet_after),
        warnings=list(resolution.warnings),
    )


@router.put("/recipes/{recipe_id}/personal-rating", response_model=RecipeDetail)
def set_personal_rating(
    recipe_id: int,
    body: PersonalRatingIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> RecipeDetail:
    _require_library_recipe(session, recipe_id)

    existing = session.get(PersonalRecipeRating, (user.id, recipe_id))
    if body.rating is None:
        if existing is not None:
            session.delete(existing)
    elif existing is None:
        session.add(
            PersonalRecipeRating(user_id=user.id, recipe_id=recipe_id, rating=body.rating)
        )
    else:
        existing.rating = body.rating
    session.commit()
    return get_recipe(recipe_id, session, factory, csv_path, user, retailer)


@router.put("/recipes/{recipe_id}/wishlist", response_model=RecipeDetail)
def set_wishlist(
    recipe_id: int,
    body: WishlistIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> RecipeDetail:
    _require_library_recipe(session, recipe_id)

    existing = session.get(PersonalRecipeWishlist, (user.id, recipe_id))
    if body.wishlisted and existing is None:
        session.add(PersonalRecipeWishlist(user_id=user.id, recipe_id=recipe_id))
    elif not body.wishlisted and existing is not None:
        session.delete(existing)
    session.commit()
    return get_recipe(recipe_id, session, factory, csv_path, user, retailer)


@router.post("/recipes/{recipe_id}/hide")
def hide_recipe(
    recipe_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, int | bool]:
    """Stop showing this recipe to the current user.

    A personal filter, not a verdict on the recipe: it stays in the library, and
    a plan or a rating that already refers to it is untouched. Taking a genuinely
    broken source row out for everybody is a different, admin-level act — see
    ``Recipe.manually_excluded``.
    """
    _require_library_recipe(session, recipe_id)
    if session.get(UserRecipeHide, (user.id, recipe_id)) is None:
        session.add(UserRecipeHide(user_id=user.id, recipe_id=recipe_id))
        session.commit()
    return {"id": recipe_id, "hidden": True}


@router.delete("/recipes/{recipe_id}/hide")
def unhide_recipe(
    recipe_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, int | bool]:
    """Put a personally hidden recipe back.

    There was no way to undo a hide when it meant editing the library — the fix
    was a SQL statement. Now that it is one row belonging to one person, it can
    just be deleted.
    """
    existing = session.get(UserRecipeHide, (user.id, recipe_id))
    if existing is not None:
        session.delete(existing)
        session.commit()
    return {"id": recipe_id, "hidden": False}

# --------------------------------------------------------------------------
# Macro audit
# --------------------------------------------------------------------------

@router.post("/recipes/{recipe_id}/flag", response_model=AuditJobOut)
def flag_recipe(
    recipe_id: int,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> AuditJobOut:
    """Flag the numbers as suspicious and start a background audit.

    Returns a job handle to poll: the arithmetic checks are instant but the
    composition check is a model call.
    """
    from app.api.deps import _session_factory
    from app import audit as audit_mod

    recipe = _require_library_recipe(session, recipe_id)
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
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    _admin: User = Depends(require_admin),
    retailer: str = Depends(get_active_retailer),
) -> RecipeDetail:
    """Put the source's original numbers back and mark the edits reverted.

    A catalogue write: the corrected macros are what every user's browse filters
    and basket scoring read, so undoing them is not a personal act.
    """
    from app import audit as audit_mod

    try:
        audit_mod.revert_recipe(session, recipe_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return get_recipe(recipe_id, session, factory, csv_path, user, retailer)


@router.get("/facets", response_model=FacetsOut)
def get_facets(
    session: Session = Depends(get_session),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> FacetsOut:
    # Counted over what this user can actually see, so a facet never promises
    # results that a personal hide has already taken off the page.
    curated = and_(*_visible_recipe_condition(user.id))

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

    def protein_count(keywords: list[str]) -> int:
        return session.scalar(
            select(func.count()).select_from(Recipe).where(curated, _main_protein_match(keywords))
        ) or 0

    proteins = [
        FacetCount(value=v, label=label, count=protein_count(facet_cfg.INGREDIENT_KEYWORDS[v]))
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
    unmapped_count = len(_unmapped_recipe_ids(session, csv_path, retailer))
    if unmapped_count:
        excludes.insert(
            0,
            FacetCount(value="unmapped", label="Unmapped ingredients", count=unmapped_count),
        )

    # Counted over the whole curated library rather than the current filters, so
    # "Sides (23)" reads the same wherever you are and tells you the toggle has
    # something behind it.
    course_counts = dict(
        session.execute(
            select(func.coalesce(Recipe.course, facet_cfg.MAIN), func.count())
            .where(*_visible_recipe_condition(user.id))
            .group_by(func.coalesce(Recipe.course, facet_cfg.MAIN))
        ).all()
    )
    courses = [
        FacetCount(value=value, label=label, count=course_counts.get(value, 0))
        for value, label in facet_cfg.COURSES.items()
    ]

    return FacetsOut(
        courses=courses,
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
