"""Re-read cached payloads to refresh source fields on existing recipe rows.

The normalize stage replaces a recipe wholesale, which is right when the parse
itself has changed but wrong when all that is needed is a field the parser used
to discard: replacing the row would also discard everything a person has since
attached to it (audit edits, personal ratings, the curation flag).

This pass updates only the source-stated columns listed below, in place. It is
idempotent, needs no network, and leaves every derived and human-owned column
alone. Use it after teaching an adapter to read a new field; use ``normalize
--force`` only when the recipe content itself needs rebuilding.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.classify import effective_ratings
from app.db.models import Recipe
from app.db.session import ensure_columns
from app.scraper import storage
from app.scraper.sources.base import RecipeSource

# column name -> SQLite declaration, added if this DB predates them.
_RECIPE_COLUMNS = {
    "aggregate_rating": "REAL",
    "aggregate_ratings_count": "INTEGER",
    "effective_rating": "REAL",
    "effective_ratings_count": "INTEGER",
    "unique_recipe_code": "VARCHAR(64)",
    "family_code": "VARCHAR(64)",
    "cloned_from": "VARCHAR(64)",
    "source_active": "INTEGER DEFAULT 0",
    "source_published": "INTEGER DEFAULT 0",
}


@dataclass
class BackfillReport:
    examined: int = 0
    updated: int = 0
    missing_raw: int = 0
    errors: int = 0
    ratings_corrected: int = 0
    families_resolved: int = 0


def backfill_source_fields(
    source: RecipeSource, session_factory: sessionmaker[Session]
) -> BackfillReport:
    report = BackfillReport()

    with session_factory() as session:
        ensure_columns(session, "recipes", _RECIPE_COLUMNS)
        source_ids = list(
            session.scalars(select(Recipe.source_id).where(Recipe.source == source.name))
        )

    for source_id in source_ids:
        report.examined += 1
        try:
            payload = storage.read_raw(source.name, source_id)
        except FileNotFoundError:
            report.missing_raw += 1
            continue
        try:
            parsed = source.normalize(payload, url="")
        except Exception:  # noqa: BLE001 - one bad payload must not abort the run
            report.errors += 1
            continue

        with session_factory() as session:
            row = session.scalar(
                select(Recipe).where(
                    Recipe.source == source.name, Recipe.source_id == source_id
                )
            )
            if row is None:
                continue
            if _apply(row, parsed, report):
                report.updated += 1
                session.commit()

    return report


def _apply(row: Recipe, parsed, report: BackfillReport) -> bool:
    rating, count = effective_ratings(
        parsed.avg_rating,
        parsed.ratings_count,
        parsed.aggregate_rating,
        parsed.aggregate_ratings_count,
    )
    updates = {
        # The per-revision counters are restated from the payload too: where the
        # row and the payload disagree the payload is the record of what was
        # scraped, and the row has drifted.
        "avg_rating": parsed.avg_rating,
        "ratings_count": parsed.ratings_count,
        "favorites_count": parsed.favorites_count,
        "aggregate_rating": parsed.aggregate_rating,
        "aggregate_ratings_count": parsed.aggregate_ratings_count,
        "effective_rating": rating,
        "effective_ratings_count": count,
        "unique_recipe_code": parsed.unique_recipe_code,
        "family_code": parsed.family_code,
        "cloned_from": parsed.cloned_from,
        "source_active": 1 if parsed.source_active else 0,
        "source_published": 1 if parsed.source_published else 0,
    }

    if (row.ratings_count or 0) != (parsed.ratings_count or 0):
        report.ratings_corrected += 1
    if parsed.family_code and row.family_code != parsed.family_code:
        report.families_resolved += 1

    changed = False
    for field, value in updates.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    return changed
