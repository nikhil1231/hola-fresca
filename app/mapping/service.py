"""Shared read/write for ingredient mappings, used by the CLI and the API.

Children (``IngredientMappingProduct``) store only the *accepted* products, in
rank order. The full candidate list is always re-derived from the search cache,
so a detail view marks which candidates were accepted by joining the two.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import IngredientMapping, IngredientMappingProduct
from app.mapping.candidates import Candidate, IngredientCandidates

RETAILER = "ocado"
VALID_STATUSES = ("proposed", "approved", "rejected", "needs_review", "no_match")
MATCH_TYPES = ("exact", "substitute", "form_differs")


@dataclass
class AcceptedInput:
    sku: str
    rank: int = 1
    match_type: str = "exact"
    reason: str | None = None


@dataclass
class DecisionInput:
    status: str
    accepted: list[AcceptedInput] = field(default_factory=list)
    each_to_grams: float | None = None
    needs_substitution: bool = False
    reviewer_notes: str | None = None


@dataclass
class CandidateView:
    candidate: Candidate
    accepted: bool
    rank: int | None
    match_type: str | None
    reason: str | None


@dataclass
class IngredientListItem:
    ingredient_key: str
    name: str
    status: str
    line_count: int
    spend_score: float | None
    num_candidates: int
    num_accepted: int
    needs_substitution: bool
    each_to_grams: float | None
    top_product_name: str | None


@dataclass
class IngredientDetail:
    ingredient_key: str
    name: str
    status: str | None
    line_count: int
    spend_score: float | None
    each_to_grams: float | None
    needs_substitution: bool
    decided_by: str | None
    model: str | None
    llm_notes: str | None
    reviewer_notes: str | None
    usage: dict
    candidates: list[CandidateView]


def existing_mapping_keys(session: Session, retailer: str = RETAILER) -> set[str]:
    return {
        row[0]
        for row in session.execute(
            select(IngredientMapping.ingredient_key).where(
                IngredientMapping.retailer == retailer
            )
        )
    }


def _representative_price(ic: IngredientCandidates, accepted_skus: list[str]) -> float | None:
    by_sku = {c.sku: c for c in ic.candidates}
    if accepted_skus:
        top = by_sku.get(accepted_skus[0])
        if top and top.price is not None:
            return top.price
    prices = [c.price for c in ic.candidates if c.price is not None]
    return statistics.median(prices) if prices else None


def _spend_score(ic: IngredientCandidates, accepted_skus: list[str]) -> float | None:
    price = _representative_price(ic, accepted_skus)
    return round(ic.line_count * price, 2) if price is not None else None


def _upsert_mapping(session: Session, ic: IngredientCandidates, retailer: str) -> IngredientMapping:
    mapping = session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.ingredient_key == ic.ingredient_key,
        )
    )
    if mapping is None:
        mapping = IngredientMapping(retailer=retailer, ingredient_key=ic.ingredient_key)
        session.add(mapping)
    mapping.name = ic.name
    mapping.line_count = ic.line_count
    return mapping


def _set_children(
    session: Session,
    mapping: IngredientMapping,
    ic: IngredientCandidates,
    accepted: list[AcceptedInput],
    source: str,
) -> None:
    session.execute(
        IngredientMappingProduct.__table__.delete().where(
            IngredientMappingProduct.mapping_id == mapping.id
        )
    )
    by_sku = {c.sku: c for c in ic.candidates}
    for a in accepted:
        cand = by_sku.get(a.sku)
        session.add(
            IngredientMappingProduct(
                mapping_id=mapping.id,
                product_id=cand.product_id if cand else None,
                sku=a.sku,
                rank=a.rank,
                match_type=a.match_type if a.match_type in MATCH_TYPES else "exact",
                accepted=1,
                reason=a.reason,
                source=source,
            )
        )


def write_proposal(session: Session, ic: IngredientCandidates, proposed, *, model: str) -> None:
    """Persist an LLM proposal as ``status='proposed'`` (overwrites any prior)."""
    mapping = _upsert_mapping(session, ic, RETAILER)
    session.flush()
    accepted = [
        AcceptedInput(sku=a.sku, rank=a.rank, match_type=a.match_type, reason=a.reason)
        for a in proposed.accepted
    ]
    _set_children(session, mapping, ic, accepted, source="llm")
    mapping.status = "proposed"
    mapping.decided_by = "llm"
    mapping.model = model
    mapping.llm_notes = proposed.note
    mapping.each_to_grams = proposed.each_to_grams
    mapping.needs_substitution = 1 if proposed.needs_substitution else 0
    mapping.spend_score = _spend_score(ic, [a.sku for a in accepted])
    session.commit()


def save_decision(
    session: Session, ic: IngredientCandidates, decision: DecisionInput, retailer: str = RETAILER
) -> IngredientMapping:
    """Persist a human review decision."""
    if decision.status not in VALID_STATUSES:
        raise ValueError(f"invalid status {decision.status!r}")
    valid_skus = {c.sku for c in ic.candidates}
    accepted = [a for a in decision.accepted if a.sku in valid_skus]
    for i, a in enumerate(sorted(accepted, key=lambda x: x.rank), start=1):
        a.rank = i

    mapping = _upsert_mapping(session, ic, retailer)
    session.flush()
    _set_children(session, mapping, ic, accepted, source="human")
    mapping.status = decision.status
    mapping.decided_by = "human"
    mapping.each_to_grams = decision.each_to_grams
    mapping.needs_substitution = 1 if decision.needs_substitution else 0
    mapping.reviewer_notes = decision.reviewer_notes
    mapping.spend_score = _spend_score(ic, [a.sku for a in accepted])
    session.commit()
    return mapping


def bulk_approve(session: Session, keys: list[str], retailer: str = RETAILER) -> int:
    n = 0
    for mapping in session.scalars(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.ingredient_key.in_(keys),
        )
    ):
        mapping.status = "approved"
        mapping.decided_by = "human"
        n += 1
    session.commit()
    return n


def get_detail(session: Session, ic: IngredientCandidates, retailer: str = RETAILER) -> IngredientDetail:
    mapping = session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.ingredient_key == ic.ingredient_key,
        )
    )
    accepted_by_sku: dict[str, IngredientMappingProduct] = {}
    if mapping is not None:
        for child in mapping.products:
            accepted_by_sku[child.sku] = child

    views = [
        CandidateView(
            candidate=c,
            accepted=c.sku in accepted_by_sku,
            rank=accepted_by_sku[c.sku].rank if c.sku in accepted_by_sku else None,
            match_type=accepted_by_sku[c.sku].match_type if c.sku in accepted_by_sku else None,
            reason=accepted_by_sku[c.sku].reason if c.sku in accepted_by_sku else None,
        )
        for c in ic.candidates
    ]
    # Accepted first (by rank), then remaining candidates by search rank.
    views.sort(key=lambda v: (not v.accepted, v.rank or 0, v.candidate.result_rank))

    usage = _usage_dict(ic)
    return IngredientDetail(
        ingredient_key=ic.ingredient_key,
        name=(mapping.name if mapping else ic.name),
        status=mapping.status if mapping else None,
        line_count=ic.line_count,
        spend_score=mapping.spend_score if mapping else None,
        each_to_grams=mapping.each_to_grams if mapping else None,
        needs_substitution=bool(mapping.needs_substitution) if mapping else False,
        decided_by=mapping.decided_by if mapping else None,
        model=mapping.model if mapping else None,
        llm_notes=mapping.llm_notes if mapping else None,
        reviewer_notes=mapping.reviewer_notes if mapping else None,
        usage=usage,
        candidates=views,
    )


def _usage_dict(ic: IngredientCandidates) -> dict:
    if not ic.usage:
        return {"line_count": ic.line_count}
    u = ic.usage
    return {
        "line_count": ic.line_count,
        "metric_unit": u.metric_unit,
        "median": u.median,
        "p25": u.p25,
        "p75": u.p75,
        "common_native_amounts": u.common_native_amounts,
    }


def list_items(
    session: Session, *, status: str | None = None, retailer: str = RETAILER
) -> list[IngredientListItem]:
    # Candidate counts per ingredient from the search cache.
    from app.db.models import ProductSearchHit

    cand_counts = dict(
        session.execute(
            select(ProductSearchHit.ingredient_key, func.count())
            .where(ProductSearchHit.retailer == retailer)
            .group_by(ProductSearchHit.ingredient_key)
        ).all()
    )

    stmt = select(IngredientMapping).where(IngredientMapping.retailer == retailer)
    if status:
        stmt = stmt.where(IngredientMapping.status == status)
    stmt = stmt.order_by(
        IngredientMapping.spend_score.is_(None), IngredientMapping.spend_score.desc()
    )

    items: list[IngredientListItem] = []
    for mapping in session.scalars(stmt):
        accepted = sorted(mapping.products, key=lambda p: p.rank)
        top = accepted[0] if accepted else None
        top_name = None
        if top is not None:
            top_name = top.product.name if top.product else top.sku
        items.append(
            IngredientListItem(
                ingredient_key=mapping.ingredient_key,
                name=mapping.name,
                status=mapping.status,
                line_count=mapping.line_count,
                spend_score=mapping.spend_score,
                num_candidates=cand_counts.get(mapping.ingredient_key, 0),
                num_accepted=len(accepted),
                needs_substitution=bool(mapping.needs_substitution),
                each_to_grams=mapping.each_to_grams,
                top_product_name=top_name,
            )
        )
    return items
