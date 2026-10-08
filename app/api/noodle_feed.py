"""``GET /api/noodle/feed``: what Noodle, the owner's organiser, can see of the household's
shopping. Read-only. Noodle reaches it through the tunnel with a Cloudflare Access
service token (listed in ``HOLAFRESCA_ACCESS_SERVICES``), or over the LAN with
``HOLAFRESCA_NOODLE_FEED_TOKEN``.

The contract is shared by every system that feeds Noodle:

    {"context": "<plain text for answering questions>",
     "upcoming": [{"title", "at" (ISO 8601), "kind", "link"?}]}

``context`` is read by the model when someone asks Noodle a question; ``upcoming``
lands in Noodle's Coming up list and its morning summary. It covers every
connected account in the household (one person's Ocado often holds the shared
order), labelled by whose it is. An account that fails is reported in
``context`` and skipped, never fails the feed.
"""
from __future__ import annotations

import hmac
import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.api import access
from app.api.deps import get_session
from app.api.hellofresh import client_for as hellofresh_client
from app.api.hellofresh import coming_boxes
from app.db.models import RetailerAccount, User
from app.hellofresh import boxes
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
        .where(RetailerAccount.retailer.in_(SECTIONS), RetailerAccount.status == "connected")
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
