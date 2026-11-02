"""One saved set of absolute quantities, applied once per Reserved delivery.

step() shares the shop's cross-process lock with all reads/edits and the queue.
A running row survives a crash and resumes with its original rules. Completed
and warning rows are terminal, even when settings or order contents change.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import OcadoReservedRules, OcadoReservedRun, OcadoSharedQueue
from app.ocado import amend, shared
from app.ocado.sync import cart_quantities


def snapshot(db, account_id: int) -> dict:
    settings = db.scalar(select(OcadoReservedRules).where(OcadoReservedRules.account_id == account_id))
    runs = db.scalars(select(OcadoReservedRun).where(OcadoReservedRun.account_id == account_id)
                      .order_by(OcadoReservedRun.id.desc()).limit(30)).all()
    return {"revision": f"{runs[0].id}:{runs[0].status}:{runs[0].updated_at.isoformat()}" if runs else None,
            "enabled": bool(settings and settings.enabled),
            "rules": json.loads(settings.rules_json) if settings else [],
            "runs": [{"order_id": row.order_id, "status": row.status, "warning": row.warning,
                      "updated_at": row.updated_at.isoformat()} for row in runs]}


def step(factory, *, client_factory=None, now=None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        with shared.account_lock(blocking=False), factory() as db, shared.using_account(db):
            account = shared.find_account(db)
            if account is None:
                return False
            settings = db.scalar(select(OcadoReservedRules).where(OcadoReservedRules.account_id == account.id))
            if not settings or not settings.enabled:
                return False
            # Human edits take precedence, including failed or held batches.
            if db.scalar(select(OcadoSharedQueue.id).where(OcadoSharedQueue.account_id == account.id,
                         OcadoSharedQueue.status.in_({"pending", "running", "failed"}))):
                return False
            client = (client_factory or shared.make_client)(account.key)
            for order in shared.upcoming_orders(client, now=now):
                order_id = str(order.get("orderId") or "")
                cutoff = shared._date((order.get("dates") or {}).get("confirmOrderChangesBy"))
                if (not order_id or not isinstance(order.get("recurringShoppingDefinition"), dict)
                        or not order["recurringShoppingDefinition"].get("definitionId") or order.get("editStatus") != "EDITABLE"
                        or (cutoff and cutoff <= now)):
                    continue
                row = db.scalar(select(OcadoReservedRun).where(OcadoReservedRun.account_id == account.id,
                                                               OcadoReservedRun.order_id == order_id))
                if row and row.status != "running":
                    continue
                rules = json.loads(row.rules_json if row else settings.rules_json)
                if not rules:
                    continue
                # Never disturb a manually built basket or an open edit session.
                # Ocado assigns order ids even to new carts; an id alone is no proof.
                cart = client.cart_view()
                if row is not None and str((cart.get("activeCheckoutGroup") or {}).get("orderId") or "") == order_id:
                    # Only discard an edit positively tied to this interrupted
                    # run. A crash after checkout may have no edit to cancel.
                    client.cancel_order_edit(order_id, order["regionId"])
                    cart = client.cart_view()
                if cart_quantities(cart):
                    return False
                if row is None:
                    row = OcadoReservedRun(account_id=account.id, user_id=settings.user_id,
                                           order_id=order_id, rules_json=json.dumps(rules), status="running",
                                           updated_at=now)
                    db.add(row)
                    db.commit()  # durable claim BEFORE the first upstream mutation
                try:
                    saved = client.get_order(order_id)
                    if str(saved.get("orderId") or "") != order_id:
                        raise amend.OrderChangeError("Could not verify the Reserved order identity")
                    actual = {str(item["product"]): item["quantity"] for item in saved.get("items") or []}
                    quantities = {rule["sku"]: rule["quantity"] for rule in rules
                                  if actual.get(rule["sku"], 0) != rule["quantity"]}
                    if quantities:
                        amend.change_order(client, order_id, quantities=quantities, minimum_total=40)
                except Exception as exc:
                    row.status = "warning"
                    row.warning = f"Reserved order {order_id}: {exc}. Review this delivery; the rules will not run again automatically."
                else:
                    row.status, row.warning = "completed", None
                row.updated_at = now
                db.commit()
                return True
            return False
    except shared.SharedError:
        return False
