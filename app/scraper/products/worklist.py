"""Ingredient-frequency worklist for retailer product searches."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from app import config


@dataclass(frozen=True)
class IngredientWorkItem:
    rank: int
    ingredient_key: str
    name: str
    line_count: int


def load_worklist(path: Path | None = None, *, limit: int = 250) -> list[IngredientWorkItem]:
    csv_path = path or (config.DATA_DIR / "ingredient_frequency.csv")
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = sorted(csv.DictReader(f), key=lambda row: int(row["rank"]))

    items: list[IngredientWorkItem] = []
    for row in rows[:limit]:
        items.append(
            IngredientWorkItem(
                rank=int(row["rank"]),
                ingredient_key=row["ingredient_key"],
                name=row["name"],
                line_count=int(row["line_count"]),
            )
        )
    return items

