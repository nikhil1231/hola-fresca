"""``GET /api/noodle/feed``: what Noodle, the owner's organiser, can see of the household's
shopping; and ``POST /api/noodle/shopping``: "add oat milk to the shopping", which puts
it on the next Ocado order and confirms the change (:mod:`app.ocado.top_up`). Noodle reaches it through the tunnel with a Cloudflare Access
service token (listed in ``HOLAFRESCA_ACCESS_SERVICES``), or over the LAN with
``HOLAFRESCA_NOODLE_FEED_TOKEN``.

The contract is shared by every system that feeds Noodle:

    {"context": "<plain text for answering questions>",
     "upcoming": [{"title", "at" (ISO 8601), "kind", "link"?}]}

``context`` is read by the model when someone asks Noodle a question; ``upcoming``
lands in Noodle's Coming up list and its morning summary. It covers every
connected account in the household (one person's Ocado often holds the shared
order), labelled by whose it is, and nobody outside it (:mod:`app.household`). An account that fails is reported in
``context`` and skipped, never fails the feed.
"""
from __future__ import annotations

import hmac
import logging
from datetime import date, datetime
import threading
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, household
from app.api import access
from app.api.cart import _push_lock
from app.api.deps import get_current_user, get_session
from app.api.hellofresh import client_for as hellofresh_client
from app.api.hellofresh import coming_boxes
from app.db.models import RetailerAccount, User
from app.hellofresh import boxes
from app.ocado import amend, purchases, resolver, top_up
from app.ocado import orders as ocado_orders
from app.ocado.client import OcadoClient
from app.ocado.session import get_shared_session

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/noodle", tags=["noodle"])

HELLOFRESH_WEEKS = 3


def require_feed_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    """The feed token, or a listed Access service (Noodle through the tunnel)."""
    identity = access.authenticated_identity(request)
    if identity is not None and identity.service:
        return
    token = config.NOODLE_FEED_TOKEN
    if not token:
        raise HTTPException(status_code=503, detail="HOLAFRESCA_NOODLE_FEED_TOKEN is not set")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="Bad token")


def ocado_section(account: RetailerAccount, whose: str, client: OcadoClient | None = None) -> tuple[list[str], list[dict]]:
    client = client or OcadoClient(get_shared_session(account.key))
    lines, upcoming = [], []
    for order in (ocado_orders.summarise(o) for o in client.orders(pending=True)):
        if order.status in ("CANCELLED",):
            continue
        cost = f"£{order.total}" if order.total and order.currency == "GBP" else order.total or "?"
        lines.append(f"- Ocado order {order.order_id} ({whose}): {order.items} items, {cost}, arriving "
                     f"{_when(order.delivery_start)}–{_clock(order.delivery_end)}, status {order.status}"
                     + (f", editable until {_when(order.edit_until)}" if order.editable and order.edit_until else "")
                     + (", repeats weekly" if order.recurring else ""))
        if order.delivery_start:
            upcoming.append({"title": f"Ocado delivery ({order.items} items, {cost})", "at": order.delivery_start,
                             "kind": "delivery"})
        if order.editable and order.edit_until:
            upcoming.append({"title": "Ocado order: last chance to edit", "at": order.edit_until, "kind": "deadline"})
    return lines or [f"- No Ocado orders coming ({whose})"], upcoming


def hellofresh_section(account: RetailerAccount, whose: str, client=None) -> tuple[list[str], list[dict]]:
    client = client or hellofresh_client(account)
    start, end = boxes.week_range(date.today(), HELLOFRESH_WEEKS)
    lines, upcoming = [], []
    for box in coming_boxes(client, start, end):
        meals = f": {', '.join(box.meals)}" if box.meals else ""
        if box.skipped:
            lines.append(f"- HelloFresh {box.week} ({whose}): skipped")
            continue
        lines.append(f"- HelloFresh box {box.week} ({whose}): arriving {_when(box.delivery_date)}, status {box.status}"
                     + (f", skip or change meals by {_when(box.cutoff)}" if box.cutoff else "") + meals)
        if box.delivery_date:
            upcoming.append({"title": "HelloFresh box", "at": box.delivery_date, "kind": "delivery"})
        if box.cutoff and box.changeable:
            upcoming.append({"title": "HelloFresh: skip or choose meals by", "at": box.cutoff, "kind": "deadline"})
    return lines or [f"- No HelloFresh boxes in the next {HELLOFRESH_WEEKS} weeks ({whose})"], upcoming


SECTIONS = {"ocado": ocado_section, "hellofresh": hellofresh_section}


@router.get("/feed", dependencies=[Depends(require_feed_token)])
def feed(session: Session = Depends(get_session)) -> dict[str, Any]:
    rows = session.execute(
        select(RetailerAccount, User).join(User, User.id == RetailerAccount.user_id)
        .where(RetailerAccount.retailer.in_(SECTIONS), RetailerAccount.status == "connected",
               RetailerAccount.user_id.in_(household.member_ids(session)))
    ).all()
    lines: list[str] = []
    upcoming: list[dict] = []
    for account, user in rows:
        whose = account.email or getattr(user, "name", None) or f"user {user.id}"
        try:
            more_lines, more_upcoming = SECTIONS[account.retailer](account, whose)
        except Exception as exc:  # noqa: BLE001 - one dead session mustn't sink the feed
            log.warning("noodle feed: %s for %s failed: %s", account.retailer, whose, exc)
            more_lines, more_upcoming = [f"- {account.retailer} ({whose}): unavailable ({exc})"], []
        lines += more_lines
        upcoming += more_upcoming
    upcoming.sort(key=lambda u: u["at"])
    return {"context": "\n".join(lines) or "No shopping accounts connected.", "upcoming": upcoming}


class ShoppingItemIn(BaseModel):
    text: str = Field(min_length=1, max_length=120)
    #: Leave out when no number was said: then something already on the order is left alone.
    quantity: int | None = Field(default=None, ge=1, le=top_up.MAX_QUANTITY)


class ShoppingIn(BaseModel):
    items: list[ShoppingItemIn] = Field(min_length=1, max_length=20)


class TakeBackIn(BaseModel):
    order_id: str
    #: sku → how many were added, as ``/shopping`` reported them (negative: how many were taken off).
    lines: dict[str, int] = Field(default_factory=dict)
    slot_id: str | None = None


class ChangeIn(BaseModel):
    op: Literal["add", "remove", "set", "swap"]
    sku: str = Field(min_length=1, max_length=128)
    quantity: int | None = Field(default=None, ge=0, le=top_up.MAX_QUANTITY)
    replaces: str | None = Field(default=None, max_length=128)
    #: The product's name, as the plan gave it: kept so the reply can say it.
    name: str | None = Field(default=None, max_length=300)
    replaces_name: str | None = Field(default=None, max_length=300)


class DeliveryIn(BaseModel):
    slot_id: str = Field(min_length=1, max_length=128)
    start: str
    end: str


class PlanIn(BaseModel):
    pending_delivery: DeliveryIn | None = None
    #: What was said, oldest first; the last is new. Earlier ones are what the pending plan came from.
    instructions: list[str] = Field(min_length=1, max_length=12)
    pending: list[ChangeIn] = Field(default_factory=list, max_length=resolver.MAX_LINES)


class ApplyIn(BaseModel):
    delivery: DeliveryIn | None = None
    order_id: str | None = None
    changes: list[ChangeIn] = Field(default_factory=list, max_length=resolver.MAX_LINES)


def _ocado_accounts(session: Session, user: User) -> list[tuple[RetailerAccount, OcadoClient]]:
    accounts = household.accounts(session, user, "ocado")
    if not accounts:
        raise HTTPException(status_code=404, detail="No Ocado account is connected")
    return [(account, OcadoClient(get_shared_session(account.key))) for account in accounts]


@router.post("/shopping", dependencies=[Depends(require_feed_token)])
def add_to_shopping(body: ShoppingIn, session: Session = Depends(get_session),
                    user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Put each item on the next editable Ocado order and confirm the change.

    Items it can't place come back under ``skipped`` with the reason; the request
    only fails when the order itself can't be changed.
    """
    accounts = _ocado_accounts(session, user)
    wants = [top_up.Want(item.text.strip(), item.quantity) for item in body.items]
    with _push_lock("ocado"):  # the edit session takes over the cart for a moment
        try:
            return top_up.add(session, user.id, wants, accounts, completer=top_up.default_completer(),
                              stock=top_up.live_stock(accounts[0][1]))
        except (top_up.NoOrder, amend.OrderChangeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/shopping/undo", dependencies=[Depends(require_feed_token)])
def undo_shopping(body: TakeBackIn, session: Session = Depends(get_session),
                  user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Take what ``/shopping`` added back off the order."""
    accounts = _ocado_accounts(session, user)
    with _push_lock("ocado"):
        try:
            return top_up.take_back(body.order_id, body.lines, accounts, slot_id=body.slot_id)
        except (top_up.NoOrder, amend.OrderChangeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/shopping/plan", dependencies=[Depends(require_feed_token)])
def plan_shopping(body: PlanIn, session: Session = Depends(get_session),
                  user: User = Depends(get_current_user)) -> dict[str, Any]:
    """What was said, turned into a proposed change to the coming order (merged with
    ``pending``). Changes nothing on Ocado: ``/shopping/apply`` does, later."""
    accounts = _ocado_accounts(session, user)
    try:
        with _push_lock("ocado"):
            return resolver.plan(session, user.id, accounts, [text.strip()[:500] for text in body.instructions],
                                 pending=[c.model_dump(exclude={"name", "replaces_name"}) for c in body.pending],
                                 pending_delivery=body.pending_delivery.model_dump() if body.pending_delivery else None)
    except top_up.NoOrder as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except resolver.ResolveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/shopping/apply", dependencies=[Depends(require_feed_token)])
def apply_shopping(body: ApplyIn, session: Session = Depends(get_session),
                   user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Make a planned change in one checkout, against the order as it is now."""
    accounts = _ocado_accounts(session, user)
    with _push_lock("ocado"):
        try:
            return resolver.apply(session, accounts, [c.model_dump() for c in body.changes], order_id=body.order_id,
                                  delivery=body.delivery.model_dump() if body.delivery else None)
        except (top_up.NoOrder, amend.OrderChangeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


_reading = threading.Lock()


@router.post("/purchases/sync", dependencies=[Depends(require_feed_token)], status_code=202)
def sync_purchases(session: Session = Depends(get_session), user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Read new delivered orders into the purchase history now, in the background (the
    heartbeat does this daily; this is for the first backfill)."""
    accounts = [(a.key, a.id) for a, _ in _ocado_accounts(session, user)]
    if not _reading.acquire(blocking=False):
        return {"started": False, "detail": "already reading"}

    def run() -> None:
        try:
            for key, _ in accounts:
                purchases.sync_account(key)
        except Exception:  # noqa: BLE001
            log.exception("purchases: sync failed")
        finally:
            _reading.release()

    threading.Thread(target=run, name="purchases-sync", daemon=True).start()
    return {"started": True}


def _when(value: str | None) -> str:
    if not value:
        return "?"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return f"{dt:%a %-d %b}" if len(value) <= 10 else f"{dt:%a %-d %b %H:%M}"


def _clock(value: str | None) -> str:
    try:
        return f"{datetime.fromisoformat((value or '').replace('Z', '+00:00')):%H:%M}"
    except ValueError:
        return "?"
