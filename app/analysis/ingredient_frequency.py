"""Rank ingredients by recipe frequency and typical per-recipe amounts.

This is an offline sizing tool for the product-mapping phase. It intentionally
uses SQLite directly instead of the ORM so it can analyze historical scrape
databases whose schema may lag behind the current SQLAlchemy models.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Iterable

from app import config
from app.canonicalize import to_grams


@dataclass(frozen=True)
class IngredientFrequency:
    rank: int
    ingredient_key: str
    source_ingredient_ids: str
    name: str
    recipe_count: int
    recipe_pct: float
    line_count: int
    metric_unit: str
    metric_known_pct: float
    median_metric_amount: float | None
    mean_metric_amount: float | None
    p25_metric_amount: float | None
    p75_metric_amount: float | None
    common_native_amounts: str
    name_variants: str


@dataclass(frozen=True)
class AnalysisResult:
    rows: list[IngredientFrequency]
    recipe_count: int
    ingredient_line_count: int
    library_filter: str
    output_path: Path


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _recipe_filter(conn: sqlite3.Connection, library: str) -> tuple[str, str]:
    columns = _columns(conn, "recipes")
    if library == "curated":
        if "curated" not in columns:
            raise ValueError("recipes.curated is not present in this database")
        return "r.curated = 1", "curated"
    if library == "complete":
        if "is_complete" not in columns:
            raise ValueError("recipes.is_complete is not present in this database")
        return "r.is_complete = 1", "complete"
    if library == "all":
        return "1 = 1", "all"
    if "curated" in columns:
        return "r.curated = 1", "curated"
    if "is_complete" in columns:
        return "r.is_complete = 1", "complete"
    return "1 = 1", "all"


def _normalize_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _ingredient_key(source_ingredient_id: str | None, name: str, group_by: str) -> str:
    if group_by == "source-id" and source_ingredient_id:
        return f"hf:{source_ingredient_id}"
    return f"name:{_normalize_name(name)}"


def _fmt_amount(amount: float | None, unit: str | None) -> str | None:
    if amount is None:
        return None
    if float(amount).is_integer():
        amount_text = str(int(amount))
    else:
        amount_text = f"{amount:.1f}".rstrip("0").rstrip(".")
    return f"{amount_text} {unit or 'unknown'}"


def _percent(part: int, whole: int) -> float:
    return round(100 * part / whole, 2) if whole else 0.0


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _round_optional(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _modal_units(conn: sqlite3.Connection) -> dict[str, str]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for ingredient_id, unit in conn.execute(
        """
        SELECT source_ingredient_id, unit
        FROM recipe_ingredients
        WHERE source_ingredient_id IS NOT NULL
          AND source_ingredient_id != ''
          AND unit IS NOT NULL
          AND unit != ''
        """
    ):
        counts[ingredient_id][unit] += 1
    return {ingredient_id: counter.most_common(1)[0][0] for ingredient_id, counter in counts.items()}


def _top_text(counter: Counter[str], limit: int = 3) -> str:
    return " | ".join(f"{value} ({count})" for value, count in counter.most_common(limit))


def analyze_ingredients(
    db_path: Path = config.DB_PATH,
    output_path: Path = config.DATA_DIR / "ingredient_frequency.csv",
    *,
    library: str = "auto",
    group_by: str = "name",
) -> AnalysisResult:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where_sql, library_filter = _recipe_filter(conn, library)
    modal_units = _modal_units(conn)

    recipe_count = conn.execute(f"SELECT COUNT(*) FROM recipes r WHERE {where_sql}").fetchone()[0]
    sql = f"""
        SELECT
            ri.recipe_id,
            ri.source_ingredient_id,
            ri.name,
            ri.amount,
            ri.unit
        FROM recipe_ingredients ri
        JOIN recipes r ON r.id = ri.recipe_id
        WHERE {where_sql}
    """

    recipe_ids_by_key: dict[str, set[int]] = defaultdict(set)
    names_by_key: dict[str, Counter[str]] = defaultdict(Counter)
    native_amounts_by_key: dict[str, Counter[str]] = defaultdict(Counter)
    source_ids_by_key: dict[str, set[str]] = defaultdict(set)
    metric_totals: dict[str, dict[tuple[int, str], float]] = defaultdict(lambda: defaultdict(float))
    line_counts: Counter[str] = Counter()
    ingredient_line_count = 0

    for row in conn.execute(sql):
        ingredient_line_count += 1
        recipe_id = int(row["recipe_id"])
        source_id = row["source_ingredient_id"] or ""
        name = row["name"]
        key = _ingredient_key(source_id, name, group_by)
        unit = row["unit"] or modal_units.get(source_id)
        amount = row["amount"]

        recipe_ids_by_key[key].add(recipe_id)
        names_by_key[key][name] += 1
        native = _fmt_amount(amount, unit)
        if native:
            native_amounts_by_key[key][native] += 1
        if source_id:
            source_ids_by_key[key].add(source_id)
        line_counts[key] += 1

        metric_amount, metric_unit = to_grams(name, amount, unit)
        if metric_amount is not None and metric_unit:
            metric_totals[key][(recipe_id, metric_unit)] += metric_amount

    rows: list[IngredientFrequency] = []
    sorted_keys = sorted(
        recipe_ids_by_key,
        key=lambda key: (-len(recipe_ids_by_key[key]), names_by_key[key].most_common(1)[0][0].lower()),
    )
    for rank, key in enumerate(sorted_keys, start=1):
        recipe_total = len(recipe_ids_by_key[key])
        metric_counter = Counter(unit for _, unit in metric_totals[key])
        metric_unit = metric_counter.most_common(1)[0][0] if metric_counter else ""
        metric_values = [
            amount
            for (_, unit), amount in metric_totals[key].items()
            if unit == metric_unit
        ]
        rows.append(
            IngredientFrequency(
                rank=rank,
                ingredient_key=key,
                source_ingredient_ids=" | ".join(sorted(source_ids_by_key.get(key, set()))),
                name=names_by_key[key].most_common(1)[0][0],
                recipe_count=recipe_total,
                recipe_pct=_percent(recipe_total, recipe_count),
                line_count=line_counts[key],
                metric_unit=metric_unit,
                metric_known_pct=_percent(len(metric_values), recipe_total),
                median_metric_amount=_round_optional(median(metric_values)) if metric_values else None,
                mean_metric_amount=_round_optional(mean(metric_values)) if metric_values else None,
                p25_metric_amount=_round_optional(_quantile(metric_values, 0.25)),
                p75_metric_amount=_round_optional(_quantile(metric_values, 0.75)),
                common_native_amounts=_top_text(native_amounts_by_key[key]),
                name_variants=_top_text(names_by_key[key]),
            )
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(IngredientFrequency.__dataclass_fields__))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)

    return AnalysisResult(
        rows=rows,
        recipe_count=recipe_count,
        ingredient_line_count=ingredient_line_count,
        library_filter=library_filter,
        output_path=output_path,
    )


def _print_summary(result: AnalysisResult, limit: int) -> None:
    metric_rows = sum(1 for row in result.rows if row.metric_unit)
    print(
        f"Analyzed {result.recipe_count:,} recipes using the {result.library_filter} library "
        f"({result.ingredient_line_count:,} ingredient lines)."
    )
    print(
        f"Grouped into {len(result.rows):,} ingredients; "
        f"{metric_rows:,} have at least one metric quantity. Wrote {result.output_path}."
    )
    print()
    print("rank, ingredient, recipes, pct, typical")
    for row in result.rows[:limit]:
        typical = ""
        if row.median_metric_amount is not None:
            typical = f"{row.median_metric_amount:g}{row.metric_unit} median"
        print(f"{row.rank:>4}  {row.name:<34} {row.recipe_count:>5}  {row.recipe_pct:>5.1f}%  {typical}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.analysis.ingredient_frequency",
        description="Rank recipe ingredients by frequency and typical quantity.",
    )
    parser.add_argument("--db", type=Path, default=config.DB_PATH)
    parser.add_argument("--output", type=Path, default=config.DATA_DIR / "ingredient_frequency.csv")
    parser.add_argument(
        "--library",
        choices=("auto", "curated", "complete", "all"),
        default="auto",
        help="recipe subset to analyze; auto prefers curated, then complete, then all",
    )
    parser.add_argument(
        "--group-by",
        choices=("name", "source-id"),
        default="name",
        help="name gives a canonical mapping worklist; source-id keeps exact HelloFresh IDs separate",
    )
    parser.add_argument("--limit", type=int, default=30, help="number of top rows to print")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = analyze_ingredients(args.db, args.output, library=args.library, group_by=args.group_by)
    _print_summary(result, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
