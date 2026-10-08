"""The Ocado endpoints that are Ocado's alone.

Everything a shop with a cart has in common — sessions, login, OTP, the basket
plan and push — moved to ``/api/cart/{retailer}`` when Sainsbury's grew a
trolley of its own (:mod:`app.api.cart`). What is left here are the two things
no other shop has an equivalent of:

* **delivery slots.** Sainsbury's has a slot API too, but nothing in the app
  talks to it yet, and inventing a retailer-neutral slot endpoint that only one
  shop can answer would be a worse lie than this module's name.
* **search and the trolley by hand.** ``/search``, ``/basket`` and
  ``/basket/items`` are for a person (or Noodle, on their behalf) shopping
  directly, outside the meal plan. A line set here is the person's own as far as
  the push ledger is concerned, exactly as if they'd added it on ocado.com.
* **the auth-event log.** It measures how long Ocado's browser-driven sessions
  survive, which is a question about that ladder specifically. Sainsbury's
  answers it with a refresh token and has nothing to count.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_session, require_admin
from app.api.schemas import (
    OcadoAuthAccountSummaryOut,
    OcadoAuthEventOut,
    OcadoAuthEventsOut,
    OcadoReserveIn,
    OcadoReserveOut,
    OcadoSlotOut,
    OcadoSlotsOut,
)
from app.db import retailer_accounts
from app.api.cart import _push_lock
from app.db.models import OcadoAuthEvent, Product, RetailerAccount, User
from app import household
from app.ocado import amend
from app.ocado import orders as ocado_orders
from app.ocado import shop
from app.ocado.availability import fetch_statuses
from app.ocado.client import OcadoClient
from app.ocado.session import get_shared_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ocado", tags=["ocado"])


def get_ocado_client(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> OcadoClient:
    """The signed-in caller's Ocado session, or 404.

    A slot is booked against a delivery address and paid for by a card, both of
    which belong to whoever's account holds the session — so this resolves the
    account the same way the cart endpoints do rather than taking an id from the
    request. It used to accept ``?account_id=``, which meant anyone could read,
    and reserve, somebody else's delivery slots.
    """
    account = retailer_accounts.find(session, user.id, "ocado")
    if account is None:
        raise HTTPException(
            status_code=404, detail="No Ocado account is connected for this user"
        )
    return OcadoClient(get_shared_session(account.key))


@router.get("/auth-events", response_model=OcadoAuthEventsOut)
def auth_events(
    days: int = 90,
    limit: int = 200,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> OcadoAuthEventsOut:
    """What the auth ladder has been doing, and what it implies.

    Admin-gated: it spans every account, and "when did this person's Ocado
    connection last work" is not something one user should read about another.

    The summary is the point. A high ``silent_per_login`` means the browser
    profile's upstream SSO session is carrying the app for long stretches and an
    interactive login is a rare chore; a low one means somebody is being asked to
    log in constantly, and the design that assumes otherwise does not hold.
    """
    days = max(1, min(days, 3650))
    limit = max(1, min(limit, 1000))
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = list(
        session.scalars(
            select(OcadoAuthEvent)
            .where(OcadoAuthEvent.created_at >= since)
            .order_by(OcadoAuthEvent.created_at.desc())
        )
    )

    by_account: dict[str, list[OcadoAuthEvent]] = defaultdict(list)
    for row in rows:
        by_account[row.account_id].append(row)

    summaries: list[OcadoAuthAccountSummaryOut] = []
    for account_id in sorted(by_account):
        # Oldest first, so "consecutive logins" means what it says.
        events = sorted(by_account[account_id], key=lambda item: item.created_at)
        silent_ok = [e for e in events if e.rung == "silent" and e.outcome == "ok"]
        logins = [e for e in events if e.rung == "login" and e.outcome == "ok"]
        successes = [e for e in events if e.outcome == "ok"]

        stretch_hours: float | None = None
        if len(logins) >= 2:
            gaps = [
                (later.created_at - earlier.created_at).total_seconds() / 3600
                for earlier, later in zip(logins, logins[1:])
            ]
            stretch_hours = round(max(gaps), 1)

        summaries.append(
            OcadoAuthAccountSummaryOut(
                account_id=account_id,
                silent_ok=len(silent_ok),
                logins=len(logins),
                silent_per_login=(
                    round(len(silent_ok) / len(logins), 2) if logins else None
                ),
                last_ok_at=successes[-1].created_at if successes else None,
                last_login_at=logins[-1].created_at if logins else None,
                longest_stretch_hours=stretch_hours,
            )
        )

    return OcadoAuthEventsOut(
        since=since,
        accounts=summaries,
        events=[
            OcadoAuthEventOut(
                account_id=row.account_id,
                rung=row.rung,
                outcome=row.outcome,
                trigger=row.trigger,
                detail=row.detail,
                duration_ms=row.duration_ms,
                created_at=row.created_at,
            )
            for row in rows[:limit]
        ],
    )


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


@router.get("/orders")
def orders(pending: bool = False, client: OcadoClient = Depends(get_ocado_client)) -> dict:
    """The caller's orders, newest first: when each arrives, until when it can be
    edited, what it costs. ``pending`` keeps only those still to come."""
    try:
        raw = client.orders(pending=pending)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado orders fetch failed: {exc}") from exc
    return {"orders": [asdict(ocado_orders.summarise(o)) for o in raw]}


class BasketLineIn(BaseModel):
    sku: str
    #: Absolute: 0 takes the line out. Absolute rather than a delta so a retried
    #: request can't add the same thing twice.
    quantity: int = Field(ge=0, le=50)


class BasketChangeIn(BaseModel):
    items: list[BasketLineIn] = Field(min_length=1, max_length=100)


@router.get("/search")
def search(q: str, limit: int = 20, client: OcadoClient = Depends(get_ocado_client)) -> dict:
    """Ocado's own search, priced for the caller's delivery region."""
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Search for something")
    limit = max(1, min(limit, 50))
    try:
        payload = client.search(q, size=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado search failed: {exc}") from exc
    return {"query": q, "products": [asdict(p) for p in shop.search_results(payload, limit)]}


def _basket_out(client: OcadoClient, session: Session, payload: dict | None = None) -> dict:
    def names(skus: list[str]) -> dict[str, str]:
        known = dict(session.execute(
            select(Product.sku, Product.name).where(Product.retailer == "ocado", Product.sku.in_(skus))).all())
        missing = [sku for sku in skus if sku not in known]
        if missing:
            try:
                known |= {sku: s.name for sku, s in fetch_statuses(missing, session=client.session).items() if s.name}
            except Exception as exc:  # noqa: BLE001 - a nameless line still says what it costs
                log.info("ocado basket: naming %d unknown skus failed: %s", len(missing), exc)
        return known

    try:
        payload = payload or client.cart_view()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ocado basket fetch failed: {exc}") from exc
    return asdict(shop.basket(payload, names))


@router.get("/basket")
def basket(client: OcadoClient = Depends(get_ocado_client), session: Session = Depends(get_session)) -> dict:
    """The live trolley: named lines, total, and whether Ocado will let it check out."""
    return _basket_out(client, session)


@router.post("/basket/items")
def set_basket_items(body: BasketChangeIn, client: OcadoClient = Depends(get_ocado_client),
                     session: Session = Depends(get_session)) -> dict:
    """Set lines to absolute quantities; ``quantity: 0`` removes one."""
    wanted = {line.sku: line.quantity for line in body.items}
    with _push_lock("ocado"):  # not alongside a plan push reading the same cart
        try:
            client.set_quantities(wanted)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Ocado basket change failed: {exc}") from exc
    return _basket_out(client, session)


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


# ---- the household's orders, and moving one --------------------------------------
# One person's Ocado often holds the household's shop, so these look across the
# accounts the caller may act on (their own, then the household's: app.household),
# find the order there, and act with that account's session.
class MoveSlotIn(BaseModel):
    slot_id: str
    #: Moving a delivery is ocado.com's own "Confirm changes": not something to do by accident.
    confirm: bool = False


def _household_clients(session: Session, user: User) -> list[tuple[RetailerAccount, OcadoClient]]:
    return [(account, OcadoClient(get_shared_session(account.key)))
            for account in household.accounts(session, user, "ocado")]


def _order_client(session: Session, user: User, order_id: str) -> tuple[OcadoClient, dict]:
    for _account, client in _household_clients(session, user):
        try:
            order = amend.find_order(client, order_id)
        except Exception as exc:  # noqa: BLE001 - a dead session is one account, not all of them
            log.info("ocado: orders for %s not read: %s", _account.key, exc)
            continue
        if order is not None:
            return client, order
    raise HTTPException(status_code=404, detail=f"Order {order_id} isn't a coming order on any account you can manage")


@router.get("/household/orders")
def household_orders(session: Session = Depends(get_session), user: User = Depends(get_current_user)) -> dict:
    """Coming orders on every account the caller may manage, each with whose it is."""
    out = []
    for account, client in _household_clients(session, user):
        try:
            orders = [asdict(ocado_orders.summarise(o)) for o in client.orders(pending=True)]
            out.append({"account": account.key, "email": account.email, "orders": orders})
        except Exception as exc:  # noqa: BLE001
            out.append({"account": account.key, "email": account.email, "orders": [], "error": str(exc)})
    return {"accounts": out}


@router.get("/orders/{order_id}/slots", response_model=OcadoSlotsOut)
def order_slots(order_id: str, days: int = 7, session: Session = Depends(get_session),
                user: User = Depends(get_current_user)) -> OcadoSlotsOut:
    """The slots a coming order could move to."""
    client, order = _order_client(session, user, order_id)
    with _push_lock("ocado"):  # the edit session takes over the cart for a moment
        try:
            items = amend.slots_for(client, order, days=min(max(days, 1), 14))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Ocado slot fetch failed: {exc}") from exc
    return OcadoSlotsOut(items=[OcadoSlotOut(**asdict(slot)) for slot in items])


@router.post("/orders/{order_id}/slot")
def move_order_slot(order_id: str, body: MoveSlotIn, session: Session = Depends(get_session),
                    user: User = Depends(get_current_user)) -> dict:
    """Move a coming order to another slot (this delivery only, for a recurring
    order). On any failure the order is left exactly as it was."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail='Moving a delivery needs {"confirm": true}')
    client, _order = _order_client(session, user, order_id)
    with _push_lock("ocado"):
        try:
            moved = amend.move_slot(client, order_id, body.slot_id)
        except amend.OrderChangeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    log.info("ocado: order %s moved to %s", order_id, moved.delivery_start)
    return asdict(moved)
