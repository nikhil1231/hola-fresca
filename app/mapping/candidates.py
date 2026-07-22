"""Assemble the inputs the mapping LLM (and reviewer) sees for one ingredient.

For each canonical ingredient we combine two sources: the cached retailer
product candidates (``ProductSearchHit`` → ``Product``) and the ingredient's
real-world usage across the recipe library (median/quartile grams, common native
amounts) from ``data/ingredient_frequency.csv``. Usage grounds the pack-size
judgement — "potatoes, ~450 g/recipe" should not map to a single loose potato.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db.models import Product, ProductSearchHit

RETAILER = "ocado"


@dataclass(frozen=True)
class UsageStats:
    line_count: int
    metric_unit: str | None = None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    common_native_amounts: str | None = None
    name_variants: str | None = None


@dataclass(frozen=True)
class Candidate:
    product_id: int
    sku: str
    name: str
    brand: str | None
    pack_size_raw: str | None
    pack_size_value: float | None
    pack_size_unit: str | None
    price: float | None
    unit_price: float | None
    unit_price_basis: str | None
    avg_rating: float | None
    ratings_count: int | None
    url: str | None
    result_rank: int
    search_term: str | None = None


@dataclass
class IngredientCandidates:
    ingredient_key: str
    name: str
    line_count: int
    usage: UsageStats | None
    candidates: list[Candidate] = field(default_factory=list)


def _f(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def load_usage_stats(csv_path: Path | None = None) -> dict[str, UsageStats]:
    """Map ``ingredient_key`` → usage stats from the frequency CSV."""
    path = csv_path or (config.DATA_DIR / "ingredient_frequency.csv")
    stats: dict[str, UsageStats] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            stats[row["ingredient_key"]] = UsageStats(
                line_count=int(row["line_count"]),
                metric_unit=row.get("metric_unit") or None,
                median=_f(row.get("median_metric_amount")),
                p25=_f(row.get("p25_metric_amount")),
                p75=_f(row.get("p75_metric_amount")),
                common_native_amounts=row.get("common_native_amounts") or None,
                name_variants=row.get("name_variants") or None,
            )
    return stats


def load_source_id_index(csv_path: Path | None = None) -> dict[str, str]:
    """Map each source ingredient id → its ``ingredient_key`` (for coverage)."""
    path = csv_path or (config.DATA_DIR / "ingredient_frequency.csv")
    index: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = row["ingredient_key"]
            for sid in (row.get("source_ingredient_ids") or "").split("|"):
                sid = sid.strip()
                if sid:
                    index[sid] = key
    return index


def gather_candidates(
    session: Session,
    ingredient_key: str,
    *,
    name: str | None = None,
    usage: UsageStats | None = None,
    retailer: str = RETAILER,
) -> IngredientCandidates:
    rows = session.execute(
        select(ProductSearchHit, Product)
        .join(Product, ProductSearchHit.product_id == Product.id)
        .where(
            ProductSearchHit.retailer == retailer,
            ProductSearchHit.ingredient_key == ingredient_key,
        )
        .order_by(ProductSearchHit.result_rank)
    ).all()

    candidates: list[Candidate] = []
    display_name = name
    line_count = usage.line_count if usage else 0
    for hit, product in rows:
        display_name = display_name or hit.search_term
        line_count = line_count or hit.line_count
        candidates.append(
            Candidate(
                product_id=product.id,
                sku=product.sku,
                name=product.name,
                brand=product.brand,
                pack_size_raw=product.pack_size_raw,
                pack_size_value=product.pack_size_value,
                pack_size_unit=product.pack_size_unit,
                price=product.price,
                unit_price=product.unit_price,
                unit_price_basis=product.unit_price_basis,
                # Treat 0 ratings as "no rating" rather than a real 0.0★ score.
                avg_rating=product.avg_rating if (product.ratings_count or 0) > 0 else None,
                ratings_count=product.ratings_count or None,
                url=product.url,
                result_rank=hit.result_rank,
                search_term=hit.search_term,
            )
        )
    return IngredientCandidates(
        ingredient_key=ingredient_key,
        name=display_name or ingredient_key,
        line_count=line_count,
        usage=usage,
        candidates=candidates,
    )


def iter_worklist(
    session: Session,
    *,
    csv_path: Path | None = None,
    limit: int | None = None,
    retailer: str = RETAILER,
) -> list[IngredientCandidates]:
    """All ingredients that have retailer candidates, richest usage first.

    Driving the worklist off ``product_search_hits`` naturally excludes the
    non-shipped pantry lines (water, "olive oil for the dressing", …) which
    returned no products.
    """
    usage_by_key = load_usage_stats(csv_path)
    key_rows = session.execute(
        select(
            ProductSearchHit.ingredient_key,
            ProductSearchHit.search_term,
            ProductSearchHit.line_count,
        )
        .where(ProductSearchHit.retailer == retailer)
        .group_by(ProductSearchHit.ingredient_key)
        .order_by(ProductSearchHit.line_count.desc())
    ).all()

    result: list[IngredientCandidates] = []
    for key, term, line_count in key_rows:
        ic = gather_candidates(
            session, key, name=term, usage=usage_by_key.get(key), retailer=retailer
        )
        if not ic.line_count:
            ic.line_count = line_count
        result.append(ic)
        if limit is not None and len(result) >= limit:
            break
    return result
