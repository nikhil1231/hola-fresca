"""Products sourced by hand, for ingredients no retailer sells.

A dozen or so HelloFresh lines have no Ocado equivalent worth buying — their own
spice blends, truffle zest, proprietary pastes. Leaving those ingredients
unmapped is the worst option available: the recipes using them then price at
zero, so the planner learns to *prefer* them. Recording them as real products
with ``retailer='manual'`` — a pack size, an estimated price, a shelf life —
keeps them in the arithmetic, and only the shopping list has to separate them.

Two things make this cheap rather than invasive:

* Pack covering and waste valuation read ``pack_size_value`` and
  ``shelf_life_days`` without caring who sells the thing, so the planner needs no
  special case beyond printing manual lines under their own heading.
* The candidate *hit* is filed under the mapping's retailer while the *product*
  carries ``manual``. That is deliberate: ``ProductSearchHit.retailer`` names the
  review context an ingredient is being shopped for, so a manual product shows up
  in the normal candidate list and the existing accept/rank UI works untouched.

Manual rows survive re-scraping: ``normalize`` only ever writes ``retailer``
``'ocado'``, and ``(retailer, sku)`` is unique.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    IngredientMapping,
    IngredientMappingProduct,
    Product,
    ProductSearchHit,
)
from app.mapping import service
from app.mapping.candidates import gather_candidates

RETAILER = "manual"
#: The shop a manual product is filed *against*. ``Product.retailer`` stays
#: ``'manual'``; this is the review context the candidate shows up in, and it is
#: per-shop because "Ocado does not sell this" is not a claim about Sainsbury's.
HOST_RETAILER = service.RETAILER

# These exist because they keep — a jar of spice blend outlives any weekly plan —
# so the default states that outright rather than leaving the waste model to
# guess from a missing value.
DEFAULT_SHELF_LIFE_DAYS = 365
CATEGORY = "Manual"

_SKU_PREFIX = "manual:"


def manual_sku(name: str) -> str:
    """Stable sku for a manual product, so re-adding one updates it in place."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return f"{_SKU_PREFIX}{slug}"


def is_manual_sku(sku: str) -> bool:
    return sku.startswith(_SKU_PREFIX)


@dataclass
class ManualProductInput:
    name: str
    price: float
    pack_size_value: float
    pack_size_unit: str = "g"
    brand: str | None = None
    shelf_life_days: int | None = None
    source_note: str | None = None
    url: str | None = None


@dataclass
class ManualProductItem:
    """A manual product plus which ingredients currently shop from it."""

    sku: str
    name: str
    brand: str | None
    pack_size_raw: str | None
    pack_size_value: float | None
    pack_size_unit: str | None
    price: float | None
    shelf_life_days: int | None
    source_note: str | None
    url: str | None
    used_by: list[tuple[str, str]]  # (ingredient_key, ingredient name)


def _pack_size_raw(value: float, unit: str) -> str:
    if unit == "each":
        return f"{value:g} per pack"
    return f"{value:g}{unit}"


def upsert_product(session: Session, data: ManualProductInput) -> Product:
    """Create or update the manual product for ``data.name``."""
    if data.price is None or data.price < 0:
        raise ValueError("a manual product needs a price")
    if not data.pack_size_value or data.pack_size_value <= 0:
        raise ValueError("a manual product needs a pack size")
    if data.pack_size_unit not in ("g", "ml", "each"):
        raise ValueError("pack size unit must be one of g, ml, each")

    sku = manual_sku(data.name)
    product = session.scalar(
        select(Product).where(Product.retailer == RETAILER, Product.sku == sku)
    )
    if product is None:
        product = Product(retailer=RETAILER, sku=sku)
        session.add(product)

    product.name = data.name
    product.brand = data.brand
    product.pack_size_value = data.pack_size_value
    product.pack_size_unit = data.pack_size_unit
    product.pack_size_raw = _pack_size_raw(data.pack_size_value, data.pack_size_unit)
    product.price = data.price
    # Unit price is what the mapping UI sorts and compares on; derive it rather
    # than asking a human to do arithmetic they will get wrong.
    if data.pack_size_unit in ("g", "ml"):
        product.unit_price = round(data.price / data.pack_size_value * 1000, 2)
        product.unit_price_basis = f"per kg" if data.pack_size_unit == "g" else "per litre"
    else:
        product.unit_price = round(data.price / data.pack_size_value, 2)
        product.unit_price_basis = "each"
    product.shelf_life_days = (
        data.shelf_life_days if data.shelf_life_days is not None else DEFAULT_SHELF_LIFE_DAYS
    )
    product.shelf_life_raw = None
    product.category = CATEGORY
    product.in_stock = 1
    # Where to actually buy it, kept on the row so the shopping list can say.
    product.raw_json = data.source_note
    product.url = data.url
    session.flush()
    return product


def attach(
    session: Session,
    ingredient_key: str,
    sku: str,
    *,
    line_count: int = 0,
    retailer: str = HOST_RETAILER,
) -> None:
    """Add a manual product to an ingredient's candidate pool (idempotent)."""
    existing = session.scalar(
        select(ProductSearchHit).where(
            ProductSearchHit.retailer == retailer,
            ProductSearchHit.ingredient_key == ingredient_key,
            ProductSearchHit.sku == sku,
        )
    )
    if existing is not None:
        return
    product = session.scalar(
        select(Product).where(Product.retailer == RETAILER, Product.sku == sku)
    )
    if product is None:
        raise ValueError(f"no manual product {sku!r}")
    # Sort last among candidates: a hand-entered product is a considered choice,
    # not a search result competing on relevance.
    last = session.scalar(
        select(func.max(ProductSearchHit.result_rank)).where(
            ProductSearchHit.retailer == retailer,
            ProductSearchHit.ingredient_key == ingredient_key,
        )
    )
    session.add(
        ProductSearchHit(
            product_id=product.id,
            retailer=retailer,
            ingredient_key=ingredient_key,
            search_term=product.name,
            term_rank=0,
            line_count=line_count,
            sku=sku,
            result_rank=(last or 0) + 1,
        )
    )
    session.flush()


def resolve_ingredient(
    session: Session,
    ingredient_key: str,
    data: ManualProductInput,
    *,
    match_type: str = "exact",
    each_to_grams: float | None = None,
    reviewer_notes: str | None = None,
    usage=None,
    retailer: str = HOST_RETAILER,
) -> IngredientMapping:
    """One-shot: create the product, attach it, accept it, approve the mapping.

    The path the review UI uses for "Ocado does not sell this" — it goes through
    :func:`service.save_decision` so spend score and accepted-product bookkeeping
    stay identical to any other decision.
    """
    product = upsert_product(session, data)
    mapping = session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.ingredient_key == ingredient_key,
        )
    )
    # Carry the ingredient's own name and line count through. Without them, an
    # ingredient with no search hits would be renamed after the product just
    # attached to it, or after its raw key.
    name = mapping.name if mapping is not None else None

    def _gather():
        ic = gather_candidates(
            session, ingredient_key, name=name, usage=usage, retailer=retailer
        )
        if not ic.line_count and mapping is not None:
            ic.line_count = mapping.line_count
        return ic

    ic = _gather()
    attach(
        session, ingredient_key, product.sku, line_count=ic.line_count, retailer=retailer
    )
    ic = _gather()

    # Keep any products already accepted, and put the manual one first: it is the
    # real article, where the retailer alternatives were the compromise.
    #
    # Read them as plain tuples rather than through ``mapping.products``.
    # save_decision replaces the child rows with a Core DELETE, SQLite reuses the
    # freed rowids, and a collection loaded beforehand then collides with the new
    # rows in the identity map — leaving the caller holding stale children.
    accepted = [service.AcceptedInput(sku=product.sku, rank=1, match_type=match_type)]
    if mapping is not None:
        existing = session.execute(
            select(
                IngredientMappingProduct.sku,
                IngredientMappingProduct.match_type,
                IngredientMappingProduct.reason,
            )
            .where(
                IngredientMappingProduct.mapping_id == mapping.id,
                IngredientMappingProduct.accepted == 1,
                IngredientMappingProduct.sku != product.sku,
            )
            .order_by(IngredientMappingProduct.rank)
        ).all()
        for rank, (sku, existing_match, reason) in enumerate(existing, start=2):
            accepted.append(
                service.AcceptedInput(
                    sku=sku, rank=rank, match_type=existing_match, reason=reason
                )
            )

    saved = service.save_decision(
        session,
        ic,
        service.DecisionInput(
            status="approved",
            accepted=accepted,
            each_to_grams=each_to_grams,
            reviewer_notes=reviewer_notes,
        ),
        retailer,
    )
    # The rowid reuse above can leave a previously-loaded collection cached, so
    # force callers to re-read the children they were just handed.
    session.expire(saved, ["products"])
    return saved


def list_products(session: Session) -> list[ManualProductItem]:
    products = session.scalars(
        select(Product).where(Product.retailer == RETAILER).order_by(Product.name)
    ).all()
    usage: dict[str, list[tuple[str, str]]] = {}
    rows = session.execute(
        select(IngredientMappingProduct.sku, IngredientMapping.ingredient_key, IngredientMapping.name)
        .join(IngredientMapping, IngredientMapping.id == IngredientMappingProduct.mapping_id)
        .where(IngredientMappingProduct.accepted == 1)
    ).all()
    for sku, key, name in rows:
        usage.setdefault(sku, []).append((key, name))
    return [
        ManualProductItem(
            sku=p.sku,
            name=p.name,
            brand=p.brand,
            pack_size_raw=p.pack_size_raw,
            pack_size_value=p.pack_size_value,
            pack_size_unit=p.pack_size_unit,
            price=p.price,
            shelf_life_days=p.shelf_life_days,
            source_note=p.raw_json,
            url=p.url,
            used_by=sorted(usage.get(p.sku, [])),
        )
        for p in products
    ]


def delete_product(session: Session, sku: str) -> None:
    """Remove a manual product, refusing while an ingredient still shops from it.

    ``IngredientMappingProduct.product_id`` is ON DELETE SET NULL, so deleting a
    product in use would leave an accepted row pointing at nothing — an ingredient
    that looks mapped and prices at zero. Better to make the reviewer detach it.
    """
    product = session.scalar(
        select(Product).where(Product.retailer == RETAILER, Product.sku == sku)
    )
    if product is None:
        raise ValueError(f"no manual product {sku!r}")
    users = [
        name
        for (name,) in session.execute(
            select(IngredientMapping.name)
            .join(
                IngredientMappingProduct,
                IngredientMappingProduct.mapping_id == IngredientMapping.id,
            )
            .where(
                IngredientMappingProduct.sku == sku,
                IngredientMappingProduct.accepted == 1,
            )
        )
    ]
    if users:
        raise ValueError(f"still used by: {', '.join(sorted(users))}")
    session.execute(
        ProductSearchHit.__table__.delete().where(ProductSearchHit.sku == sku)
    )
    session.delete(product)
    session.commit()
