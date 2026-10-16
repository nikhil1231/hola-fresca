"""Change and re-check out an existing Ocado order, or cancel its delivery.

This is ocado.com's "Edit order" followed by "Change slot" and "Confirm changes",
in the calls the site itself makes:

1. open an order-edit session, which makes the order the active cart;
2. change quantities and/or reserve the new slot, as for any cart;
3. check out, which for an edited order confirms its changes. The order uses an available
   saved card (the account default when available) and, if it's
   a recurring order, the change is to this delivery only.

Ocado keeps the original order until changes are confirmed. Failures before
confirmation cancel the edit session. An ambiguous network failure or a failed
read after confirmation must be checked on Ocado rather than assumed reverted.
Payment forms and SDK sessions are continued in the site's normal browser flow,
including saved-card selection and confirmation controls. Only unavailable input,
an unresolved bank action, or an actual technical failure stops continuation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.ocado.client import OcadoClient
from app.ocado.payment import PaymentFlowError

log = logging.getLogger(__name__)

CHECKOUT_DONE = "checkout-result-response"
REDIRECT_URL = "https://www.ocado.com/checkout/3ds/end"


class OrderChangeError(RuntimeError):
    """The change failed or its final state could not be verified."""


@dataclass(frozen=True, slots=True)
class Moved:
    order_id: str
    slot_id: str
    delivery_start: str
    delivery_end: str


def find_order(client: OcadoClient, order_id: str) -> dict[str, Any] | None:
    return next((o for o in client.orders(pending=True) if str(o.get("orderId")) == str(order_id)), None)


def payment_method(client: OcadoClient, order_id: str) -> dict[str, Any]:
    """Use the account's default usable saved card, or its first usable card."""
    wallet = [w for w in client.wallet_items()
              if not w.get("expired") and w.get("paymentMethodType") == "CARDS" and w.get("walletItemId")]
    if not wallet:
        raise OrderChangeError("No unexpired saved card is available on the account")
    card = next((w for w in wallet if w.get("defaultWalletItem")), wallet[0])
    return {"group": card.get("paymentMethodGroup") or "PAY_NOW", "type": "CARDS",
            "toSave": False, "walletItemId": card["walletItemId"]}


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
    after = change_order(client, order_id, slot_id=slot_id)
    dates = after.get("dates") or {}
    return Moved(order_id, slot_id, dates.get("deliveryStartDate", ""), dates.get("deliveryEndDate", ""))


def change_order(client: OcadoClient, order_id: str, *,
                 quantities: dict[str, int] | None = None, slot_id: str | None = None) -> dict[str, Any]:
    """Edit basket lines and/or a slot, then re-check out the same order."""
    if not quantities and not slot_id:
        raise OrderChangeError("Supply basket quantities or a delivery slot")
    if quantities and any(isinstance(q, bool) or not isinstance(q, int) or q < 0 for q in quantities.values()):
        raise OrderChangeError("Basket quantities must be non-negative integers")
    order = find_order(client, order_id)
    if order is None:
        raise OrderChangeError(f"Order {order_id} isn't one of the coming orders")
    if order.get("editStatus") != "EDITABLE":
        raise OrderChangeError(f"Order {order_id} can't be changed any more ({order.get('editStatus')})")
    if not quantities and order.get("slotId") == slot_id:
        return order
    region, ddid = order["regionId"], order["deliveryDestinationId"]
    body = checkout_body(client, order)  # before touching anything: a missing card stops it here

    client.start_order_edit(order_id, region)
    try:
        if quantities:
            client.set_quantities(quantities)
        if slot_id and slot_id != order.get("slotId"):
            client.reserve(slot_id, ddid=ddid, region=region)
        client.checkout_start()
        summary = client.checkout_summary()
        group = summary.get("activeCheckoutGroup") or {}
        restrictions = group.get("checkoutRestrictions") or []
        if restrictions or group.get("canCheckout") is False:
            raise OrderChangeError(f"Ocado cannot check out these changes: {', '.join(restrictions) or 'basket not ready'}")
        result = client.checkout(body, order_id=str(order_id))
        if result.get("type") != CHECKOUT_DONE:
            raise OrderChangeError(f"Ocado did not complete checkout ({result.get('type') or 'missing response type'})")
        if str(result.get("orderId", order_id)) != str(order_id):
            raise OrderChangeError("Ocado returned a different order while confirming changes; check ocado.com")
    except PaymentFlowError as exc:
        _cancel(client, order_id, region)
        raise OrderChangeError(str(exc)) from exc
    except OrderChangeError:
        _cancel(client, order_id, region)
        raise
    except Exception as exc:
        _cancel(client, order_id, region)
        raise OrderChangeError("Ocado could not confirm the change; the edit was discarded where possible. "
                               "Check the order on ocado.com before retrying") from exc

    try:
        after = client.get_order(order_id) if quantities else find_order(client, order_id)
    except Exception as exc:
        raise OrderChangeError("Checkout completed but the saved order could not be read; check ocado.com") from exc
    if not after or (slot_id and after.get("slotId") != slot_id):
        raise OrderChangeError(f"Checkout completed but order {order_id}'s delivery could not be verified; check ocado.com")
    if quantities:
        actual = {str(item["product"]): item["quantity"] for item in after.get("items") or []}
        if any(actual.get(sku, 0) != qty for sku, qty in quantities.items()):
            raise OrderChangeError("Checkout completed but the order's basket differs from the requested quantities; check ocado.com")
    return after


def cancel_order(client: OcadoClient, order_id: str) -> dict[str, Any]:
    order = find_order(client, order_id)
    if order is None or not order.get("cancelable"):
        raise OrderChangeError(f"Order {order_id} isn't a cancellable coming order")
    try:
        result = client.cancel_order(order_id)
    except Exception as exc:
        raise OrderChangeError("Cancellation could not be confirmed; check the order on ocado.com before retrying") from exc
    if not result.get("success"):
        raise OrderChangeError(f"Ocado refused cancellation: {result.get('failureReason') or 'unknown reason'}")
    try:
        after = client.get_order(order_id)
    except Exception as exc:
        raise OrderChangeError("Cancellation accepted but the saved order could not be read; check ocado.com") from exc
    if after.get("status") != "CANCELLED":
        raise OrderChangeError("Cancellation requested but the order's cancelled status could not be verified; check ocado.com")
    return after


def _cancel(client: OcadoClient, order_id: str, region: str) -> None:
    try:
        client.cancel_order_edit(order_id, region)
    except Exception:  # noqa: BLE001 - unconfirmed changes lapse anyway; say so and move on
        log.warning("ocado: couldn't cancel the edit of order %s; Ocado drops unconfirmed changes", order_id,
                    exc_info=True)
