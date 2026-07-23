"""Ingredient → product mapping review API.

Lists the proposed mappings (spend-weighted), serves one ingredient's full
candidate set with the current decision overlaid, and persists human decisions.
Backs the ``/mapping`` review UI.
"""
from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.schemas import (
    AliasIn,
    AliasListOut,
    AliasOut,
    BulkApproveIn,
    DecisionIn,
    GenerateIn,
    JobOut,
    SearchIn,
    MappingCandidateOut,
    MappingDetailOut,
    MappingListItem,
    MappingListOut,
)
from app.db.models import IngredientMapping
from app.mapping import service
from app.mapping.candidates import UsageStats, gather_candidates, load_usage_stats

router = APIRouter(prefix="/api/mapping", tags=["mapping"])


@lru_cache(maxsize=1)
def _usage_stats() -> dict[str, UsageStats]:
    return load_usage_stats()


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
    session: Session = Depends(get_session),
) -> MappingListOut:
    items = service.list_items(session, status=status)
    counts = dict(
        session.execute(
            select(IngredientMapping.status, func.count()).group_by(IngredientMapping.status)
        ).all()
    )
    return MappingListOut(
        items=[MappingListItem(**vars(i)) for i in items],
        counts=counts,
    )


@router.get("/ingredients/{key}", response_model=MappingDetailOut)
def get_ingredient(key: str, session: Session = Depends(get_session)) -> MappingDetailOut:
    detail = service.get_detail(session, _ic(session, key))
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
    try:
        service.set_alias(session, key, body.alias_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
