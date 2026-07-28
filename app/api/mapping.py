"""Ingredient → product mapping review API.

Lists the proposed mappings (spend-weighted), serves one ingredient's full
candidate set with the current decision overlaid, and persists human decisions.
Backs the ``/mapping`` review UI.
"""
from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_session
from app.api.schemas import (
    AliasIn,
    AliasListOut,
    AliasOptionOut,
    AliasOptionsOut,
    AliasOut,
    BulkApproveIn,
    CatalogueAttachIn,
    CatalogueAttachOut,
    CatalogueMatchListOut,
    CatalogueMatchOut,
    CatalogueStatusOut,
    DecisionIn,
    GenerateIn,
    JobOut,
    MappingStatsOut,
    SearchIn,
    MappingCandidateOut,
    MappingDetailOut,
    MappingListItem,
    MappingListOut,
    ManualProductIn,
    ManualProductListOut,
    ManualProductOut,
    ManualResolveIn,
)
from app.db.models import IngredientMapping, Recipe, RecipeIngredient
from app.mapping import service
from app.api.recipes import _to_card
from app.media import image_url
from app.mapping.candidates import (
    UsageStats,
    gather_candidates,
    load_source_id_index,
    load_usage_stats,
)

router = APIRouter(prefix="/api/mapping", tags=["mapping"])


@lru_cache(maxsize=1)
def _usage_stats() -> dict[str, UsageStats]:
    return load_usage_stats()


def _source_ids_for_key(key: str) -> list[str]:
    return [
        source_id
        for source_id, mapped_key in load_source_id_index().items()
        if mapped_key == key
    ]


def _ingredient_icon_url(session: Session, source_ids: list[str]) -> str | None:
    if not source_ids:
        return None
    image_path = session.scalar(
        select(RecipeIngredient.image_path)
        .where(
            RecipeIngredient.source_ingredient_id.in_(source_ids),
            RecipeIngredient.image_path.is_not(None),
        )
        .order_by(RecipeIngredient.id.asc())
        .limit(1)
    )
    return image_url(image_path, 160)


def _example_recipes(session: Session, source_ids: list[str], limit: int = 4):
    if not source_ids:
        return []
    rows = session.scalars(
        select(Recipe)
        .join(RecipeIngredient, RecipeIngredient.recipe_id == Recipe.id)
        .where(Recipe.curated == 1, RecipeIngredient.source_ingredient_id.in_(source_ids))
        .options(selectinload(Recipe.cuisines), selectinload(Recipe.tags))
        .order_by(
            Recipe.ratings_count.desc().nullslast(),
            Recipe.avg_rating.desc().nullslast(),
            Recipe.id,
        )
        .distinct()
        .limit(limit)
    ).all()
    return [_to_card(recipe) for recipe in rows]


def _ic(session: Session, key: str):
    # Having no cached candidates is a legitimate state — a search that found
    # nothing, or a pantry line filed without one — so those still open, letting
    # the reviewer reach the re-search box and fix them by hand. Only a key the
    # system has never heard of (no mapping row, no candidates, not in the
    # frequency data) is a genuine 404.
    usage = _usage_stats().get(key)
    ic = gather_candidates(session, key, usage=usage)
    if not ic.candidates and usage is None:
        known = session.scalar(
            select(IngredientMapping.id).where(IngredientMapping.ingredient_key == key)
        )
        if known is None:
            raise HTTPException(status_code=404, detail="unknown ingredient")
    return ic


@router.get("/ingredients", response_model=MappingListOut)
def list_ingredients(
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> MappingListOut:
    offset = (page - 1) * page_size
    items = service.list_items(session, status=status, q=q, limit=page_size, offset=offset)
    total = service.count_items(session, status=status, q=q)
    counts = dict(
        session.execute(
            select(IngredientMapping.status, func.count()).group_by(IngredientMapping.status)
        ).all()
    )
    return MappingListOut(
        items=[MappingListItem(**vars(i)) for i in items],
        counts=counts,
        total=total,
        page=page,
        page_size=page_size,
        has_more=offset + len(items) < total,
    )


@router.get("/alias-options", response_model=AliasOptionsOut)
def alias_options(
    exclude: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> AliasOptionsOut:
    return AliasOptionsOut(
        items=[
            AliasOptionOut(ingredient_key=k, name=n)
            for k, n in service.list_alias_options(session, exclude_key=exclude, q=q, limit=limit)
        ]
    )


@router.get("/ingredients/{key}", response_model=MappingDetailOut)
def get_ingredient(key: str, session: Session = Depends(get_session)) -> MappingDetailOut:
    detail = service.get_detail(session, _ic(session, key))
    source_ids = _source_ids_for_key(key)
    candidates = [
        MappingCandidateOut(
            **vars(v.candidate),
            accepted=v.accepted,
            rank=v.rank,
            match_type=v.match_type,
            reason=v.reason,
        )
        for v in detail.candidates
    ]
    return MappingDetailOut(
        ingredient_key=detail.ingredient_key,
        name=detail.name,
        ingredient_icon_url=_ingredient_icon_url(session, source_ids),
        status=detail.status,
        line_count=detail.line_count,
        spend_score=detail.spend_score,
        each_to_grams=detail.each_to_grams,
        needs_substitution=detail.needs_substitution,
        pantry_staple=detail.pantry_staple,
        search_term=detail.search_term,
        alias_of=detail.alias_of,
        alias_of_name=detail.alias_of_name,
        decided_by=detail.decided_by,
        model=detail.model,
        llm_notes=detail.llm_notes,
        reviewer_notes=detail.reviewer_notes,
        usage=detail.usage,
        example_recipes=_example_recipes(session, source_ids),
        candidates=candidates,
    )


@router.post("/ingredients/{key}", response_model=MappingDetailOut)
def save_ingredient(
    key: str, body: DecisionIn, session: Session = Depends(get_session)
) -> MappingDetailOut:
    ic = _ic(session, key)
    decision = service.DecisionInput(
        status=body.status,
        accepted=[
            service.AcceptedInput(sku=a.sku, rank=a.rank, match_type=a.match_type, reason=a.reason)
            for a in body.accepted
        ],
        each_to_grams=body.each_to_grams,
        needs_substitution=body.needs_substitution,
        pantry_staple=body.pantry_staple,
        reviewer_notes=body.reviewer_notes,
    )
    try:
        service.save_decision(session, ic, decision)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_ingredient(key, session)


@router.post("/ingredients/{key}/search", response_model=MappingDetailOut)
def search_ingredient(
    key: str, body: SearchIn, session: Session = Depends(get_session)
) -> MappingDetailOut:
    """Re-search Ocado with the reviewer's own wording and merge the results.

    Widens the candidate pool rather than replacing it, so an earlier good match
    is never lost. Runs a real browser session, so it is slow (seconds) and
    deliberately one-at-a-time.
    """
    from app.mapping import live_search

    try:
        live_search.search_and_store(session, key, body.term)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - browser/network failures are expected
        raise HTTPException(status_code=502, detail=f"Ocado search failed: {exc}") from exc
    return get_ingredient(key, session)


@router.get("/stats", response_model=MappingStatsOut)
def get_stats(session: Session = Depends(get_session)) -> MappingStatsOut:
    """Headline progress: how much of the recipe library the mapping can price."""
    from contextlib import contextmanager

    from app.mapping import coverage as coverage_mod
    from app.mapping import generate as generate_mod

    # coverage_report wants a session factory; hand it this request's session
    # (without closing it) so the endpoint honours the injected dependency.
    @contextmanager
    def _request_session():
        yield session

    rep = coverage_mod.coverage_report(_request_session)
    counts = dict(
        session.execute(
            select(IngredientMapping.status, func.count()).group_by(IngredientMapping.status)
        ).all()
    )
    remaining = len(generate_mod.pending_worklist(session, count=100_000))
    return MappingStatsOut(
        lines_total=rep.lines_total,
        lines_resolved=rep.lines_resolved,
        lines_pct=round(rep.pct, 1),
        distinct_keys=rep.distinct_keys,
        resolved_keys=rep.resolved_keys,
        mappings_total=sum(counts.values()),
        approved=counts.get("approved", 0),
        remaining_to_add=remaining,
    )


@router.get("/aliases", response_model=AliasListOut)
def list_aliases(session: Session = Depends(get_session)) -> AliasListOut:
    return AliasListOut(
        items=[
            AliasOut(ingredient_key=k, name=n, alias_of=t, alias_of_name=tn)
            for k, n, t, tn in service.list_aliases(session)
        ]
    )


@router.post("/ingredients/{key}/alias", response_model=MappingDetailOut)
def set_alias(
    key: str, body: AliasIn, session: Session = Depends(get_session)
) -> MappingDetailOut:
    """Link this ingredient to another (or clear the link when alias_of is null)."""
    from app.api.deps import _session_factory
    from app.planner.index import derive_count_metadata

    try:
        service.set_alias(session, key, body.alias_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Aliasing moves this ingredient's recipe lines under a different root, which
    # is exactly the evidence the counted/weighed classification is drawn from.
    derive_count_metadata(_session_factory())
    return get_ingredient(key, session)


@router.post("/generate", response_model=JobOut)
def start_generate(body: GenerateIn) -> JobOut:
    """Pull the next batch of ingredients into the review queue, in the background.

    Slow (an Ocado search and an LLM call each), so this returns a job handle to
    poll rather than blocking the request.
    """
    from app.api.deps import _session_factory
    from app.mapping import generate as generate_mod

    try:
        job = generate_mod.start_background(_session_factory(), count=body.count)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobOut(**job.as_dict())


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str) -> JobOut:
    from app.mapping import generate as generate_mod

    job = generate_mod.REGISTRY.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return JobOut(**job.as_dict())


@router.post("/bulk-approve")
def bulk_approve(body: BulkApproveIn, session: Session = Depends(get_session)) -> dict:
    n = service.bulk_approve(session, body.keys)
    return {"approved": n}


# --------------------------------------------------------------------------
# Manually sourced products
# --------------------------------------------------------------------------

def _manual_out(item) -> ManualProductOut:
    return ManualProductOut(
        **{k: v for k, v in vars(item).items() if k != "used_by"},
        used_by=[{"ingredient_key": k, "name": n} for k, n in item.used_by],
    )


@router.get("/manual-products", response_model=ManualProductListOut)
def list_manual_products(session: Session = Depends(get_session)) -> ManualProductListOut:
    from app.mapping import manual

    return ManualProductListOut(items=[_manual_out(i) for i in manual.list_products(session)])


@router.post("/manual-products", response_model=ManualProductListOut)
def save_manual_product(
    body: ManualProductIn, session: Session = Depends(get_session)
) -> ManualProductListOut:
    """Create or update a manual product (keyed on its name)."""
    from app.mapping import manual

    try:
        manual.upsert_product(session, manual.ManualProductInput(**body.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return list_manual_products(session)


@router.delete("/manual-products/{sku:path}", response_model=ManualProductListOut)
def delete_manual_product(sku: str, session: Session = Depends(get_session)) -> ManualProductListOut:
    from app.mapping import manual

    try:
        manual.delete_product(session, sku)
    except ValueError as exc:
        # In-use is the common case and is the reviewer's to resolve, not an error
        # in the request itself.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return list_manual_products(session)


@router.post("/ingredients/{key}/manual", response_model=MappingDetailOut)
def resolve_with_manual_product(
    key: str, body: ManualResolveIn, session: Session = Depends(get_session)
) -> MappingDetailOut:
    """"Ocado does not sell this" — record what you buy instead and approve it."""
    from app.mapping import manual

    payload = body.model_dump()
    match_type = payload.pop("match_type")
    each_to_grams = payload.pop("each_to_grams")
    reviewer_notes = payload.pop("reviewer_notes")
    try:
        manual.resolve_ingredient(
            session,
            key,
            manual.ManualProductInput(**payload),
            match_type=match_type,
            each_to_grams=each_to_grams,
            reviewer_notes=reviewer_notes,
            usage=_usage_stats().get(key),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_ingredient(key, session)


@router.post("/ingredients/{key}/manual/{sku:path}", response_model=MappingDetailOut)
def attach_manual_product(
    key: str, sku: str, session: Session = Depends(get_session)
) -> MappingDetailOut:
    """Offer an existing manual product as a candidate for another ingredient."""
    from app.mapping import manual

    try:
        manual.attach(session, key, sku)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    session.commit()
    return get_ingredient(key, session)


# --------------------------------------------------------------------------
# Specialist-retailer catalogue (Seasoned Pioneers)
# --------------------------------------------------------------------------

@router.get("/catalogue/status", response_model=CatalogueStatusOut)
def catalogue_status(session: Session = Depends(get_session)) -> CatalogueStatusOut:
    """Is the catalogue synced, and how old is the snapshot behind it?"""
    from app.scraper.products import catalogue as catalogue_mod
    from app.scraper.products.seasoned_pioneers import RETAILER as CATALOGUE_RETAILER

    from contextlib import contextmanager

    @contextmanager
    def _request_session():
        yield session

    counts = catalogue_mod.status_counts(_request_session)
    snapshot = counts.get("snapshot") or {}
    return CatalogueStatusOut(
        retailer=CATALOGUE_RETAILER,
        products=counts["products"],
        in_stock=counts["in_stock"],
        captured_at=snapshot.get("captured_at"),
        snapshot_product_count=snapshot.get("product_count"),
        source=snapshot.get("source"),
    )


@router.get("/ingredients/{key}/catalogue", response_model=CatalogueMatchListOut)
def preview_catalogue_matches(
    key: str,
    q: str | None = Query(default=None, description="score against this wording instead"),
    min_score: float | None = Query(default=None, ge=0.0, le=1.0),
    limit: int = Query(default=8, ge=1, le=50),
    session: Session = Depends(get_session),
) -> CatalogueMatchListOut:
    """What the catalogue would offer for this ingredient, attaching nothing."""
    from app.mapping import external

    ic = _ic(session, key)
    matches = external.match_products(
        session,
        external.resolve_name(session, key, name=q, fallback=ic.name),
        limit=limit,
        min_score=min_score if min_score is not None else external.MIN_SCORE,
    )
    return CatalogueMatchListOut(items=[CatalogueMatchOut(**vars(m)) for m in matches])


@router.post("/ingredients/{key}/catalogue", response_model=MappingDetailOut)
def attach_catalogue_matches(
    key: str,
    q: str | None = Query(default=None, description="match against this wording instead"),
    min_score: float | None = Query(default=None, ge=0.0, le=1.0),
    limit: int = Query(default=4, ge=1, le=20),
    session: Session = Depends(get_session),
) -> MappingDetailOut:
    """Add the catalogue's best matches to this ingredient's candidate pool.

    Asked for by name, so the seasoning guard that governs the bulk pass does not
    apply — a reviewer looking at "Harissa Paste" and wanting to see the dry
    harissa should get it.
    """
    from app.mapping import external

    ic = _ic(session, key)
    external.attach_matches(
        session,
        key,
        name=external.resolve_name(session, key, name=q, fallback=ic.name),
        limit=limit,
        min_score=min_score if min_score is not None else external.MIN_SCORE,
    )
    session.commit()
    return get_ingredient(key, session)


@router.post("/catalogue/attach", response_model=CatalogueAttachOut)
def attach_catalogue_across_queue(
    body: CatalogueAttachIn, session: Session = Depends(get_session)
) -> CatalogueAttachOut:
    """Offer catalogue matches across the whole review queue in one pass.

    Cheap enough to run inline — matching is string work over a few hundred
    cached products, not a network call per ingredient — so unlike ``/generate``
    this needs no job handle.
    """
    from app.mapping import external

    result = external.attach_all(
        session,
        seasonings_only=body.seasonings_only,
        include_approved=body.include_approved,
        min_score=body.min_score if body.min_score is not None else external.MIN_SCORE,
        limit=body.limit if body.limit is not None else external.DEFAULT_LIMIT,
        usage=_usage_stats() if body.seasonings_only else None,
    )
    return CatalogueAttachOut(
        considered=result.considered,
        ingredients_matched=result.ingredients_matched,
        hits_added=result.hits_added,
        skipped_not_seasoning=result.skipped_not_seasoning,
        notes=result.notes,
    )
