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
    BulkApproveIn,
    DecisionIn,
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
    ic = gather_candidates(session, key, usage=_usage_stats().get(key))
    if not ic.candidates:
        raise HTTPException(status_code=404, detail="ingredient has no cached candidates")
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


@router.post("/bulk-approve")
def bulk_approve(body: BulkApproveIn, session: Session = Depends(get_session)) -> dict:
    n = service.bulk_approve(session, body.keys)
    return {"approved": n}
