"""All authorized people shop one explicitly configured, debounced Ocado account.

Host/audience isolation is enforced by the application boundary; every route
still resolves an authenticated user here. No request can select an account key.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Annotated, Iterator, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.access import require_person
from app.api.deps import get_active_retailer, get_current_user, get_session, get_session_factory
from app.api.schemas import CartLoginIn, CartLoginOut, CartOtpIn
from app.cart.adapters import OcadoAdapter
from app.db import retailer_accounts
from app.db.models import Product, User, RetailerAccount, OcadoSharedSettings, OcadoSharedQueue
from app import config
from app.ocado import orders, shared, shop
from app.scraper.products.ocado import parse_shelf_life
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
    from app.ocado.reserved import snapshot
    reserved_state = snapshot(session, account.id) if account else {"runs": []}
    allowed = _can_manage_settings(user)
    return {**result.model_dump(), **_scope(session), "accounts": accounts if allowed else [],
            "can_manage_settings": allowed, "reserved_revision": reserved_state.get("revision"), "reserved_warnings": [run for run in reserved_state["runs"] if run["warning"]]}


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


def _selection(client, store: QueueStore, selected: str | None = None) -> tuple[str, dict | None, dict, list[dict]]:
    queue = store.snapshot()
    upcoming = shared.upcoming_orders(client)
    order = upcoming[0] if upcoming else None
    if selected is not None:
        order = next((item for item in upcoming if str(item.get("orderId")) == selected), None)
        if selected == "cart":
            if upcoming:
                raise shared.SharedError("A delivery has been booked since this cart was selected; choose a delivery")
        elif order is None:
            raise shared.SharedError("This delivery is no longer available. Choose another delivery.")
    target_id = str(order["orderId"]) if order else "cart"
    # A browser's choice is never written into the shared selection. Cache only
    # the default idle target, so browsers viewing different orders don't fight.
    if selected is None and queue["status"] not in ACTIVE:
        queue = store.remember_target(target_id)
    return target_id, order, queue, upcoming


def _require_editable(order: dict | None) -> None:
    if order is None:
        return
    summary = orders.summarise(order)
    deadline = shared._date(summary.edit_until)
    if not summary.editable or (deadline is not None and deadline <= datetime.now(timezone.utc)):
        raise shared.SharedError("Editing is closed for this delivery. Choose another delivery.")


@router.get("/deliveries")
def deliveries(session: Session = Depends(get_session)):
    with _operation(session):
        client = shared.make_client(shared.require_account(session).key)
        return {**_scope(session), "items": [asdict(orders.summarise(order)) for order in shared.upcoming_orders(client)]}


@router.get("/state")
def state(session: Session = Depends(get_session), factory: sessionmaker[Session] = Depends(get_session_factory),
          retailer: str = Depends(get_active_retailer),
          target_id: str | None = Query(default=None, min_length=1, max_length=128)):
    with _operation(session):
        account = shared.require_account(session)
        client, store = shared.make_client(account.key), QueueStore(factory)
        selected, order, queue, upcoming = _selection(client, store, target_id)
        confirmed = shared.decorate(shared.basket(client, session, order, retailer=retailer), client, session, retailer=retailer)
        basket = shared.project(confirmed, queue["actions"] if queue["target_id"] == selected else [])
        basket = shared.decorate(basket, client, session, retailer=retailer)
        pictured = {line.sku: line for line in (*confirmed.lines, *basket.lines)}
        changes = shop.Basket(tuple(pictured.get(action["sku"]) or shop.Line(
            action["sku"], action.get("name"), 1, None
        ) for action in queue["actions"] if action["kind"] == "quantity"), None, False)
        changes = shared.decorate(changes, client, session, retailer=retailer)
        change_products = {line.sku: {"name": line.name, "image_url": line.image_url} for line in changes.lines}
        scope = _scope(session)
        earlier = upcoming[0] if order and upcoming and shared._date(upcoming[0]["dates"]["deliveryStartDate"]) < shared._date(order["dates"]["deliveryStartDate"]) else None
        return {**scope, "target_id": selected, "order": asdict(orders.summarise(order)) if order else None,
                "earlier_delivery": asdict(orders.summarise(earlier)) if earlier else None,
                "basket": asdict(basket), "confirmed_lines": [asdict(line) for line in confirmed.lines], "confirmed_quantities": {line.sku: line.quantity for line in confirmed.lines},
                "queue": {**queue, **scope}, "change_products": change_products}


@router.get("/search")
def search(q: str, session: Session = Depends(get_session)):
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Search for something")
    with _operation(session):
        client = shared.make_client(shared.require_account(session).key)
        return {**_scope(session), "query": q, "products": [asdict(product) for product in shop.search_results(client.search(q, size=20), 20)]}


@router.get("/products/{sku}")
def product_details(sku: str = Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$"),
                    session: Session = Depends(get_session), retailer: str = Depends(get_active_retailer)):
    with _operation(session):
        client = shared.make_client(shared.require_account(session).key)
        rows = client.products([sku])
        raw = next((row for row in rows if str(row.get("productId")) == sku), None)
        products = shop.search_results({"products": [raw]} if raw else {}, 1)
        if not products:
            raise shared.SharedError("This product is no longer available on Ocado", 404)
        text = None
        unavailable = False
        product_id = str(raw.get("retailerProductId") or "")
        if product_id:
            try:
                text = client.product_description(product_id)
            except Exception:
                unavailable = True
        # Ocado's guaranteed minimum life; the catalogue's copy covers a live read without it.
        life_raw, life_days = parse_shelf_life(raw.get("guaranteedProductLife"))
        if life_days is None:
            cached = session.scalar(select(Product).where(Product.retailer == retailer, Product.sku == sku))
            life_raw, life_days = (cached.shelf_life_raw, cached.shelf_life_days) if cached else (None, None)
        return {**_scope(session), "product": {**asdict(products[0]), "description": text,
                                              "description_unavailable": unavailable,
                                              "shelf_life": life_raw, "shelf_life_days": life_days}}


@router.get("/slots")
def slots(session: Session = Depends(get_session), factory: sessionmaker[Session] = Depends(get_session_factory),
          target_id: str | None = Query(default=None, min_length=1, max_length=128)):
    with _operation(session):
        client = shared.make_client(shared.require_account(session).key)
        _, order, _, _ = _selection(client, QueueStore(factory), target_id)
        _require_editable(order)
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
        current_target, order, queue, _ = _selection(client, store, body.target_id)
        _require_editable(order)
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
        basket = shared.project(confirmed, queue["actions"] if queue["target_id"] == current_target else [])
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
        current_target, order, _, _ = _selection(client, store, body.target_id)
        _require_editable(order)
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


class UndoIn(SelectionIn):
    target_id: str = Field(min_length=1, max_length=128)
    action_id: str = Field(min_length=1, max_length=128)


@router.post("/queue/undo", dependencies=[Depends(require_person)])
def undo(body: UndoIn, session: Session = Depends(get_session),
         factory: sessionmaker[Session] = Depends(get_session_factory)):
    with _operation(session, blocking=False):
        _check_selection(session, body)
        shared.require_account(session)
        return {**QueueStore(factory).undo(body.target_id, body.action_id), **_scope(session)}

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


class ReservedRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")
    name: str = Field(min_length=1, max_length=500)
    quantity: int = Field(ge=0, le=50, strict=True)


class ReservedRulesIn(SelectionIn):
    enabled: bool = Field(strict=True)
    rules: list[ReservedRuleIn] = Field(max_length=200)


@router.get("/reserved")
def reserved_rules(session: Session = Depends(get_session)):
    from app.ocado.reserved import snapshot

    with _operation(session):
        account = shared.require_account(session)
        return {**_scope(session), **snapshot(session, account.id)}


@router.post("/reserved", dependencies=[Depends(require_person)])
def save_reserved_rules(body: ReservedRulesIn, session: Session = Depends(get_session),
                        user: User = Depends(get_current_user)):
    import json
    from app.db.models import OcadoReservedRules
    from app.ocado.reserved import snapshot

    if len({rule.sku for rule in body.rules}) != len(body.rules):
        raise HTTPException(status_code=400, detail="Each product can only have one Reserved rule")
    if body.enabled and not body.rules:
        raise HTTPException(status_code=400, detail="Add a product rule before enabling Reserved adjustments")
    with _operation(session, blocking=False):
        _check_selection(session, body)
        account = shared.require_account(session)
        row = session.scalar(select(OcadoReservedRules).where(OcadoReservedRules.account_id == account.id))
        if row is None:
            row = OcadoReservedRules(account_id=account.id, user_id=user.id)
            session.add(row)
        row.user_id, row.enabled = user.id, body.enabled
        row.rules_json = json.dumps([rule.model_dump() for rule in body.rules])
        session.commit()
        return {**_scope(session), **snapshot(session, account.id)}
