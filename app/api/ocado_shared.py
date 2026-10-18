"""All authorized people shop one explicitly configured, debounced Ocado account.

Host/audience isolation is enforced by the application boundary; every route
still resolves an authenticated user here. No request can select an account key.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from typing import Annotated, Iterator, Literal

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


def _auth_out(auth, account, session: Session) -> dict:
    labels = {item.id: item.label for item in config.OCADO_ACCOUNTS}
    accounts = [{"id": row.id, "label": labels.get(row.key) or name or row.email or "Ocado account",
                 "email": row.email, "status": row.status}
                for row, name in session.execute(select(RetailerAccount, User.name).join(
                    User, User.id == RetailerAccount.user_id
                ).where(RetailerAccount.retailer == "ocado").order_by(User.id))]
    result = CartLoginOut(status=auth.status, stage=auth.stage or "idle", email=account.email) if account else CartLoginOut(status="logged_out")
    return {**result.model_dump(), **_scope(session), "accounts": accounts}


class AccountSelectionIn(SelectionIn):
    selected_account_id: int = Field(gt=0, strict=True)


@router.post("/account", dependencies=[Depends(require_person)])
def choose_account(body: AccountSelectionIn, user: User = Depends(get_current_user),
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
        return _auth_out(adapter.status(account.key), account, session)


@router.get("/status")
def status(session: Session = Depends(get_session), adapter: OcadoAdapter = Depends(get_shared_adapter)):
    # Status only reads the auth ladder's in-memory state. It must remain
    # available during checkout so clients can continue polling the queue.
    try:
        with shared.using_account(session):
            account = shared.find_account(session)
            return _auth_out(adapter.status(account.key) if account else None, account, session)
    except shared.SharedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/login", dependencies=[Depends(require_person)])
def login(body: SharedLoginIn, session: Session = Depends(get_session),
          adapter: OcadoAdapter = Depends(get_shared_adapter)):
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
        return _auth_out(auth, account, session)


@router.post("/otp", dependencies=[Depends(require_person)])
def otp(body: SharedOtpIn, session: Session = Depends(get_session),
        adapter: OcadoAdapter = Depends(get_shared_adapter)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        account = shared.require_account(session)
        try:
            auth = adapter.submit_otp(body.code, account.key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        retailer_accounts.record_status(session, account, auth.status, after_login=True)
        return _auth_out(auth, account, session)


@router.post("/session/refresh", dependencies=[Depends(require_person)])
def refresh(body: SelectionIn, session: Session = Depends(get_session), adapter: OcadoAdapter = Depends(get_shared_adapter)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        account = shared.find_account(session)
        if account is None:
            return _auth_out(None, None, session)
        auth = adapter.ensure_authenticated(account.key)
        retailer_accounts.record_status(session, account, auth.status)
        return _auth_out(auth, account, session)


@router.post("/logout", dependencies=[Depends(require_person)])
def logout(body: SelectionIn, session: Session = Depends(get_session), adapter: OcadoAdapter = Depends(get_shared_adapter)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        account = shared.find_account(session)
        if account is None:
            return _auth_out(None, None, session)
        auth = adapter.logout(account.key)
        retailer_accounts.disconnect(session, account)
        return _auth_out(auth, account, session)


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
        basket = shared.project(shared.basket(client, session, order, retailer=retailer), queue["actions"])
        return {**_scope(session), "order": asdict(orders.summarise(order)) if order else None,
                "basket": asdict(basket), "queue": queue}


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


@router.post("/actions", dependencies=[Depends(require_person)])
def actions(body: Action, user: User = Depends(get_current_user), session: Session = Depends(get_session),
            factory: sessionmaker[Session] = Depends(get_session_factory)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        client = shared.make_client(shared.require_account(session).key)
        store = QueueStore(factory)
        queue = store.snapshot()
        if queue["status"] in {"running", "failed"}:
            raise shared.SharedError("Wait for the running batch, or inspect and cancel the failed queue before adding changes")
        pinned = queue["target_id"] if queue["status"] == "pending" else None
        current_target, _ = shared.target(client, pinned)
        action = body.model_dump(exclude={"target_id", "confirm", "account_id", "account_version"}, exclude_none=True)
        return {**store.enqueue(body.target_id, current_target, action, user.id), **_scope(session)}


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
