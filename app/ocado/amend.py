"""Moving an existing Ocado order to another delivery slot.

This is ocado.com's "Edit order" followed by "Change slot" and "Confirm changes",
in the calls the site itself makes:

1. open an order-edit session, which makes the order the active cart;
2. reserve the new slot, as for any cart;
3. check out, which for an edited order confirms its changes. The order keeps its
   payment (the saved card it was placed with, charged at delivery) and, if it's
   a recurring order, the change is to this delivery only.

Ocado keeps the original order until changes are confirmed, so every failure
cancels the edit session and the order is exactly as it was. That includes
Ocado asking for the payment to be authenticated (a card check run in a
browser): that is the customer's to do on ocado.com, never something to get
round, so it is reported, not attempted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.ocado.client import OcadoClient

log = logging.getLogger(__name__)

CHECKOUT_DONE = "checkout-result-response"
REDIRECT_URL = "https://www.ocado.com/checkout/3ds/end"


class OrderChangeError(RuntimeError):
    """The order couldn't be changed; it is as it was."""


@dataclass(frozen=True, slots=True)
class Moved:
    order_id: str
    slot_id: str
    delivery_start: str
    delivery_end: str


def find_order(client: OcadoClient, order_id: str) -> dict[str, Any] | None:
    return next((o for o in client.orders(pending=True) if str(o.get("orderId")) == str(order_id)), None)


def payment_method(client: OcadoClient, order_id: str) -> dict[str, Any]:
    """The order's own payment method, pointed at the saved card it uses."""
    payments = (client.order_payments(order_id).get("payments") or [{}])
    method = payments[0].get("paymentMethod") or {}
    group, kind = method.get("group") or "PAY_NOW", method.get("type") or "CARDS"
    wallet = [w for w in client.wallet_items() if not w.get("expired") and w.get("paymentMethodType") == kind]
    if not wallet:
        raise OrderChangeError(f"No saved {kind.lower()} on the account to keep paying with")
    return {"group": group, "type": kind, "toSave": False, "walletItemId": wallet[0]["walletItemId"]}


def checkout_body(client: OcadoClient, order: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"paymentMethod": payment_method(client, str(order["orderId"])), "redirectUrl": REDIRECT_URL}
    billing = client.default_billing_address_id()
    if billing:
        body["billingAddressId"] = billing
    if order.get("recurringShoppingDefinition"):
        body["recurringOrder"] = {"edit": {"replaceItems": False}}  # this delivery only, not the series
    return body


def slots_for(client: OcadoClient, order: dict[str, Any], days: int = 7) -> list:
    """The slots the order could move to: asked inside an edit session (they're the
    order's, not a new cart's), which is then dropped."""
    order_id, region = str(order["orderId"]), order["regionId"]
    client.start_order_edit(order_id, region)
    try:
        return client.slots(ddid=order["deliveryDestinationId"], region=region, days=days)
    finally:
        _cancel(client, order_id, region)


def move_slot(client: OcadoClient, order_id: str, slot_id: str) -> Moved:
    order = find_order(client, order_id)
    if order is None:
        raise OrderChangeError(f"Order {order_id} isn't one of the coming orders")
    if order.get("editStatus") != "EDITABLE":
        raise OrderChangeError(f"Order {order_id} can't be changed any more ({order.get('editStatus')})")
    if order.get("slotId") == slot_id:
        dates = order.get("dates") or {}
        return Moved(order_id, slot_id, dates.get("deliveryStartDate", ""), dates.get("deliveryEndDate", ""))
    region, ddid = order["regionId"], order["deliveryDestinationId"]
    body = checkout_body(client, order)  # before touching anything: a missing card stops it here

    client.start_order_edit(order_id, region)
    try:
        client.reserve(slot_id, ddid=ddid, region=region)
        client.checkout_start()
        client.checkout_summary()
        result = client.checkout(body)
        if result.get("type") != CHECKOUT_DONE:
            raise OrderChangeError("Ocado wants the payment confirmed on ocado.com before it'll take the change")
    except OrderChangeError:
        _cancel(client, order_id, region)
        raise
    except Exception as exc:
        _cancel(client, order_id, region)
        raise OrderChangeError(f"Ocado refused the change: {exc}") from exc

    after = find_order(client, order_id) or {}
    dates = after.get("dates") or {}
    if after.get("slotId") != slot_id:
        raise OrderChangeError(f"Ocado accepted the change but order {order_id} still shows its old slot")
    return Moved(order_id, slot_id, dates.get("deliveryStartDate", ""), dates.get("deliveryEndDate", ""))


def _cancel(client: OcadoClient, order_id: str, region: str) -> None:
    try:
        client.cancel_order_edit(order_id, region)
    except Exception:  # noqa: BLE001 - unconfirmed changes lapse anyway; say so and move on
        log.warning("ocado: couldn't cancel the edit of order %s; Ocado drops unconfirmed changes", order_id,
                    exc_info=True)
