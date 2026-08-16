"""The Ocado endpoints that are Ocado's alone.

Everything a shop with a cart has in common — sessions, login, OTP, the basket
plan and push — moved to ``/api/cart/{retailer}`` when Sainsbury's grew a
trolley of its own (:mod:`app.api.cart`). What is left here are the two things
no other shop has an equivalent of:

* **delivery slots.** Sainsbury's has a slot API too, but nothing in the app
  talks to it yet, and inventing a retailer-neutral slot endpoint that only one
  shop can answer would be a worse lie than this module's name.
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
from app.db.models import OcadoAuthEvent, User
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
