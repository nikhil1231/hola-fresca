"""All authorized people shop one explicitly configured, debounced Ocado account.

Host/audience isolation is enforced by the application boundary; every route
still resolves an authenticated user here. No request can select an account key.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from typing import Annotated, Iterator, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.access import require_person
from app.api.deps import get_active_retailer, get_current_user, get_session, get_session_factory
from app.api.schemas import CartLoginIn, CartLoginOut, CartOtpIn
from app.cart.adapters import OcadoAdapter
from app.db import retailer_accounts
from app.db.models import User, RetailerAccount, OcadoSharedSettings, OcadoSharedQueue
from app import config
from app.ocado import orders, shared, shop
from app.ocado.shared_queue import ACTIVE, QueueStore, empty

router = APIRouter(prefix="/api/ocado-shared", tags=["ocado-shared"], dependencies=[Depends(get_current_user)])


def _can_manage_settings(user: User) -> bool:
    return (user.email or "").strip().lower() == "nikhilkunde1231@gmail.com"


def require_settings_owner(user: User = Depends(get_current_user)) -> User:
    if not _can_manage_settings(user):
        raise HTTPException(status_code=403, detail="Only the shop owner can change these settings")
    return user


def get_shared_adapter() -> OcadoAdapter:
    return OcadoAdapter()


@contextmanager
def _operation(session: Session, *, blocking: bool = True, bind: bool = True) -> Iterator[None]:
    try:
        with shared.account_lock(blocking=blocking), (shared.using_account(session) if bind else nullcontext()):
            yield
    except shared.SharedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Shared Ocado operation failed: {exc}") from exc


class SelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int | None
    account_version: int = Field(ge=0, strict=True)


class SharedLoginIn(SelectionIn, CartLoginIn):
    pass


class SharedOtpIn(SelectionIn, CartOtpIn):
    pass


def _scope(session: Session) -> dict:
    account = shared.find_account(session)
    return {"account_id": account.id if account else None,
            "account_version": shared.selection_version(session)}


def _check_selection(session: Session, body: SelectionIn) -> None:
    if _scope(session) != {"account_id": body.account_id, "account_version": body.account_version}:
        raise shared.SharedError("The household's Ocado account changed. Refresh before making changes.")


def _auth_out(auth, account, session: Session, user: User) -> dict:
    labels = {item.id: item.label for item in config.OCADO_ACCOUNTS}
    accounts = [{"id": row.id, "label": labels.get(row.key) or name or row.email or "Ocado account",
                 "email": row.email, "status": row.status}
                for row, name in session.execute(select(RetailerAccount, User.name).join(
                    User, User.id == RetailerAccount.user_id
                ).where(RetailerAccount.retailer == "ocado").order_by(User.id))]
    result = CartLoginOut(status=auth.status, stage=auth.stage or "idle", email=account.email) if account else CartLoginOut(status="logged_out")
    allowed = _can_manage_settings(user)
    return {**result.model_dump(), **_scope(session), "accounts": accounts if allowed else [],
            "can_manage_settings": allowed}


class AccountSelectionIn(SelectionIn):
    selected_account_id: int = Field(gt=0, strict=True)


@router.post("/account", dependencies=[Depends(require_person)])
def choose_account(body: AccountSelectionIn, user: User = Depends(require_settings_owner),
                   session: Session = Depends(get_session), adapter: OcadoAdapter = Depends(get_shared_adapter)):
    with _operation(session, blocking=False, bind=False):
        with shared.using_account(session):
            _check_selection(session, body)
            account = session.get(RetailerAccount, body.selected_account_id)
            if account is None or account.retailer != "ocado":
                raise HTTPException(status_code=404, detail="Ocado account not found")
            if body.account_id != account.id:
                if session.scalar(select(OcadoSharedQueue.id).where(OcadoSharedQueue.status.in_(ACTIVE)).limit(1)):
                    raise HTTPException(status_code=409, detail="Clear or finish the queued changes before switching accounts")
                settings = session.get(OcadoSharedSettings, 1)
                if settings is None:
                    settings = OcadoSharedSettings(id=1, user_id=user.id, account_id=account.id, version=1)
                    session.add(settings)
                else:
                    settings.account_id, settings.user_id = account.id, user.id
                    settings.version += 1
                session.commit()
        return _auth_out(adapter.status(account.key), account, session, user)


@router.get("/status")
def status(session: Session = Depends(get_session), adapter: OcadoAdapter = Depends(get_shared_adapter),
           user: User = Depends(get_current_user)):
    # Status only reads the auth ladder's in-memory state. It must remain
    # available during checkout so clients can continue polling the queue.
    try:
        with shared.using_account(session):
            account = shared.find_account(session)
            return _auth_out(adapter.status(account.key) if account else None, account, session, user)
    except shared.SharedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/login", dependencies=[Depends(require_person)])
def login(body: SharedLoginIn, session: Session = Depends(get_session),
          adapter: OcadoAdapter = Depends(get_shared_adapter), user: User = Depends(require_settings_owner)):
    email, password = body.email.strip(), body.password.get_secret_value()
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    with _operation(session, blocking=False):
        _check_selection(session, body)
        account = shared.connect_account(session, email)
        try:
            auth = adapter.ensure_authenticated(account.key, email=email, password=password)
        finally:
            del password
        retailer_accounts.record_status(session, account, auth.status, email=email, after_login=True)
        return _auth_out(auth, account, session, user)


@router.post("/otp", dependencies=[Depends(require_person)])
def otp(body: SharedOtpIn, session: Session = Depends(get_session),
        adapter: OcadoAdapter = Depends(get_shared_adapter), user: User = Depends(require_settings_owner)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        account = shared.require_account(session)
        try:
            auth = adapter.submit_otp(body.code, account.key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        retailer_accounts.record_status(session, account, auth.status, after_login=True)
        return _auth_out(auth, account, session, user)


@router.post("/session/refresh", dependencies=[Depends(require_person)])
def refresh(body: SelectionIn, session: Session = Depends(get_session), adapter: OcadoAdapter = Depends(get_shared_adapter),
            user: User = Depends(get_current_user)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        account = shared.find_account(session)
        if account is None:
            return _auth_out(None, None, session, user)
        auth = adapter.ensure_authenticated(account.key)
        retailer_accounts.record_status(session, account, auth.status)
        return _auth_out(auth, account, session, user)


@router.post("/logout", dependencies=[Depends(require_person)])
def logout(body: SelectionIn, session: Session = Depends(get_session), adapter: OcadoAdapter = Depends(get_shared_adapter),
           user: User = Depends(require_settings_owner)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        account = shared.find_account(session)
        if account is None:
            return _auth_out(None, None, session, user)
        auth = adapter.logout(account.key)
        retailer_accounts.disconnect(session, account)
        return _auth_out(auth, account, session, user)


def _selection(client, store: QueueStore) -> tuple[str, dict | None, dict]:
    queue = store.snapshot()
    pinned = queue["target_id"] if queue["status"] in ACTIVE else None
    target_id, order = shared.target(client, pinned)
    if pinned is None:
        queue = store.remember_target(target_id)
    return target_id, order, queue


@router.get("/state")
def state(session: Session = Depends(get_session), factory: sessionmaker[Session] = Depends(get_session_factory),
          retailer: str = Depends(get_active_retailer)):
    with _operation(session):
        account = shared.require_account(session)
        client, store = shared.make_client(account.key), QueueStore(factory)
        _, order, queue = _selection(client, store)
        confirmed = shared.basket(client, session, order, retailer=retailer)
        basket = shared.project(confirmed, queue["actions"])
        basket = shared.decorate(basket, client, session, retailer=retailer)
        scope = _scope(session)
        return {**scope, "order": asdict(orders.summarise(order)) if order else None,
                "basket": asdict(basket), "confirmed_quantities": {line.sku: line.quantity for line in confirmed.lines},
                "queue": {**queue, **scope}}


@router.get("/search")
def search(q: str, session: Session = Depends(get_session)):
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Search for something")
    with _operation(session):
        client = shared.make_client(shared.require_account(session).key)
        return {**_scope(session), "query": q, "products": [asdict(product) for product in shop.search_results(client.search(q, size=20), 20)]}


@router.get("/slots")
def slots(session: Session = Depends(get_session), factory: sessionmaker[Session] = Depends(get_session_factory)):
    with _operation(session):
        client = shared.make_client(shared.require_account(session).key)
        _, order, _ = _selection(client, QueueStore(factory))
        return {**_scope(session), "items": shared.slots(client, order)}


@router.get("/offers/{offer_id}")
def offer(offer_id: UUID, session: Session = Depends(get_session)):
    with _operation(session):
        client = shared.make_client(shared.require_account(session).key)
        return {**_scope(session), **shared.promotion(client, str(offer_id))}


class _Action(SelectionIn):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(min_length=1, max_length=128)


class QuantityAction(_Action):
    kind: Literal["quantity"]
    sku: str = Field(min_length=1, max_length=128)
    quantity: int = Field(ge=0, le=50, strict=True)
    name: str | None = Field(default=None, max_length=500)


class SlotAction(_Action):
    kind: Literal["slot"]
    slot_id: str = Field(min_length=1, max_length=128)
    start: str | None = None
    end: str | None = None


class CheckoutAction(_Action):
    kind: Literal["checkout"]
    confirm: Literal[True]


Action = Annotated[QuantityAction | SlotAction | CheckoutAction, Body(discriminator="kind")]


class OfferItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=128)
    quantity: int = Field(ge=1, le=50, strict=True)
    existing_quantity: int = Field(ge=0, le=50, strict=True)


class OfferSelectionIn(_Action):
    offer_id: UUID
    items: list[OfferItemIn] = Field(min_length=1, max_length=50)


@router.post("/offer-items", dependencies=[Depends(require_person)])
def add_offer_items(body: OfferSelectionIn, user: User = Depends(get_current_user),
                    session: Session = Depends(get_session),
                    factory: sessionmaker[Session] = Depends(get_session_factory),
                    retailer: str = Depends(get_active_retailer)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        client = shared.make_client(shared.require_account(session).key)
        store = QueueStore(factory)
        current_target, order, queue = _selection(client, store)
        if queue["status"] in {"running", "failed"}:
            raise shared.SharedError("Wait for or clear the current batch before adding offer items")
        promotion = shared.promotion(client, str(body.offer_id))
        selected = {item.sku: item for item in body.items}
        if len(selected) != len(body.items):
            raise shared.SharedError("Choose each product once, using its quantity buttons")
        membership = {}
        for index, group in enumerate(promotion["groups"]):
            quantity = group["quantity"]
            if not isinstance(quantity, int) or not 1 <= quantity <= 50:
                raise shared.SharedError("This offer cannot be added as a set; add its products individually")
            for product in group["products"]:
                sku = product["sku"]
                if sku in membership:
                    raise shared.SharedError("This offer has overlapping groups; add its products individually")
                membership[sku] = (index, product)
            if sum(item.quantity for sku, item in selected.items()
                   if membership.get(sku, (None,))[0] == index) != quantity:
                raise shared.SharedError("Choose the required number of items from each offer group")
        if any(sku not in membership or membership[sku][1]["in_stock"] is False for sku in selected):
            raise shared.SharedError("An item is no longer available in this offer; reopen the offer")
        confirmed = shared.basket(client, session, order, retailer=retailer)
        basket = shared.project(confirmed, queue["actions"])
        current = {line.sku: line.quantity for line in basket.lines}
        additions = []
        for sku, item in selected.items():
            if current.get(sku, 0) != item.existing_quantity:
                raise shared.SharedError("Someone changed these basket quantities; review the offer again")
            quantity = item.existing_quantity + item.quantity
            if quantity > 50:
                raise shared.SharedError("A basket item cannot exceed 50 packs")
            additions.append({"kind": "quantity", "sku": sku,
                              "name": membership[sku][1]["name"], "quantity": quantity})
        return {**store.enqueue_many(body.target_id, current_target, additions, user.id,
                                    quantities={line.sku: line.quantity for line in confirmed.lines}), **_scope(session)}


@router.post("/actions", dependencies=[Depends(require_person)])
def actions(body: Action, user: User = Depends(get_current_user), session: Session = Depends(get_session),
            factory: sessionmaker[Session] = Depends(get_session_factory),
            retailer: str = Depends(get_active_retailer)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        client = shared.make_client(shared.require_account(session).key)
        store = QueueStore(factory)
        queue = store.snapshot()
        if queue["status"] in {"running", "failed"}:
            raise shared.SharedError("Wait for the running batch, or inspect and cancel the failed queue before adding changes")
        pinned = queue["target_id"] if queue["status"] == "pending" else None
        current_target, order = shared.target(client, pinned)
        action = body.model_dump(exclude={"target_id", "confirm", "account_id", "account_version"}, exclude_none=True)
        quantities = None
        if body.kind == "quantity":
            confirmed = shared.basket(client, session, order, retailer=retailer, name_products=False)
            quantities = {line.sku: line.quantity for line in confirmed.lines}
        return {**store.enqueue(body.target_id, current_target, action, user.id, quantities=quantities,
                               slot_id=order.get("slotId") if order else None), **_scope(session)}


@router.get("/queue")
def queue(session: Session = Depends(get_session), factory: sessionmaker[Session] = Depends(get_session_factory)):
    # In particular, do not wait behind checkout: polling must show "running".
    with shared.using_account(session):
        snapshot = QueueStore(factory).snapshot() if shared.find_account(session) else empty()
        return {**snapshot, **_scope(session)}


class CancelIn(SelectionIn):
    model_config = ConfigDict(extra="forbid")
    target_id: str | None = None


@router.post("/queue/cancel", dependencies=[Depends(require_person)])
def cancel(body: CancelIn, session: Session = Depends(get_session),
           factory: sessionmaker[Session] = Depends(get_session_factory)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        if shared.find_account(session) is None:
            return {**empty(), **_scope(session)}
        return {**QueueStore(factory).cancel(body.target_id), **_scope(session)}


@router.post("/queue/retry", dependencies=[Depends(require_person)])
def retry(body: CancelIn, session: Session = Depends(get_session),
          factory: sessionmaker[Session] = Depends(get_session_factory)):
    """Send changes Ocado refused (and that we confirmed didn't land) straight away."""
    with _operation(session, blocking=False):
        _check_selection(session, body)
        return {**QueueStore(factory).retry(body.target_id), **_scope(session)}


@router.post("/queue/check", dependencies=[Depends(require_person)])
def check(body: CancelIn, session: Session = Depends(get_session),
          factory: sessionmaker[Session] = Depends(get_session_factory)):
    """Read Ocado back for a failed batch: clear it if it landed, unlock it if nothing changed."""
    with _operation(session, blocking=False):
        _check_selection(session, body)
        if shared.find_account(session) is None:
            return {**empty(), **_scope(session)}
        client = shared.make_client(shared.account_key(session))
        return {**QueueStore(factory).recheck(lambda target, actions: shared.verify(client, target, actions)),
                **_scope(session)}
