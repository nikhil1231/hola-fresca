"""Library curation: choose the active recipe set from the full scrape.

Curation never deletes rows. It sets the ``curated`` flag on the recipes that
form the active library the app and planner use; everything else is retained so
curation can be re-run with different rules. Because the full raw payload store
is also kept, the library can always be rebuilt from scratch.

The default rules implement "Profile A — Proven": complete, cookable single
meals that real people have rated, deduplicated to one version of each dish —
the one the source still serves. Popularity is judged on the dish's whole
lineage rather than the individual revision, because a revision carries only
the ratings it earned itself. The thresholds are parameters so the set can be
widened or tightened without code changes.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Recipe

# Retail products and meal bundles that are not cookable recipes.
BUNDLE_RE = re.compile(
    r"(\bPlan\b|Meal Deal|Course Meal|\bSelection\b|\bBundle\b|Recipe Box|\bGü\b|\bBOL\b)",
    re.I,
)

# A dinner main should clear this many kcal per portion; below it the entry is a
# sauce/dip/side sold as an add-on.
MIN_KCAL = 150.0


@dataclass
class CurationRules:
    min_ratings: int = 25
    min_avg_rating: float = 0.0
    since_year: int | None = None
    drop_addons: bool = True
    dedup_versions: bool = True
    # Recency exception: recipes published within this many days need only
    # ``recent_min_ratings`` ratings, so new menu items surface before they've
    # accumulated the full rating count. Set recent_days=0 to disable.
    recent_days: int = 120
    recent_min_ratings: int = 3


@dataclass
class CurationReport:
    total: int = 0
    curated: int = 0
    cut_incomplete: int = 0
    cut_bundle: int = 0
    cut_low_kcal: int = 0
    cut_addon: int = 0
    cut_unrated: int = 0
    cut_old: int = 0
    cut_low_stars: int = 0
    cut_suspect: int = 0
    cut_zero_quantities: int = 0
    cut_dup: int = 0
    kept_recent: int = 0


def _year(recipe: Recipe) -> int | None:
    return recipe.source_created_at.year if recipe.source_created_at else None


def _is_recent(recipe: Recipe, days: int) -> bool:
    if days <= 0 or recipe.source_created_at is None:
        return False
    return (datetime.utcnow() - recipe.source_created_at) <= timedelta(days=days)


def _ratings_count(recipe: Recipe) -> int:
    """Ratings to judge popularity by, preferring the lineage-wide count.

    Falls back to the per-revision count so a database that predates the
    backfill still curates exactly as it did before.
    """
    if recipe.effective_ratings_count is not None:
        return recipe.effective_ratings_count
    return recipe.ratings_count or 0


def _avg_rating(recipe: Recipe) -> float:
    if recipe.effective_rating is not None:
        return recipe.effective_rating
    return recipe.avg_rating or 0.0


def _has_nonzero_ingredient_amount(recipe: Recipe) -> bool:
    return any(
        (line.amount_g is not None and line.amount_g > 0)
        or (line.amount is not None and line.amount > 0)
        for line in recipe.ingredients
    )


def curate(
    session_factory: sessionmaker[Session],
    source: str = "hellofresh",
    rules: CurationRules | None = None,
) -> CurationReport:
    rules = rules or CurationRules()
    report = CurationReport()

    with session_factory() as session:
        recipes = list(session.scalars(select(Recipe).where(Recipe.source == source)))
        report.total = len(recipes)

        keep: list[Recipe] = []
        for r in recipes:
            if not r.is_complete:
                report.cut_incomplete += 1
                continue
            if r.name and BUNDLE_RE.search(r.name):
                report.cut_bundle += 1
                continue
            if r.energy_kcal is not None and r.energy_kcal < MIN_KCAL:
                report.cut_low_kcal += 1
                continue
            if r.macros_suspect:
                report.cut_suspect += 1
                continue
            if not _has_nonzero_ingredient_amount(r):
                report.cut_zero_quantities += 1
                continue
            if rules.drop_addons and r.is_addon:
                report.cut_addon += 1
                continue
            ratings = _ratings_count(r)
            if ratings < rules.min_ratings:
                # Recency exception: newer recipes qualify with fewer ratings.
                if _is_recent(r, rules.recent_days) and ratings >= rules.recent_min_ratings:
                    report.kept_recent += 1
                else:
                    report.cut_unrated += 1
                    continue
            if rules.min_avg_rating and _avg_rating(r) < rules.min_avg_rating:
                report.cut_low_stars += 1
                continue
            if rules.since_year is not None:
                yr = _year(r)
                if yr is None or yr < rules.since_year:
                    report.cut_old += 1
                    continue
            keep.append(r)

        if rules.dedup_versions:
            keep = _dedup_versions(keep, report)

        keep_ids = {r.id for r in keep}
        report.curated = len(keep_ids)

        # Apply the flag in two bulk updates.
        session.execute(update(Recipe).where(Recipe.source == source).values(curated=0))
        if keep_ids:
            session.execute(update(Recipe).where(Recipe.id.in_(keep_ids)).values(curated=1))
        session.commit()

    return report


def _dedup_versions(recipes: list[Recipe], report: CurationReport) -> list[Recipe]:
    """Collapse every revision of a dish down to the one worth cooking.

    Sources revise a dish repeatedly and rename it as they go, so matching on the
    name alone misses most duplicates. ``family_code`` is the source's own dish
    id and catches them exactly; the name is the fallback for the minority of
    rows that carry no usable code.
    """
    groups: dict[str, list[Recipe]] = defaultdict(list)
    for r in recipes:
        key = r.family_code or "name::" + (r.name or "").strip().lower()
        groups[key].append(r)

    result: list[Recipe] = []
    for group in groups.values():
        if len(group) == 1:
            result.append(group[0])
            continue
        # The revision the source still serves wins outright — it is the one
        # whose ingredients and method are current. Otherwise fall back to
        # newest, then most-rated.
        best = max(
            group,
            key=lambda r: (
                1 if r.source_active else 0,
                r.source_created_at or _MIN_DT,
                _ratings_count(r),
                r.id,
            ),
        )
        report.cut_dup += len(group) - 1
        result.append(best)
    return result


_MIN_DT = datetime.min
