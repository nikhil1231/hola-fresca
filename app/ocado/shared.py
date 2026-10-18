"""The shared shop's identity and serialized upstream operations.

Every entry point, including reads that temporarily edit an order, holds the
same advisory lock. The lock is on disk rather than just in memory: two server
processes must not edit the same upstream cart at once. It is automatically
released on process death, which also lets the queue detect orphaned claims.
"""
from __future__ import annotations

import fcntl
import logging
import hashlib
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.db.models import Product, RetailerAccount, User
from app.api.cart import _push_lock
from app.ocado import amend, shop
from app.ocado.client import OcadoClient
from app.ocado.availability import fetch_statuses
from app.ocado.session import get_shared_session

log = logging.getLogger(__name__)


class SharedError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def account_key() -> str:
    # Never infer an account from a caller, the legacy default, or row order.
    key = config.OCADO_SHARED_ACCOUNT_KEY
    if not key or key != key.strip():
        raise SharedError("The shared Ocado account key is not configured correctly", 503)
    return key


@contextmanager
def account_lock(*, blocking: bool = True) -> Iterator[None]:
    digest = hashlib.sha256(account_key().encode()).hexdigest()
    directory = config.DATA_DIR / "ocado" / "shared-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{digest}.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise SharedError("The shared Ocado account is busy; try again when the current operation finishes") from exc
        try:
            # The planner can be configured to use this same registry key.
            # Its read/merge/write and temporary order edits must not overlap.
            lock = _push_lock("ocado")
            if not lock.acquire(blocking=blocking):
                raise SharedError("Ocado is busy applying another basket change")
            try:
                yield
            finally:
                lock.release()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def find_account(session: Session) -> RetailerAccount | None:
    return session.scalar(select(RetailerAccount).where(
        RetailerAccount.key == account_key(), RetailerAccount.retailer == "ocado"
    ))


def connect_account(session: Session, email: str) -> RetailerAccount:
    """Only login creates the account, owned by a non-login shared identity."""
    account = find_account(session)
    if account is not None:
        return account
    digest = hashlib.sha256(account_key().encode()).hexdigest()
    owner = User(email=f"ocado-shared-{digest}@internal.invalid", name="Shared Ocado account", is_admin=0)
    account = RetailerAccount(user_id=0, retailer="ocado", key=account_key(), email=email, status="never")
    try:
        session.add(owner)
        session.flush()
        account.user_id = owner.id
        session.add(account)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # A concurrent first login may have won. A key owned by another retailer
        # is a configuration error, not permission to choose some other account.
        account = find_account(session)
        if account is None:
            raise SharedError("The configured shared account key is already in use", 503) from exc
    return account


def require_account(session: Session) -> RetailerAccount:
    account = find_account(session)
    if account is None:
        raise SharedError("Sign in to the shared Ocado account first", 404)
    return account


def make_client(key: str) -> OcadoClient:
    return OcadoClient(get_shared_session(key))


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except ValueError:
        return None


def target(client: OcadoClient, pinned: str | None = None, *, now: datetime | None = None) -> tuple[str, dict | None]:
    coming = client.orders(pending=True)
    if pinned is not None and pinned != "cart":
        order = next((o for o in coming if str(o.get("orderId")) == pinned
                      and o.get("status") not in {"CANCELLED", "DELIVERED"}), None)
        if order is None:
            raise SharedError("The queued delivery is no longer available; cancel the queue and refresh before choosing a new target")
        return pinned, order
    now = now or datetime.now(timezone.utc)
    booked = []
    for order in coming:
        start = _date((order.get("dates") or {}).get("deliveryStartDate"))
        if start is not None and start >= now and order.get("status") not in {"CANCELLED", "DELIVERED"}:
            booked.append((start, str(order.get("orderId")), order))
    if booked:
        _, order_id, order = min(booked, key=lambda item: (item[0], item[1]))
        if pinned == "cart":
            raise SharedError("A delivery has been booked since this cart was selected; cancel the queue and refresh")
        return order_id, order
    return "cart", None


def _names(client: OcadoClient, session: Session, skus: list[str], retailer: str) -> dict[str, str]:
    known = dict(session.execute(select(Product.sku, Product.name).where(
        Product.retailer == retailer, Product.sku.in_(skus)
    )).all()) if skus else {}
    missing = [sku for sku in skus if sku not in known]
    if missing:
        try:
            known.update({sku: status.name for sku, status in
                          fetch_statuses(missing, session=client.session).items() if status.name})
        except Exception as exc:
            log.info("Shared Ocado basket could not name %d products: %s", len(missing), exc)
    return known


def _amount(value) -> float | None:
    try:
        return float(value.get("amount")) if isinstance(value, dict) else float(value)
    except (TypeError, ValueError):
        return None


def basket(client: OcadoClient, session: Session, order: dict | None, *, retailer: str) -> shop.Basket:
    if order is None:
        result = shop.basket(client.cart_view(), lambda skus: _names(client, session, skus, retailer))
        return replace(result, lines=tuple(replace(line, name=line.name or line.sku) for line in result.lines))
    # Decorated order reads do not open an edit session or disturb the active cart.
    raw = client.get_order(str(order["orderId"]))
    if not raw or str(raw.get("orderId", order["orderId"])) != str(order["orderId"]):
        raise SharedError("Ocado could not read this delivery's basket", 502)
    rows = raw.get("items") or []
    skus = [str(row["product"]) for row in rows if row.get("product") is not None]
    names = _names(client, session, skus, retailer)
    lines = []
    for row in rows:
        sku = str(row.get("product") or "")
        quantity = int(row.get("quantity") or 0)
        if sku and quantity > 0:
            price = row.get("totalPrice") or (row.get("totalPrices") or {}).get("finalPrice")
            lines.append(shop.Line(sku, names.get(sku) or row.get("name") or sku, quantity, _amount(price)))
    total = _amount((raw.get("orderTotals") or order.get("orderTotals") or {}).get("finalPrice"))
    return shop.Basket(tuple(lines), total, False, order_id=str(order["orderId"]))


def project(basket: shop.Basket, actions: list[dict]) -> shop.Basket:
    lines = {line.sku: line for line in basket.lines}
    for action in actions:
        if action["kind"] != "quantity":
            continue
        sku, quantity = action["sku"], action["quantity"]
        if not quantity:
            lines.pop(sku, None)
            continue
        old = lines.get(sku)
        # A changed line has no known live cost. In particular a new product is
        # never labelled free. Basket totals remain the upstream live estimate.
        cost = old.cost if old is not None and old.quantity == quantity else None
        lines[sku] = shop.Line(sku, action.get("name") or (old.name if old else sku), quantity, cost)
    return replace(basket, lines=tuple(lines.values()))


def slots(client: OcadoClient, order: dict | None) -> list[dict]:
    items = amend.slots_for(client, order) if order is not None else client.slots()
    return [{key: value for key, value in asdict(item).items() if key != "raw"} for item in items]


def checkout_cart(client: OcadoClient) -> None:
    """Real saved-card checkout; challenges and ambiguous results are failures."""
    cards = [card for card in client.wallet_items()
             if card.get("paymentMethodType") == "CARDS" and not card.get("expired") and card.get("walletItemId")]
    if len(cards) != 1:
        raise SharedError("New-cart checkout needs exactly one usable saved card; choose a card on ocado.com", 400)
    body = {"paymentMethod": {"group": "PAY_NOW", "type": "CARDS", "toSave": False,
                              "walletItemId": cards[0]["walletItemId"]}, "redirectUrl": amend.REDIRECT_URL}
    billing = client.default_billing_address_id()
    if billing:
        body["billingAddressId"] = billing
    client.checkout_start()
    group = (client.checkout_summary().get("activeCheckoutGroup") or {})
    restrictions = group.get("checkoutRestrictions") or []
    if restrictions or group.get("canCheckout") is not True:
        raise SharedError(f"Ocado cannot check out this basket: {', '.join(map(str, restrictions)) or 'basket not ready'}", 400)
    result = client.checkout(body)
    if result.get("type") != amend.CHECKOUT_DONE:
        raise SharedError("Ocado requires payment authentication or an unsupported checkout step. Check ocado.com before trying again", 400)
    order_id = result.get("orderId")
    if not order_id:
        raise SharedError("Checkout returned without a verifiable order. Check ocado.com before trying again", 502)
    saved = client.get_order(str(order_id))
    if not saved or str(saved.get("orderId")) != str(order_id) or saved.get("status") == "CANCELLED":
        raise SharedError("Checkout completed but the new order could not be verified. Check ocado.com before trying again", 502)


def execute(client: OcadoClient, target_id: str, actions: list[dict]) -> None:
    _, order = target(client, target_id)
    quantities = {action["sku"]: action["quantity"] for action in actions if action["kind"] == "quantity"}
    slot = next((action["slot_id"] for action in reversed(actions) if action["kind"] == "slot"), None)
    checkout = any(action["kind"] == "checkout" for action in actions)
    if order is not None:
        if quantities or slot:
            # This already confirms payment for the existing order. Never make a
            # second checkout because an explicit checkout action was also queued.
            amend.change_order(client, target_id, quantities=quantities or None, slot_id=slot)
        return  # an untouched booked delivery is already checked out
    if quantities:
        from app.ocado.sync import cart_quantities

        client.set_quantities(quantities)
        actual = cart_quantities(client.cart_view())
        if any(actual.get(sku, 0) != quantity for sku, quantity in quantities.items()):
            raise SharedError("Ocado did not save all the requested quantities; inspect the basket before trying again", 502)
    if slot:
        reserved = client.reserve(slot)
        if str((reserved.get("slot") or {}).get("slotId")) != slot:
            raise SharedError("Ocado did not confirm the requested delivery slot", 502)
    if checkout:
        checkout_cart(client)
