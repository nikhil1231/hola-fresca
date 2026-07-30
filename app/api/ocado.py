"""Ocado basket and slot API."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_planner_csv_path, get_session, get_session_factory
from app.api.planner import _basket_out, _load_planner_index, _planner_selection, _require_curated
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
    PushLineOut,
)
from app.ocado.auth import AUTH
from app.ocado.client import OcadoClient
from app.ocado.session import get_shared_session
from app.ocado.sync import push_basket
from app.planner.basket import build_basket
from app.planner.index import PlanIndex

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
    index: PlanIndex = _load_planner_index(factory, recipe_ids, csv_path)
    basket = build_basket(index, [_planner_selection(selection) for selection in body.selections])
    try:
        result = push_basket(client, basket, owned_item_keys=set(body.owned_item_keys))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado basket push failed: {exc}") from exc
    # asdict, not vars: these dataclasses use slots and so have no __dict__.
    return OcadoPushResultOut(
        applied=[PushLineOut(**asdict(line)) for line in result.applied],
        dropped=[PushLineOut(**asdict(line)) for line in result.dropped],
        unmapped=result.unmapped,
        deltas=result.deltas,
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
