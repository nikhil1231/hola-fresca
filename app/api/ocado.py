"""Ocado basket and slot API."""
from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_planner_csv_path, get_session, get_session_factory
from app.api.planner import (
    _load_planner_index,
    _planner_selection,
    _require_curated,
    _round_money,
    _stock_checked_at,
)
from app.api.schemas import (
    BasketIn,
    OcadoBasketOut,
    OcadoLoginOut,
    OcadoOtpIn,
    OcadoPushResultOut,
    OcadoReserveIn,
    OcadoReserveOut,
    OcadoSlotOut,
    OcadoSlotsOut,
    OcadoStockRefreshOut,
    OcadoSwapOut,
    PushLineOut,
)
from app.ocado.auth import AUTH
from app.ocado.availability import mark_unavailable, refresh_stock
from app.ocado.client import OcadoClient
from app.ocado.session import get_shared_session
from app.ocado.sync import push_basket
from app.planner.basket import Basket, Selection, build_basket
from app.planner.index import PlanIndex

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ocado", tags=["ocado"])


def get_ocado_client() -> OcadoClient:
    return OcadoClient(get_shared_session())


@router.get("/status", response_model=OcadoLoginOut)
def status() -> OcadoLoginOut:
    return OcadoLoginOut(status=AUTH.state)


@router.post("/login", response_model=OcadoLoginOut)
def login() -> OcadoLoginOut:
    # Deliberately not closed: when this returns AWAITING_OTP the ladder keeps a
    # reference to this session for the /otp call that follows.
    session = get_shared_session()
    try:
        state = AUTH.ensure_authenticated(session)
    except Exception as exc:  # noqa: BLE001 - browser/login failures surface as bad gateway
        raise HTTPException(status_code=502, detail=f"Ocado login failed: {exc}") from exc
    return OcadoLoginOut(status=state)


@router.post("/session/refresh", response_model=OcadoLoginOut)
def refresh_session() -> OcadoLoginOut:
    """Become ready if that is possible without asking the user anything.

    Safe to call automatically on page load: it stops before the password step,
    which would email an OTP to someone who only opened the page.
    """
    session = get_shared_session()
    try:
        state = AUTH.ensure_authenticated(session, allow_login=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado session refresh failed: {exc}") from exc
    return OcadoLoginOut(status=state)


@router.post("/otp", response_model=OcadoLoginOut)
def otp(body: OcadoOtpIn) -> OcadoLoginOut:
    try:
        state = AUTH.submit_otp(body.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado OTP failed: {exc}") from exc
    return OcadoLoginOut(status=state)


def _candidate_skus(index: PlanIndex, basket: Basket) -> list[str]:
    """Every product the basket's ingredients are allowed to be covered from.

    Not just the packs it chose: a substitute is only reachable if its stock is
    known, and one marked sold out weeks ago never comes back without being
    asked again. Checking the whole shortlist is what lets the planner move
    between them.
    """
    skus: list[str] = []
    keys = {line.key for line in basket.lines}
    for key in sorted(keys):
        ingredient = index.ingredient(key)
        if ingredient is None:
            continue
        skus.extend(pack.sku for pack in ingredient.packs if not pack.external)
    return list(dict.fromkeys(skus))


def _rebuild(
    factory: sessionmaker[Session],
    recipe_ids: list[int],
    csv_path: Path | None,
    selections: list[Selection],
) -> tuple[PlanIndex, Basket]:
    index: PlanIndex = _load_planner_index(factory, recipe_ids, csv_path)
    return index, build_basket(index, selections)


def _refresh_basket_stock(
    factory: sessionmaker[Session],
    index: PlanIndex,
    basket: Basket,
) -> None:
    """Best-effort live stock read. A failure here must not block the push.

    Ocado being unreachable is a reason to shop from a stale catalogue, not a
    reason to refuse to shop - the push that follows will find out the hard way
    and recover from that instead.
    """
    try:
        refresh_stock(factory, _candidate_skus(index, basket))
    except Exception as exc:  # noqa: BLE001
        log.warning("ocado stock refresh failed, pushing from the cached catalogue: %s", exc)


def _swaps(basket: Basket) -> list[OcadoSwapOut]:
    return [
        OcadoSwapOut(
            ingredient=line.name,
            ingredient_key=line.key,
            from_products=list(line.substitution.displaced),
            to_products=[choice.pack.product_name for choice in line.cover.choices],
            cost_delta=_round_money(line.substitution.cost_delta),
            tier_changed=line.substitution.tier_changed,
        )
        for line in basket.substituted_lines
        if line.cover is not None and line.substitution is not None
    ]


@router.post("/stock/refresh", response_model=OcadoStockRefreshOut)
def stock_refresh(
    body: BasketIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
) -> OcadoStockRefreshOut:
    """Re-read stock and price for everything this basket could be covered from.

    Needs no Ocado login: the products endpoint answers an anonymous session, so
    this works from the basket page whether or not you have signed in yet.
    """
    recipe_ids = list(dict.fromkeys(s.recipe_id for s in body.selections))
    _require_curated(session, recipe_ids)
    selections = [_planner_selection(selection) for selection in body.selections]
    index, basket = _rebuild(factory, recipe_ids, csv_path, selections)
    try:
        result = refresh_stock(factory, _candidate_skus(index, basket))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado stock check failed: {exc}") from exc
    return OcadoStockRefreshOut(
        checked_at=result.checked_at,
        checked=result.checked,
        available=result.available,
        sold_out=result.sold_out,
        restocked=result.restocked,
        repriced=result.repriced,
        changed=result.changed,
    )


@router.post("/basket/push", response_model=OcadoPushResultOut)
def push(
    body: BasketIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    client: OcadoClient = Depends(get_ocado_client),
) -> OcadoPushResultOut:
    recipe_ids = list(dict.fromkeys(s.recipe_id for s in body.selections))
    _require_curated(session, recipe_ids)
    selections = [_planner_selection(selection) for selection in body.selections]
    index, basket = _rebuild(factory, recipe_ids, csv_path, selections)

    # Check the shelves before filling the trolley. The write-back moves the
    # database file, which is what makes the rebuild below see the new stock and
    # cover around anything that has sold out since the last scrape.
    _refresh_basket_stock(factory, index, basket)
    index, basket = _rebuild(factory, recipe_ids, csv_path, selections)

    def recover(skus: list[str]) -> Basket | None:
        """Believe the cart over the catalogue, then cover the week again."""
        if not mark_unavailable(factory, skus):
            return None
        return _rebuild(factory, recipe_ids, csv_path, selections)[1]

    try:
        result = push_basket(
            client,
            basket,
            owned_item_keys=set(body.owned_item_keys),
            recover=recover,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado basket push failed: {exc}") from exc

    pushed = result.basket or basket
    # asdict, not vars: these dataclasses use slots and so have no __dict__.
    return OcadoPushResultOut(
        applied=[PushLineOut(**asdict(line)) for line in result.applied],
        dropped=[PushLineOut(**asdict(line)) for line in result.dropped],
        unmapped=result.unmapped,
        deltas=result.deltas,
        swaps=_swaps(pushed),
        sold_out=pushed.sold_out,
        stock_checked_at=_stock_checked_at(pushed),
    )


@router.get("/basket", response_model=OcadoBasketOut)
def basket(client: OcadoClient = Depends(get_ocado_client)) -> OcadoBasketOut:
    try:
        return OcadoBasketOut(raw=client.cart_view())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado basket fetch failed: {exc}") from exc


@router.get("/slots", response_model=OcadoSlotsOut)
def slots(
    ddid: str | None = None,
    region: str | None = None,
    client: OcadoClient = Depends(get_ocado_client),
) -> OcadoSlotsOut:
    try:
        items = client.slots(ddid=ddid, region=region)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado slot fetch failed: {exc}") from exc
    return OcadoSlotsOut(items=[OcadoSlotOut(**asdict(slot)) for slot in items])


@router.post("/slots/reserve", response_model=OcadoReserveOut)
def reserve(
    body: OcadoReserveIn,
    client: OcadoClient = Depends(get_ocado_client),
) -> OcadoReserveOut:
    try:
        payload = client.reserve(body.slot_id, ddid=body.ddid, region=body.region)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado slot reserve failed: {exc}") from exc
    return OcadoReserveOut(raw=payload)
