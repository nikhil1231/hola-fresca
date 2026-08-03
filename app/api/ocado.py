"""Ocado basket and slot API."""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
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
    OcadoAccountIn,
    OcadoAccountOut,
    OcadoAccountsOut,
    OcadoBasketOut,
    OcadoLoginOut,
    OcadoOtpIn,
    OcadoPushPlanOut,
    OcadoPushResultOut,
    OcadoReserveIn,
    OcadoReserveOut,
    OcadoSlotOut,
    OcadoSlotsOut,
    OcadoStockRefreshOut,
    OcadoSwapOut,
    PushLineOut,
)
from app.db.models import Product
from app.ocado.availability import mark_unavailable, refresh_stock
from app.ocado.client import OcadoClient
from app.ocado.ledger import read_ledger, write_ledger
from app.ocado.session import (
    OcadoAccountRuntime,
    get_account_runtime,
    get_shared_session,
    list_account_runtimes,
)
from app.ocado.sync import PushLine, plan_push, push_basket
from app.planner.basket import Basket, Selection, build_basket
from app.planner.index import PlanIndex

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ocado", tags=["ocado"])

#: Read cart -> merge -> write cart is not atomic, and the ledger written at the
#: end describes the cart as the *last* writer left it. One live session and one
#: cart, so serialising the whole push is both sufficient and cheap.
_PUSH_LOCK = threading.Lock()


def _runtime(account_id: str | None = None) -> OcadoAccountRuntime:
    try:
        return get_account_runtime(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown Ocado account: {exc.args[0]}") from exc


def get_ocado_client(account_id: str | None = None) -> OcadoClient:
    return OcadoClient(get_shared_session(account_id))


def _login_out(runtime: OcadoAccountRuntime, state: str | None = None) -> OcadoLoginOut:
    return OcadoLoginOut(
        account_id=runtime.account.id,
        status=state if state is not None else runtime.auth.state,
        stage=runtime.auth.stage,
    )


def _client_for_body(
    body_account_id: str | None,
    injected: OcadoClient,
) -> tuple[OcadoAccountRuntime, OcadoClient]:
    runtime = _runtime(body_account_id)
    if body_account_id is None:
        return runtime, injected
    return runtime, OcadoClient(runtime.session)


@router.get("/accounts", response_model=OcadoAccountsOut)
def accounts() -> OcadoAccountsOut:
    runtimes = list_account_runtimes()
    return OcadoAccountsOut(
        default_account_id=runtimes[0].account.id,
        items=[
            OcadoAccountOut(
                id=runtime.account.id,
                label=runtime.account.label,
                email=runtime.account.email,
                status=runtime.auth.state,
            )
            for runtime in runtimes
        ],
    )


@router.get("/status", response_model=OcadoLoginOut)
def status(account_id: str | None = None) -> OcadoLoginOut:
    return _login_out(_runtime(account_id))


@router.post("/login", response_model=OcadoLoginOut)
def login(body: OcadoAccountIn) -> OcadoLoginOut:
    # Deliberately not closed: when this returns AWAITING_OTP the ladder keeps a
    # reference to this session for the /otp call that follows.
    runtime = _runtime(body.account_id)
    try:
        state = runtime.auth.ensure_authenticated(runtime.session)
    except Exception as exc:  # noqa: BLE001 - browser/login failures surface as bad gateway
        raise HTTPException(status_code=502, detail=f"Ocado login failed: {exc}") from exc
    return _login_out(runtime, state)


@router.post("/session/refresh", response_model=OcadoLoginOut)
def refresh_session(body: OcadoAccountIn) -> OcadoLoginOut:
    """Become ready if that is possible without asking the user anything.

    Safe to call automatically on page load: it stops before the password step,
    which would email an OTP to someone who only opened the page.
    """
    runtime = _runtime(body.account_id)
    try:
        state = runtime.auth.ensure_authenticated(runtime.session, allow_login=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado session refresh failed: {exc}") from exc
    return _login_out(runtime, state)


@router.post("/otp", response_model=OcadoLoginOut)
def otp(body: OcadoOtpIn) -> OcadoLoginOut:
    runtime = _runtime(body.account_id)
    try:
        state = runtime.auth.submit_otp(body.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado OTP failed: {exc}") from exc
    return _login_out(runtime, state)


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
    overrides: dict[str, str] | None = None,
    snap_overrides: dict[str, bool] | None = None,
) -> tuple[PlanIndex, Basket]:
    index: PlanIndex = _load_planner_index(factory, recipe_ids, csv_path)
    return index, build_basket(index, selections, pack_overrides=overrides, snap_overrides=snap_overrides)


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
    index, basket = _rebuild(factory, recipe_ids, csv_path, selections, body.pack_overrides, body.snap_overrides)
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


def _out_lines(
    factory: sessionmaker[Session], lines: list[PushLine]
) -> list[PushLineOut]:
    """Push lines with a product name filled in wherever HF has none.

    Which is exactly your own items: they never came out of a basket cover, so
    nothing upstream knows what they are called. The catalogue usually does, and
    "2 x Cathedral City Mature" beats a UUID in a report whose whole job is to
    show you what was left alone.
    """
    missing = sorted({line.sku for line in lines if not line.name})
    names: dict[str, str] = {}
    if missing:
        with factory() as session:
            names = dict(
                session.execute(
                    select(Product.sku, Product.name)
                    .where(Product.sku.in_(missing))
                    .where(Product.retailer == "ocado")
                ).all()
            )
    # asdict, not vars: these dataclasses use slots and so have no __dict__.
    return [
        PushLineOut(**{**asdict(line), "name": line.name or names.get(line.sku)})
        for line in lines
    ]


@router.post("/basket/plan", response_model=OcadoPushPlanOut)
def plan(
    body: BasketIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    injected_client: OcadoClient = Depends(get_ocado_client),
) -> OcadoPushPlanOut:
    """What a push would change, without changing it.

    Deliberately cheaper than the push: no stock check, so it covers from the
    cached catalogue and can name a pack the push then substitutes away from.
    The question it answers - what of yours gets touched - does not depend on
    which pack of sesame seeds wins.
    """
    recipe_ids = list(dict.fromkeys(s.recipe_id for s in body.selections))
    _require_curated(session, recipe_ids)
    selections = [_planner_selection(selection) for selection in body.selections]
    _, basket = _rebuild(factory, recipe_ids, csv_path, selections, body.pack_overrides, body.snap_overrides)
    runtime, client = _client_for_body(body.account_id, injected_client)
    ledger = read_ledger(factory, account_id=runtime.account.id)
    try:
        result = plan_push(
            client, basket, ledger=ledger, owned_item_keys=set(body.owned_item_keys)
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado basket plan failed: {exc}") from exc
    return OcadoPushPlanOut(
        added=_out_lines(factory, result.added),
        removed=_out_lines(factory, result.removed),
        restored=_out_lines(factory, result.restored),
        yours=_out_lines(factory, result.yours),
        unmapped=result.unmapped,
        deltas=result.deltas,
        synced=ledger.synced,
        synced_at=ledger.synced_at,
        synced_week_start=ledger.week_start,
    )


@router.post("/basket/push", response_model=OcadoPushResultOut)
def push(
    body: BasketIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    injected_client: OcadoClient = Depends(get_ocado_client),
) -> OcadoPushResultOut:
    recipe_ids = list(dict.fromkeys(s.recipe_id for s in body.selections))
    _require_curated(session, recipe_ids)
    selections = [_planner_selection(selection) for selection in body.selections]
    index, basket = _rebuild(factory, recipe_ids, csv_path, selections, body.pack_overrides, body.snap_overrides)

    # Check the shelves before filling the trolley. The write-back moves the
    # database file, which is what makes the rebuild below see the new stock and
    # cover around anything that has sold out since the last scrape.
    _refresh_basket_stock(factory, index, basket)
    index, basket = _rebuild(factory, recipe_ids, csv_path, selections, body.pack_overrides, body.snap_overrides)

    def recover(skus: list[str]) -> Basket | None:
        """Believe the cart over the catalogue, then cover the week again."""
        if not mark_unavailable(factory, skus):
            return None
        return _rebuild(factory, recipe_ids, csv_path, selections, body.pack_overrides, body.snap_overrides)[1]

    with _PUSH_LOCK:
        runtime, client = _client_for_body(body.account_id, injected_client)
        try:
            result = push_basket(
                client,
                basket,
                ledger=read_ledger(factory, account_id=runtime.account.id),
                owned_item_keys=set(body.owned_item_keys),
                recover=recover,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Ocado basket push failed: {exc}") from exc
        # Inside the lock: the ledger describes the cart the push just left, and
        # a second push reading it before this write would merge against a stale
        # claim and buy the week twice.
        write_ledger(factory, result.ledger, account_id=runtime.account.id, week_start=body.week_start)

    pushed = result.basket or basket
    return OcadoPushResultOut(
        applied=_out_lines(factory, result.applied),
        dropped=_out_lines(factory, result.dropped),
        unmapped=result.unmapped,
        deltas=result.deltas,
        yours=_out_lines(factory, result.yours),
        restored=_out_lines(factory, result.restored),
        removed=_out_lines(factory, result.removed),
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
    account_id: str | None = None,
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
    injected_client: OcadoClient = Depends(get_ocado_client),
) -> OcadoReserveOut:
    _, client = _client_for_body(body.account_id, injected_client)
    try:
        payload = client.reserve(body.slot_id, ddid=body.ddid, region=body.region)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado slot reserve failed: {exc}") from exc
    return OcadoReserveOut(raw=payload)
