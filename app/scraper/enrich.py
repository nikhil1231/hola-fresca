"""Enrich an existing database with derived fields, in place.

Adds the derived columns if missing (so an already-populated DB is upgraded
without a full re-normalise), backfills ingredient units from the corpus,
converts amounts to grams, and computes the per-recipe signals (protein density,
macro sanity, diet suitability). Idempotent and re-runnable.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.canonicalize import backfill_units, to_grams
from app.classify import (
    diet_flags,
    macros_implausible_for_veg,
    macros_suspect,
    protein_energy_ratio,
)
from app.db.models import Recipe

# column name -> SQLite column declaration, added if not already present.
_RECIPE_COLUMNS = {
    "protein_energy_ratio": "REAL",
    "macros_suspect": "INTEGER DEFAULT 0",
    "is_vegetarian": "INTEGER DEFAULT 0",
    "is_pescatarian": "INTEGER DEFAULT 0",
    "is_dairy_free": "INTEGER DEFAULT 0",
    "is_gluten_free": "INTEGER DEFAULT 0",
    "is_low_carb": "INTEGER DEFAULT 0",
}
_INGREDIENT_COLUMNS = {
    "amount_g": "REAL",
    "canonical_unit": "VARCHAR(4)",
}


@dataclass
class EnrichReport:
    units_backfilled: int = 0
    ingredients_gram_resolved: int = 0
    ingredients_total: int = 0
    recipes: int = 0


def _ensure_columns(session: Session, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in session.execute(text(f"PRAGMA table_info({table})"))}
    for name, decl in columns.items():
        if name not in existing:
            session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))
    session.commit()


def enrich(session_factory: sessionmaker[Session]) -> EnrichReport:
    report = EnrichReport()

    with session_factory() as session:
        _ensure_columns(session, "recipes", _RECIPE_COLUMNS)
        _ensure_columns(session, "recipe_ingredients", _INGREDIENT_COLUMNS)

        report.units_backfilled = backfill_units(session)
        session.commit()

        recipes = session.scalars(
            select(Recipe).options(
                selectinload(Recipe.ingredients),
                selectinload(Recipe.allergens),
            )
        )
        for r in recipes:
            report.recipes += 1
            _apply_recipe(r)
            for ing in r.ingredients:
                report.ingredients_total += 1
                grams, unit = to_grams(ing.name, ing.amount, ing.unit)
                ing.amount_g = grams
                ing.canonical_unit = unit
                if grams is not None:
                    report.ingredients_gram_resolved += 1
        session.commit()

    return report


def _apply_recipe(r: Recipe) -> None:
    """Set the derived per-recipe fields on a Recipe row (shared with normalize)."""
    names = [i.name for i in r.ingredients]
    allergens = [a.name for a in r.allergens]
    flags = diet_flags(names, allergens, r.carbs_g, r.energy_kcal)
    r.is_vegetarian = int(flags["is_vegetarian"])
    r.is_pescatarian = int(flags["is_pescatarian"])
    r.is_dairy_free = int(flags["is_dairy_free"])
    r.is_gluten_free = int(flags["is_gluten_free"])
    r.is_low_carb = int(flags["is_low_carb"])
    r.macros_suspect = int(
        macros_suspect(r.protein_g, r.carbs_g, r.fat_g, r.energy_kcal)
        or macros_implausible_for_veg(flags["is_vegetarian"], r.protein_g)
    )
    r.protein_energy_ratio = protein_energy_ratio(r.protein_g, r.energy_kcal)
