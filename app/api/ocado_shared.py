"""All authorized people shop one explicitly configured, debounced Ocado account.

Host/audience isolation is enforced by the application boundary; every route
still resolves an authenticated user here. No request can select an account key.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from typing import Annotated, Iterator, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from app.api.access import require_person
from app.api.deps import get_active_retailer, get_current_user, get_session, get_session_factory
from app.api.schemas import CartLoginIn, CartLoginOut, CartOtpIn
from app.cart.adapters import OcadoAdapter
from app.db import retailer_accounts
from app.db.models import User
from app.ocado import orders, shared, shop
from app.ocado.shared_queue import ACTIVE, QueueStore, empty

router = APIRouter(prefix="/api/ocado-shared", tags=["ocado-shared"], dependencies=[Depends(get_current_user)])


def get_shared_adapter() -> OcadoAdapter:
    return OcadoAdapter()


@contextmanager
def _operation(*, blocking: bool = True) -> Iterator[None]:
    try:
        with shared.account_lock(blocking=blocking):
            yield
    except shared.SharedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Shared Ocado operation failed: {exc}") from exc


def _auth_out(auth, account) -> CartLoginOut:
    return CartLoginOut(status=auth.status, stage=auth.stage or "idle", email=account.email)


@router.get("/status", response_model=CartLoginOut)
def status(session: Session = Depends(get_session), adapter: OcadoAdapter = Depends(get_shared_adapter)):
    # Status only reads the auth ladder's in-memory state. It must remain
    # available during checkout so clients can continue polling the queue.
    try:
        account = shared.find_account(session)
        return _auth_out(adapter.status(account.key), account) if account else CartLoginOut(status="logged_out")
    except shared.SharedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/login", response_model=CartLoginOut, dependencies=[Depends(require_person)])
def login(body: CartLoginIn, session: Session = Depends(get_session),
          adapter: OcadoAdapter = Depends(get_shared_adapter)):
    email, password = body.email.strip(), body.password.get_secret_value()
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    with _operation(blocking=False):
        account = shared.connect_account(session, email)
        try:
            auth = adapter.ensure_authenticated(account.key, email=email, password=password)
        finally:
            del password
        retailer_accounts.record_status(session, account, auth.status, email=email, after_login=True)
        return _auth_out(auth, account)


@router.post("/otp", response_model=CartLoginOut, dependencies=[Depends(require_person)])
def otp(body: CartOtpIn, session: Session = Depends(get_session),
        adapter: OcadoAdapter = Depends(get_shared_adapter)):
    with _operation(blocking=False):
        account = shared.require_account(session)
        try:
            auth = adapter.submit_otp(body.code, account.key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        retailer_accounts.record_status(session, account, auth.status, after_login=True)
        return _auth_out(auth, account)


@router.post("/session/refresh", response_model=CartLoginOut, dependencies=[Depends(require_person)])
def refresh(session: Session = Depends(get_session), adapter: OcadoAdapter = Depends(get_shared_adapter)):
    with _operation(blocking=False):
        account = shared.find_account(session)
        if account is None:
            return CartLoginOut(status="logged_out")
        auth = adapter.ensure_authenticated(account.key)
        retailer_accounts.record_status(session, account, auth.status)
        return _auth_out(auth, account)


@router.post("/logout", response_model=CartLoginOut, dependencies=[Depends(require_person)])
def logout(session: Session = Depends(get_session), adapter: OcadoAdapter = Depends(get_shared_adapter)):
    with _operation(blocking=False):
        account = shared.find_account(session)
        if account is None:
            return CartLoginOut(status="logged_out")
        auth = adapter.logout(account.key)
        retailer_accounts.disconnect(session, account)
        return _auth_out(auth, account)


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
    with _operation():
        account = shared.require_account(session)
        client, store = shared.make_client(account.key), QueueStore(factory)
        _, order, queue = _selection(client, store)
        basket = shared.project(shared.basket(client, session, order, retailer=retailer), queue["actions"])
        return {"order": asdict(orders.summarise(order)) if order else None,
                "basket": asdict(basket), "queue": queue}


@router.get("/search")
def search(q: str, session: Session = Depends(get_session)):
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Search for something")
    with _operation():
        client = shared.make_client(shared.require_account(session).key)
        return {"query": q, "products": [asdict(product) for product in shop.search_results(client.search(q, size=20), 20)]}


@router.get("/slots")
def slots(session: Session = Depends(get_session), factory: sessionmaker[Session] = Depends(get_session_factory)):
    with _operation():
        client = shared.make_client(shared.require_account(session).key)
        _, order, _ = _selection(client, QueueStore(factory))
        return {"items": shared.slots(client, order)}


class _Action(BaseModel):
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
    with _operation(blocking=False):
        client = shared.make_client(shared.require_account(session).key)
        store = QueueStore(factory)
        queue = store.snapshot()
        if queue["status"] in {"running", "failed"}:
            raise shared.SharedError("Wait for the running batch, or inspect and cancel the failed queue before adding changes")
        pinned = queue["target_id"] if queue["status"] == "pending" else None
        current_target, _ = shared.target(client, pinned)
        action = body.model_dump(exclude={"target_id", "confirm"}, exclude_none=True)
        return store.enqueue(body.target_id, current_target, action, user.id)


@router.get("/queue")
def queue(session: Session = Depends(get_session), factory: sessionmaker[Session] = Depends(get_session_factory)):
    # In particular, do not wait behind checkout: polling must show "running".
    if shared.find_account(session) is None:
        return empty()
    return QueueStore(factory).snapshot()


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str | None = None


@router.post("/queue/cancel", dependencies=[Depends(require_person)])
def cancel(body: CancelIn, session: Session = Depends(get_session),
           factory: sessionmaker[Session] = Depends(get_session_factory)):
    with _operation(blocking=False):
        if shared.find_account(session) is None:
            return empty()
        return QueueStore(factory).cancel(body.target_id)
