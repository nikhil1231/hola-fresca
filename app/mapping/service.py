"""Shared read/write for ingredient mappings, used by the CLI and the API.

Children (``IngredientMappingProduct``) store only the *accepted* products, in
rank order. The full candidate list is always re-derived from the search cache,
so a detail view marks which candidates were accepted by joining the two.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import IngredientMapping, IngredientMappingProduct
from app.mapping import ordering
from app.mapping.candidates import Candidate, IngredientCandidates
from app.retailers import DEFAULT_RETAILER, RETAILER_IDS

#: The shop these functions read when the caller names none. Kept as a module
#: constant so existing call sites keep working; every function that touches a
#: retailer-scoped table takes ``retailer`` and defaults to this.
RETAILER = DEFAULT_RETAILER
VALID_STATUSES = ("proposed", "approved", "rejected", "needs_review", "no_match", "alias")
MATCH_TYPES = ("exact", "substitute", "form_differs")


@dataclass
class AcceptedInput:
    sku: str
    rank: int = 1
    match_type: str = "exact"
    reason: str | None = None
    #: The model's own ordering, kept so the offered order can be recomputed
    #: without another LLM pass. None for a human decision.
    llm_rank: int | None = None


@dataclass
class DecisionInput:
    status: str
    accepted: list[AcceptedInput] = field(default_factory=list)
    each_to_grams: float | None = None
    needs_substitution: bool = False
    pantry_staple: bool = False
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
    pantry_staple: bool
    alias_of: str | None
    each_to_grams: float | None
    top_product_name: str | None
    top_product_rating: float | None
    top_product_ratings_count: int | None


@dataclass
class IngredientDetail:
    ingredient_key: str
    name: str
    status: str | None
    line_count: int
    spend_score: float | None
    each_to_grams: float | None
    needs_substitution: bool
    pantry_staple: bool
    search_term: str | None
    alias_of: str | None
    alias_of_name: str | None
    decided_by: str | None
    model: str | None
    llm_notes: str | None
    reviewer_notes: str | None
    usage: dict
    candidates: list[CandidateView]


def existing_mapping_keys(session: Session, retailer: str = RETAILER) -> set[str]:
    """Keys already represented for ``retailer``.

    Product decisions are retailer-specific, but aliases describe recipe
    ingredients and are shared.  A globally aliased key must therefore never be
    sent through another retailer's product proposal pass.
    """
    return {
        row[0]
        for row in session.execute(
            select(IngredientMapping.ingredient_key).where(
                or_(
                    IngredientMapping.retailer == retailer,
                    IngredientMapping.alias_of.is_not(None),
                )
            )
        )
    }


def _shared_alias_target(session: Session, key: str) -> str | None:
    """Return the retailer-independent alias target for ``key``.

    ``alias_of`` predates multiple catalogues and still lives on each mapping
    row.  Treat those columns as replicated copies of one ingredient fact.  The
    write path below keeps them equal; raising on conflicting legacy data is
    safer than allowing query order to decide which ingredient gets bought.
    """
    targets = list(
        session.scalars(
            select(IngredientMapping.alias_of)
            .where(
                IngredientMapping.ingredient_key == key,
                IngredientMapping.alias_of.is_not(None),
            )
            .distinct()
        )
    )
    if len(targets) > 1:
        raise ValueError(f"conflicting shared aliases for {key!r}: {', '.join(sorted(targets))}")
    return targets[0] if targets else None


def _keep_shared_alias(mapping: IngredientMapping, target: str | None) -> None:
    """Restore alias state after a retailer-specific proposal/decision write."""
    if target is None:
        return
    mapping.alias_of = target
    mapping.status = "alias"
    mapping.decided_by = "human"


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
    _keep_shared_alias(mapping, _shared_alias_target(session, ic.ingredient_key))
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
                llm_rank=a.llm_rank,
                match_type=a.match_type if a.match_type in MATCH_TYPES else "exact",
                accepted=1,
                reason=a.reason,
                source=source,
            )
        )


def write_proposal(
    session: Session,
    ic: IngredientCandidates,
    proposed,
    *,
    model: str,
    retailer: str = RETAILER,
) -> None:
    """Persist an LLM proposal as ``status='proposed'`` (overwrites any prior)."""
    mapping = _upsert_mapping(session, ic, retailer)
    session.flush()
    accepted = [
        AcceptedInput(
            sku=a.sku, rank=a.rank, match_type=a.match_type, reason=a.reason,
            llm_rank=a.llm_rank,
        )
        for a in proposed.accepted
    ]
    _set_children(session, mapping, ic, accepted, source="llm")
    mapping.status = "proposed"
    mapping.decided_by = "llm"
    mapping.model = model
    mapping.llm_notes = proposed.note
    # Only ever filled in, never overwritten. ``derive_count_metadata`` defers to
    # a value that is already there, so a re-proposal that guessed differently
    # would stick — and grams-per-unit is not what a re-run is trying to improve.
    if mapping.each_to_grams is None:
        mapping.each_to_grams = proposed.each_to_grams
    mapping.needs_substitution = 1 if proposed.needs_substitution else 0
    mapping.spend_score = _spend_score(ic, [a.sku for a in accepted])
    _keep_shared_alias(mapping, _shared_alias_target(session, ic.ingredient_key))
    session.commit()


def _candidate_from_product(mp: IngredientMappingProduct) -> Candidate | None:
    """A :class:`Candidate` for an already-accepted product row.

    Reordering only compares the products a mapping already accepted, so it reads
    them straight off the join rather than re-gathering the whole search cache.
    """
    product = mp.product
    if product is None:
        return None
    return Candidate(
        product_id=product.id,
        sku=product.sku,
        name=product.name,
        brand=product.brand,
        pack_size_raw=product.pack_size_raw,
        pack_size_value=product.pack_size_value,
        pack_size_unit=product.pack_size_unit,
        price=product.price,
        base_price=product.base_price,
        unit_price=product.unit_price,
        unit_price_basis=product.unit_price_basis,
        base_unit_price=product.base_unit_price,
        # Treat 0 ratings as "no rating" rather than a real 0.0-star score.
        avg_rating=product.avg_rating if (product.ratings_count or 0) > 0 else None,
        ratings_count=product.ratings_count or None,
        url=product.url,
        result_rank=mp.rank,
        retailer=product.retailer,
        is_frozen=bool(product.is_frozen),
    )


def reorder_proposals(session: Session, *, retailer: str = RETAILER) -> int:
    """Re-sort every LLM proposal's products under the current ordering rules.

    The point of storing ``llm_rank`` — retuning the balance between unit price
    and rating costs a re-sort, not another pass over the model. Only untouched
    LLM proposals are moved: once a human has ordered a mapping, that order is
    theirs. Returns the number of mappings whose order actually changed.
    """
    mappings = session.scalars(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.status == "proposed",
            IngredientMapping.decided_by == "llm",
        )
    ).all()

    changed = 0
    for mapping in mappings:
        children = list(mapping.products)
        if len(children) < 2:
            continue
        candidates = [c for c in (_candidate_from_product(mp) for mp in children) if c]
        ordered = ordering.order_accepted(children, candidates)
        if [mp.sku for mp in ordered] == [mp.sku for mp in sorted(children, key=lambda p: p.rank)]:
            continue
        # (mapping_id, rank) is unique, so the new ranks are parked out of the
        # way before being written; assigning them directly would collide with
        # the rows still holding the old values.
        for i, mp in enumerate(ordered, start=1):
            mp.rank = -i
        session.flush()
        for mp in children:
            mp.rank = -mp.rank
        session.flush()
        mapping.spend_score = _spend_score_from_children(mapping, ordered)
        changed += 1

    session.commit()
    return changed


def _spend_score_from_children(
    mapping: IngredientMapping, ordered: list[IngredientMappingProduct]
) -> float | None:
    """``line_count x top price``, recomputed when reordering moves the top.

    Mirrors :func:`_representative_price`: the new top product's price when it
    has one, and the median of the rest when it does not.
    """
    if mapping.pantry_staple:
        return None
    prices = [mp.product.price for mp in ordered if mp.product and mp.product.price is not None]
    if not prices:
        return None
    top = ordered[0].product
    price = top.price if top is not None and top.price is not None else statistics.median(prices)
    return round(mapping.line_count * price, 2)


def save_decision(
    session: Session, ic: IngredientCandidates, decision: DecisionInput, retailer: str = RETAILER
) -> IngredientMapping:
    """Persist a human review decision."""
    if decision.status not in VALID_STATUSES:
        raise ValueError(f"invalid status {decision.status!r}")
    valid_skus = {c.sku for c in ic.candidates}
    unknown = sorted({a.sku for a in decision.accepted if a.sku not in valid_skus})
    if unknown:
        raise ValueError(f"unknown accepted sku(s): {', '.join(unknown)}")
    # One SKU can only be accepted once per mapping (uq_mapping_product_sku).
    # A payload repeating a SKU keeps its best-ranked entry rather than erroring.
    accepted = []
    seen_skus: set[str] = set()
    for a in sorted(decision.accepted, key=lambda x: x.rank):
        if a.sku not in seen_skus:
            seen_skus.add(a.sku)
            accepted.append(a)
    if decision.status == "approved" and not decision.pantry_staple and not accepted:
        raise ValueError("approved mappings need at least one accepted product unless pantry_staple is true")
    for i, a in enumerate(sorted(accepted, key=lambda x: x.rank), start=1):
        a.rank = i

    mapping = _upsert_mapping(session, ic, retailer)
    session.flush()
    _set_children(session, mapping, ic, accepted, source="human")
    mapping.status = decision.status
    mapping.decided_by = "human"
    mapping.each_to_grams = decision.each_to_grams
    mapping.needs_substitution = 1 if decision.needs_substitution else 0
    mapping.pantry_staple = 1 if decision.pantry_staple else 0
    mapping.reviewer_notes = decision.reviewer_notes
    mapping.spend_score = None if decision.pantry_staple else _spend_score(ic, [a.sku for a in accepted])
    _keep_shared_alias(mapping, _shared_alias_target(session, ic.ingredient_key))
    session.commit()
    return mapping


def resolve_alias(session: Session, key: str, retailer: str = RETAILER) -> str:
    """Follow ``alias_of`` to the canonical ingredient for ``key``.

    Aliases are stored flat (always pointing at a root), but this walks the chain
    defensively with a visited set so hand-edited data can never loop forever.
    """
    seen: set[str] = set()
    current = key
    while current not in seen:
        seen.add(current)
        # Ingredient aliases are independent of the catalogue. ``retailer`` is
        # retained in the signature for compatibility with mapping call sites.
        target = _shared_alias_target(session, current)
        if not target:
            return current
        current = target
    return current


def set_alias(
    session: Session, ingredient_key: str, target_key: str | None, retailer: str = RETAILER
) -> IngredientMapping:
    """Point ``ingredient_key`` at ``target_key`` (or clear the alias when None).

    The alias inherits the target's products, so its own accepted rows are left
    untouched and simply ignored — clearing the alias restores them.
    """
    mapping = session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.ingredient_key == ingredient_key,
        )
    )
    if mapping is None:
        raise ValueError(f"no mapping for {ingredient_key!r}")

    if target_key is None:
        for row in session.scalars(
            select(IngredientMapping).where(
                IngredientMapping.ingredient_key == ingredient_key
            )
        ):
            row.alias_of = None
            # Back into each retailer's review queue: the products it kept are
            # its own again.
            row.status = "proposed"
            row.decided_by = "human"
        session.commit()
        return mapping

    if target_key == ingredient_key:
        raise ValueError("an ingredient cannot be an alias of itself")
    target_exists = session.scalar(
        select(IngredientMapping.id).where(
            IngredientMapping.ingredient_key == target_key
        ).limit(1)
    )
    if target_exists is None:
        raise ValueError(f"no mapping for target {target_key!r}")

    # Point at the target's root so chains stay flat, then reject the link if
    # that root is this ingredient (which would make a cycle).
    root = resolve_alias(session, target_key, retailer)
    if root == ingredient_key:
        raise ValueError("that would create an alias cycle")

    # Materialise the shared relationship for every catalogue. This makes a new
    # retailer inherit aliases immediately, even before that retailer has
    # searched either spelling itself.
    by_retailer = {
        row.retailer: row
        for row in session.scalars(
            select(IngredientMapping).where(
                IngredientMapping.ingredient_key == ingredient_key
            )
        )
    }
    for retailer_id in RETAILER_IDS:
        row = by_retailer.get(retailer_id)
        if row is None:
            row = IngredientMapping(
                retailer=retailer_id,
                ingredient_key=ingredient_key,
                name=mapping.name,
                line_count=mapping.line_count,
            )
            session.add(row)
        _keep_shared_alias(row, root)
    session.commit()
    return mapping


def list_aliases(session: Session, retailer: str = RETAILER) -> list[tuple[str, str, str, str]]:
    """``(alias_key, alias_name, canonical_key, canonical_name)`` for every alias."""
    rows = session.scalars(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.alias_of.is_not(None),
        )
    ).all()
    out = []
    for m in rows:
        out.append(
            (
                m.ingredient_key,
                m.name,
                m.alias_of,
                _name_of(session, m.alias_of, retailer) or m.alias_of,
            )
        )
    return sorted(out, key=lambda r: r[3].lower())


def _name_of(session: Session, key: str, retailer: str = RETAILER) -> str | None:
    name = session.scalar(
        select(IngredientMapping.name).where(
            IngredientMapping.retailer == retailer, IngredientMapping.ingredient_key == key
        )
    )
    if name is not None:
        return name
    # The target may not have reached this retailer's mapping queue yet. Its
    # recipe-facing name is still shared, so fall back to another catalogue.
    return session.scalar(
        select(IngredientMapping.name)
        .where(IngredientMapping.ingredient_key == key)
        .order_by(IngredientMapping.id)
        .limit(1)
    )


def bulk_approve(session: Session, keys: list[str], retailer: str = RETAILER) -> int:
    n = 0
    if not keys:
        return 0
    for mapping in session.scalars(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.ingredient_key.in_(keys),
            IngredientMapping.status == "proposed",
        )
    ):
        if not mapping.pantry_staple and not mapping.products:
            continue
        mapping.status = "approved"
        mapping.decided_by = "human"
        if mapping.pantry_staple:
            mapping.spend_score = None
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
    display_name = ic.name
    if display_name == ic.ingredient_key and mapping is not None:
        display_name = mapping.name
    return IngredientDetail(
        ingredient_key=ic.ingredient_key,
        name=display_name,
        status=mapping.status if mapping else None,
        line_count=ic.line_count,
        spend_score=mapping.spend_score if mapping else None,
        each_to_grams=mapping.each_to_grams if mapping else None,
        needs_substitution=bool(mapping.needs_substitution) if mapping else False,
        pantry_staple=bool(mapping.pantry_staple) if mapping else False,
        search_term=(mapping.search_term if mapping and mapping.search_term else _default_term(ic)),
        alias_of=mapping.alias_of if mapping else None,
        alias_of_name=(
            _name_of(session, mapping.alias_of, retailer)
            if mapping and mapping.alias_of
            else None
        ),
        decided_by=mapping.decided_by if mapping else None,
        model=mapping.model if mapping else None,
        llm_notes=mapping.llm_notes if mapping else None,
        reviewer_notes=mapping.reviewer_notes if mapping else None,
        usage=usage,
        candidates=views,
    )


def _default_term(ic: IngredientCandidates) -> str:
    """The term the batch scrape used — the ingredient's own name."""
    return ic.name


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


def _mapping_filters(stmt, *, status: str | None, q: str | None, retailer: str):
    stmt = stmt.where(IngredientMapping.retailer == retailer)
    if status:
        stmt = stmt.where(IngredientMapping.status == status)
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(IngredientMapping.name.ilike(term), IngredientMapping.ingredient_key.ilike(term))
        )
    return stmt


def count_items(
    session: Session, *, status: str | None = None, q: str | None = None,
    retailer: str = RETAILER,
) -> int:
    stmt = _mapping_filters(
        select(func.count(IngredientMapping.id)), status=status, q=q, retailer=retailer
    )
    return session.scalar(stmt) or 0


def list_items(
    session: Session, *, status: str | None = None, q: str | None = None,
    limit: int | None = None, offset: int = 0, retailer: str = RETAILER
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

    stmt = _mapping_filters(select(IngredientMapping), status=status, q=q, retailer=retailer)
    stmt = stmt.order_by(
        IngredientMapping.spend_score.is_(None), IngredientMapping.spend_score.desc(),
        IngredientMapping.line_count.desc(), IngredientMapping.name.asc(), IngredientMapping.id.asc(),
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)

    items: list[IngredientListItem] = []
    for mapping in session.scalars(stmt):
        accepted = sorted(mapping.products, key=lambda p: p.rank)
        top = accepted[0] if accepted and not mapping.pantry_staple else None
        top_name = None
        top_rating = None
        top_ratings_count = None
        if top is not None:
            top_name = top.product.name if top.product else top.sku
            if top.product is not None and (top.product.ratings_count or 0) > 0:
                top_rating = top.product.avg_rating
                top_ratings_count = top.product.ratings_count
        items.append(
            IngredientListItem(
                ingredient_key=mapping.ingredient_key,
                name=mapping.name,
                status=mapping.status,
                line_count=mapping.line_count,
                spend_score=None if mapping.pantry_staple else mapping.spend_score,
                num_candidates=cand_counts.get(mapping.ingredient_key, 0),
                num_accepted=0 if mapping.pantry_staple else len(accepted),
                needs_substitution=bool(mapping.needs_substitution),
                pantry_staple=bool(mapping.pantry_staple),
                alias_of=mapping.alias_of,
                each_to_grams=mapping.each_to_grams,
                top_product_name=top_name,
                top_product_rating=top_rating,
                top_product_ratings_count=top_ratings_count,
            )
        )
    return items


def list_alias_options(
    session: Session, *, exclude_key: str | None = None, q: str | None = None,
    limit: int = 200, retailer: str = RETAILER,
) -> list[tuple[str, str]]:
    stmt = select(IngredientMapping.ingredient_key, IngredientMapping.name).where(
        IngredientMapping.retailer == retailer,
        IngredientMapping.alias_of.is_(None),
        IngredientMapping.status != "alias",
    )
    if exclude_key:
        stmt = stmt.where(IngredientMapping.ingredient_key != exclude_key)
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(IngredientMapping.name.ilike(term), IngredientMapping.ingredient_key.ilike(term))
        )
    stmt = stmt.order_by(IngredientMapping.name.asc(), IngredientMapping.id.asc()).limit(limit)
    return list(session.execute(stmt).all())
