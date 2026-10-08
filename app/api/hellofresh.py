"""The caller's own HelloFresh subscription: sign in, see the coming boxes, skip or
un-skip a week, change or cancel the plan.

HelloFresh is a connection, not a shop: it isn't in :data:`app.retailers.RETAILERS`
(nothing is priced or pushed to it), but the account lives in the same
``retailer_accounts`` registry as Ocado's, under ``retailer="hellofresh"``, with
the same rule that the password crosses one request and is never stored. What
survives is the token pair, on disk under the account's key.

Everything that changes the subscription takes ``{"confirm": true}``: these are
the site's own buttons, and a skipped box or a cancelled plan isn't something to
do by accident. The ``/raw/...`` reads pass HelloFresh's answer through
untouched, for anything the summaries don't cover yet.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, household
from app.api.access import require_person
from app.api.deps import get_current_user, get_session
from app.db import retailer_accounts
from app.db.models import RetailerAccount, User
from app.hellofresh import boxes
from app.hellofresh.client import PAUSED, RUNNING, HelloFreshClient, HelloFreshError, NeedsLogin, TokenStore, unwrap

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/hellofresh", tags=["hellofresh"])
RETAILER = boxes.RETAILER


class LoginIn(BaseModel):
    email: str
    password: SecretStr


class StatusOut(BaseModel):
    status: str  # ready | expired | logged_out
    email: str | None = None


class ConfirmIn(BaseModel):
    confirm: bool = False


class CancelIn(ConfirmIn):
    reason: str | None = None


class WeekIn(ConfirmIn):
    #: Only needed when the account has more than one live subscription.
    subscription_id: str | None = None


class CancelAllIn(CancelIn):
    plan_id: str | None = None


class ChangePlanIn(ConfirmIn):
    product_handle: str


def token_path(account_key: str) -> Path:
    return config.DATA_DIR / "hellofresh" / "accounts" / account_key / "session.json"


def client_for(account: RetailerAccount) -> HelloFreshClient:
    return HelloFreshClient(TokenStore(token_path(account.key)).load())


#: When each configured login last failed: a wrong password isn't retried on every request.
_login_failed_at: dict[str, float] = {}
LOGIN_RETRY_S = 30 * 60


def configured_login(session: Session, user: User) -> RetailerAccount | None:
    """``user``'s HelloFresh account, its session restored from their env-file login
    (:data:`config.HELLOFRESH_LOGINS`) if it's missing or can't be refreshed: first by
    refreshing, then from the configured refresh token, then (rarely possible from a
    server: HelloFresh's login is behind a Cloudflare challenge) with the password.
    None when they have no such login or nothing worked; a refused password isn't
    tried again for half an hour."""
    who = (user.email or "").lower()
    login = config.HELLOFRESH_LOGINS.get(who)
    if login is None:
        return None
    email, password, seed = login
    account = retailer_accounts.find(session, user.id, RETAILER) or retailer_accounts.connect(
        session, user.id, RETAILER, email=email)
    client = client_for(account)
    if client.status() == "ready":
        return account

    def refreshed() -> bool:
        try:
            client.refresh()
        except (NeedsLogin, HelloFreshError):
            return False
        retailer_accounts.record_status(session, account, "ready", email=email)
        return True

    if client.status() == "expired" and refreshed():
        return account
    if seed and client.tokens.refresh_token != seed:  # a new token from the env file
        client.tokens.save({"refresh_token": seed})
        if refreshed():
            log.info("hellofresh: %s's session restored from the configured refresh token", who)
            return account
    if not password or (who in _login_failed_at and time.monotonic() - _login_failed_at[who] < LOGIN_RETRY_S):
        return None
    try:
        client.login(email, password)
    except HelloFreshError as exc:
        _login_failed_at[who] = time.monotonic()
        log.warning("hellofresh: the configured login for %s was refused: %s", who, str(exc)[:120])
        return None
    _login_failed_at.pop(who, None)
    retailer_accounts.record_status(session, account, "ready", email=email, after_login=True)
    log.info("hellofresh: signed %s in with the configured login", who)
    return account


def household_accounts(session: Session, user: User) -> list[RetailerAccount]:
    """The HelloFresh accounts ``user`` may act on (their own first, then the
    household's: :mod:`app.household`), each signed in from the env file if it can be."""
    members = (session.scalars(select(User).where(User.id.in_(household.member_ids(session)))).all()
               if household.is_member(user) else [user])
    for member in members:
        configured_login(session, member)
    return household.accounts(session, user, RETAILER)


def _account(session: Session, user: User, which: str | None = None) -> RetailerAccount:
    """The account a request acts on: the caller's own, or with ``which`` (a HelloFresh
    email) another in their household."""
    if which:
        wanted = which.strip().lower()
        account = next((a for a in household_accounts(session, user) if (a.email or "").lower() == wanted), None)
        if account is None:
            raise HTTPException(status_code=404, detail=f"No HelloFresh account {which} that you can manage")
        return account
    account = retailer_accounts.find(session, user.id, RETAILER)
    if account is None or client_for(account).status() == "logged_out":
        account = configured_login(session, user) or account
    if account is None:
        raise HTTPException(status_code=404, detail="No HelloFresh account is connected: sign in first")
    return account


def get_hellofresh_client(account: str | None = None, session: Session = Depends(get_session),
                          user: User = Depends(get_current_user)) -> HelloFreshClient:
    """Every HelloFresh route takes ``?account=<HelloFresh email>`` to act on another
    household member's subscription; without it, the caller's own."""
    return client_for(_account(session, user, account))


def _call(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except NeedsLogin as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except HelloFreshError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _confirmed(body: ConfirmIn) -> None:
    if not body.confirm:
        raise HTTPException(status_code=400, detail='This changes your HelloFresh subscription: send {"confirm": true}')


# ---- session -----------------------------------------------------------------
@router.get("/status", response_model=StatusOut)
def status(session: Session = Depends(get_session), user: User = Depends(get_current_user)) -> StatusOut:
    account = configured_login(session, user) or retailer_accounts.find(session, user.id, RETAILER)
    if account is None:
        return StatusOut(status="logged_out")
    return StatusOut(status=client_for(account).status(), email=account.email)


@router.post("/login", response_model=StatusOut, dependencies=[Depends(require_person)])
def login(body: LoginIn, session: Session = Depends(get_session),
          user: User = Depends(get_current_user)) -> StatusOut:
    email = body.email.strip()
    password = body.password.get_secret_value()
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    account = retailer_accounts.find(session, user.id, RETAILER) or retailer_accounts.connect(
        session, user.id, RETAILER, email=email)
    try:
        _call(client_for(account).login, email, password)
    finally:
        del password
    retailer_accounts.record_status(session, account, "ready", email=email, after_login=True)
    return StatusOut(status="ready", email=email)


@router.post("/logout", response_model=StatusOut, dependencies=[Depends(require_person)])
def logout(session: Session = Depends(get_session), user: User = Depends(get_current_user)) -> StatusOut:
    account = _account(session, user)
    client_for(account).logout()
    retailer_accounts.disconnect(session, account)
    return StatusOut(status="logged_out")


# ---- reads -------------------------------------------------------------------
@router.get("/boxes")
def upcoming_boxes(weeks: int = 4, client: HelloFreshClient = Depends(get_hellofresh_client)) -> dict:
    """The next ``weeks`` weeks of boxes: date, skip/edit cutoff, skipped or not, meals."""
    start, end = boxes.week_range(date.today(), min(max(weeks, 1), 12))
    return {"range": [start, end], "boxes": [asdict(b) for b in _call(coming_boxes, client, start, end)]}


def coming_boxes(client: HelloFreshClient, start: str, end: str) -> list[boxes.Box]:
    """The weeks ``start``..``end``, with the chosen meals of each box not skipped.
    A menu that won't load leaves that box's meals empty rather than failing."""
    payload = client.deliveries(start, end)
    subscriptions = {str(s["id"]): s for s in boxes.live(client.subscriptions())}
    meals = {}
    for delivery in (payload.get("items") if isinstance(payload, dict) else None) or []:
        subscription = subscriptions.get(str(delivery.get("subscriptionId")))
        if subscription is None or delivery.get("status") != RUNNING:
            continue
        try:
            menu = client.week_menu(boxes.menu_params(subscription, delivery, delivery["id"]))
        except HelloFreshError as exc:
            log.info("hellofresh menu for %s not read: %s", delivery.get("id"), exc)
            continue
        meals[delivery["id"]] = boxes.chosen_meals(menu)
    return boxes.summarise(payload, meals)


@router.get("/household/boxes")
def household_boxes(weeks: int = 4, session: Session = Depends(get_session),
                    user: User = Depends(get_current_user)) -> dict:
    """The coming boxes on every HelloFresh account the caller may manage, each with whose it is."""
    start, end = boxes.week_range(date.today(), min(max(weeks, 1), 12))
    out = []
    for account in household_accounts(session, user):
        try:
            out.append({"account": account.email, "boxes": [asdict(b) for b in coming_boxes(client_for(account), start, end)]})
        except (HelloFreshError, NeedsLogin) as exc:
            out.append({"account": account.email, "boxes": [], "error": str(exc)})
    return {"range": [start, end], "accounts": out}


@router.get("/subscriptions")
def subscriptions(client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    return _call(client.subscriptions)


@router.get("/subscriptions/{subscription_id}")
def subscription(subscription_id: str, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    return _call(client.subscription, subscription_id)


@router.get("/subscriptions/{subscription_id}/weeks/{week}")
def delivery(subscription_id: str, week: str, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    return _call(client.delivery, subscription_id, week)


@router.get("/subscriptions/{subscription_id}/product-options")
def product_options(subscription_id: str, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    return _call(client.product_options, subscription_id)


@router.get("/orders")
def orders(limit: int = 10, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    return _call(client.orders, min(max(limit, 1), 50))


@router.get("/plans")
def plans(client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    return _call(client.plans)


@router.get("/plans/{plan_id}")
def plan(plan_id: str, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    return _call(client.plan, plan_id)


@router.get("/menu/{week}")
def menu(week: str, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    return _call(client.menu, week)


@router.get("/raw/customer")
def customer(client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    return _call(client.customer)


@router.get("/raw/deliveries")
def raw_deliveries(range_start: str, range_end: str, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    return _call(client.deliveries, range_start, range_end)


# ---- changes -----------------------------------------------------------------
def _only(ids: list[str], what: str) -> str:
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise HTTPException(status_code=409, detail=f"No live HelloFresh {what} on this account")
    raise HTTPException(status_code=409, detail=f"More than one live HelloFresh {what} ({', '.join(ids)}): name one")


def _change_week(client: HelloFreshClient, when: str, body: WeekIn, status: str) -> dict:
    _confirmed(body)
    try:
        week = boxes.week_of(when)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="A week is 2026-W42 or any date in it, e.g. 2026-10-19") from exc
    subscription_id = body.subscription_id or _only(boxes.live_ids(_call(client.subscriptions)), "subscription")
    delivery = unwrap(_call(client.delivery, subscription_id, week))
    if delivery.get("status") == status:
        return {"week": week, "subscription_id": subscription_id, "status": status, "changed": False}
    if not boxes.changeable(delivery):
        raise HTTPException(status_code=409, detail=f"{week} is past its cutoff ({delivery.get('cutoffDate')}): "
                                                    "HelloFresh won't change it now")
    _call(client.set_delivery_status, subscription_id, week, status,
          cutoff_date=delivery.get("cutoffDate"), delivery_date=delivery.get("deliveryDate"))
    return {"week": week, "subscription_id": subscription_id, "status": status, "changed": True}


@router.post("/weeks/{when}/skip")
def skip_week(when: str, body: WeekIn, client: HelloFreshClient = Depends(get_hellofresh_client)) -> dict:
    """Skip a week's box: ``when`` is an ISO week or any date in it. Skipping a week
    already skipped is a no-op; one past its cutoff is refused."""
    return _change_week(client, when, body, PAUSED)


@router.post("/weeks/{when}/unskip")
def unskip_week(when: str, body: WeekIn, client: HelloFreshClient = Depends(get_hellofresh_client)) -> dict:
    return _change_week(client, when, body, RUNNING)


@router.post("/cancel")
def cancel_subscription(body: CancelAllIn, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    """Cancel the account's plan outright: every future box stops."""
    _confirmed(body)
    plan_id = body.plan_id or _only(boxes.live_ids(_call(client.plans)), "plan")
    return {"plan_id": plan_id, "result": _call(client.cancel_plan, plan_id, body.reason)}


@router.post("/subscriptions/{subscription_id}/weeks/{week}/skip")
def skip(subscription_id: str, week: str, body: ConfirmIn, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    _confirmed(body)
    return _call(client.skip_week, subscription_id, week)


@router.post("/subscriptions/{subscription_id}/weeks/{week}/unskip")
def unskip(subscription_id: str, week: str, body: ConfirmIn, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    _confirmed(body)
    return _call(client.unskip_week, subscription_id, week)


@router.post("/plans/{plan_id}/product")
def change_plan(plan_id: str, body: ChangePlanIn, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    _confirmed(body)
    return _call(client.change_plan_product, plan_id, body.product_handle)


@router.post("/plans/{plan_id}/cancel")
def cancel(plan_id: str, body: CancelIn, client: HelloFreshClient = Depends(get_hellofresh_client)) -> Any:
    _confirmed(body)
    return _call(client.cancel_plan, plan_id, body.reason)
