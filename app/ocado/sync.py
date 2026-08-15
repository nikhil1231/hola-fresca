"""Push a planner basket into the Ocado cart.

The three-way merge that decides *what* to change lives in
:mod:`app.cart.merge`, and is shared with every other shop a basket can be
pushed to. What is Ocado's own, and stays here, is the two ends of it: reading
quantities out of the three differently-shaped basket payloads
(:func:`cart_quantities`), and expressing a change as the signed deltas
``apply-quantity`` takes.
"""
from __future__ import annotations

from typing import Any, Callable

from app.cart.merge import (
    CartLedger,
    CartMerge,
    LedgerLine,
    LineRef,
    PushLine,
    PushPlan,
    PushResult,
    basket_targets,
    claims_from_cart,
    ledger_from_claims,
    ledger_line,
    merge_cart,
    merge_deltas,
    merge_lines,
    plan_from_merge,
)
from app.ocado.client import OcadoClient
from app.planner.basket import Basket

#: Re-exported so callers that only ever needed the merge keep importing it from
#: the module that used to own it.
__all__ = [
    "CartLedger",
    "CartMerge",
    "LedgerLine",
    "LineRef",
    "PushLine",
    "PushPlan",
    "PushResult",
    "basket_targets",
    "cart_quantities",
    "merge_cart",
    "plan_push",
    "push_basket",
    "refusal_reasons",
]


def push_basket(
    client: OcadoClient,
    basket: Basket,
    *,
    ledger: CartLedger | None = None,
    owned_item_keys: set[str] | None = None,
    recover: Callable[[list[str]], Basket | None] | None = None,
) -> PushResult:
    """Merge the basket into the cart, substituting around anything it refuses.

    ``recover`` is handed the SKUs the cart would not take and returns a basket
    covered without them - which is where the substitution actually happens, in
    the planner, with the whole week's demand in view. It is called at most once:
    a second refusal is reported rather than chased, because by then the useful
    thing is to say what is missing, not to keep spending requests on it.
    """
    ledger = ledger or CartLedger()
    result = _push_once(client, basket, ledger=ledger, owned_item_keys=owned_item_keys)
    if not result.dropped or recover is None:
        return result

    revised = recover([line.sku for line in result.dropped])
    if revised is None:
        return result

    # The retry merges against the ledger the first round produced, not the one
    # it started from: the cart has already moved, and re-using the stale ledger
    # would read HF's own additions as yours and refuse to touch them again.
    retry = _push_once(
        client, revised, ledger=result.ledger, owned_item_keys=owned_item_keys
    )
    return PushResult(
        applied=retry.applied,
        dropped=retry.dropped,
        unmapped=retry.unmapped,
        # Both rounds moved the cart, so the report has to cover both.
        deltas=merge_deltas(result.deltas, retry.deltas),
        yours=retry.yours,
        restored=merge_lines(result.restored, retry.restored),
        removed=merge_lines(result.removed, retry.removed),
        basket=revised,
        retried=True,
        ledger=retry.ledger,
    )


def plan_push(
    client: OcadoClient,
    basket: Basket,
    *,
    ledger: CartLedger | None = None,
    owned_item_keys: set[str] | None = None,
    cart_payload: Any | None = None,
) -> PushPlan:
    """The same merge a push runs, reported instead of applied.

    Worth having only because a sync now deletes things: "adds 4, removes 1,
    leaves your 6 alone" is the sentence that makes that safe to press. It reads
    the cart and nothing else - no stock check, no writes.
    """
    ledger = ledger or CartLedger()
    current = cart_quantities(client.cart_view() if cart_payload is None else cart_payload)
    targets, names, origins, unmapped = basket_targets(
        basket, owned_item_keys=owned_item_keys
    )
    merge = merge_cart(ledger.quantities, current, targets, synced=ledger.synced)
    return plan_from_merge(
        merge,
        ledger,
        targets=targets,
        current=current,
        names=names,
        origins=origins,
        unmapped=unmapped,
    )


def _push_once(
    client: OcadoClient,
    basket: Basket,
    *,
    ledger: CartLedger,
    owned_item_keys: set[str] | None = None,
) -> PushResult:
    current = cart_quantities(client.cart_view())
    targets, names, origins, unmapped = basket_targets(
        basket, owned_item_keys=owned_item_keys
    )
    merge = merge_cart(ledger.quantities, current, targets, synced=ledger.synced)
    mine = merge.yours
    reasons: dict[str, str] = {}
    if merge.deltas:
        reasons = refusal_reasons(client.apply_quantity(merge.deltas))
    after = cart_quantities(client.cart_view())

    def hf_share(sku: str) -> int:
        """What landed for HF, with your own copies of the product set aside."""
        return max(0, after.get(sku, 0) - mine.get(sku, 0))

    def line(sku: str) -> PushLine:
        got = hf_share(sku)
        want = targets[sku]
        origin = origins.get(sku)
        return PushLine(
            sku=sku,
            # Applied lines report the quantity bought; shortfalls report what is
            # missing, which is the number worth acting on.
            quantity=got if got >= want else want - got,
            name=names.get(sku),
            ingredient=origin.name if origin else None,
            ingredient_key=origin.key if origin else None,
            wanted=want,
            got=got,
            reason=reasons.get(sku) if got < want else None,
        )

    applied = [line(sku) for sku in sorted(targets) if hf_share(sku) >= targets[sku]]
    dropped = [line(sku) for sku in sorted(targets) if hf_share(sku) < targets[sku]]
    claims = claims_from_cart(after, mine, targets, merge.ledger)
    return PushResult(
        applied=applied,
        dropped=dropped,
        unmapped=unmapped,
        deltas=merge.deltas,
        yours=[
            PushLine(sku=sku, quantity=qty, name=names.get(sku))
            for sku, qty in merge.yours.items()
        ],
        restored=[
            ledger_line(ledger, sku, becomes - was)
            for sku, (was, becomes) in merge.restored.items()
        ],
        removed=[
            ledger_line(ledger, sku, claimed - claims.get(sku, 0))
            for sku, claimed in sorted(merge.ledger.items())
            if claimed > claims.get(sku, 0)
        ],
        basket=basket,
        ledger=ledger_from_claims(claims, names, origins, ledger),
    )








def refusal_reasons(payload: Any) -> dict[str, str]:
    """Why the cart would not take something, straight from its own answer.

    ``apply-quantity`` says so itself - ``unavailableData`` for what it will not
    sell, ``limitedItems`` for what it will not sell *this much of* - and the
    older code threw the whole response away, which is why every failure looked
    the same. The shapes are read defensively: an unrecognised entry still yields
    the product id and a bare label, which beats reporting nothing.
    """
    reasons: dict[str, str] = {}
    if not isinstance(payload, dict):
        return reasons
    for key, default in (
        ("unavailableData", "unavailable"),
        ("limitedItems", "quantity limited"),
        ("pricingNotifications", "price changed"),
    ):
        for entry in _dicts(payload.get(key)):
            sku = entry.get("productId")
            if isinstance(sku, str) and sku:
                reasons.setdefault(sku, _reason_text(entry) or default)
    for item in _cart_items(payload):
        sku = item.get("productId")
        if isinstance(sku, str) and item.get("maxQuantityReached") is True:
            reasons.setdefault(sku, "maximum quantity reached")
    return reasons


def _reason_text(entry: dict[str, Any]) -> str | None:
    for key in ("reason", "message", "type", "status", "code"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("_", " ").lower()
    return None


def cart_quantities(payload: Any) -> dict[str, int]:
    """Read ``{productId: quantity}`` out of any of Ocado's basket payloads.

    The three that matter nest their lines differently, so the paths are spelled
    out rather than searched for - a generic walker silently returns ``{}`` on
    cart-view, which reads as "basket empty" and makes every push a double-add:

    * ``cart-view``      → ``checkoutGroups.assignedCheckoutGroups[].itemGroups[].items[]``
    * ``checkout-walk``  → ``items[]``
    * ``apply-quantity`` → ``basketUpdateResult.itemGroups[].items[]``
    """
    quantities: dict[str, int] = {}
    for item in _cart_items(payload):
        sku = item.get("productId")
        quantity = _quantity(item)
        if isinstance(sku, str) and sku and quantity is not None:
            quantities[sku] = quantity
    return quantities


def _cart_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items: list[dict[str, Any]] = []

    groups = payload.get("checkoutGroups")
    if isinstance(groups, dict):
        for group in _dicts(groups.get("assignedCheckoutGroups")):
            items.extend(_item_group_items(group))

    update = payload.get("basketUpdateResult")
    if isinstance(update, dict):
        items.extend(_item_group_items(update))

    items.extend(_item_group_items(payload))

    # checkout-walk carries its lines at the top level; its itemGroups hold bare
    # product-id strings, which _item_group_items already skips.
    items.extend(_dicts(payload.get("items")))

    deduped: dict[int, dict[str, Any]] = {id(item): item for item in items}
    return list(deduped.values())


def _item_group_items(node: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for group in _dicts(node.get("itemGroups")):
        items.extend(_dicts(group.get("items")))
    return items


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _quantity(item: dict[str, Any]) -> int | None:
    value = item.get("quantity")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
