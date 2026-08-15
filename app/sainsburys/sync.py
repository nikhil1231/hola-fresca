"""Push a planner basket into the Sainsbury's trolley.

The merge is the shared one (:mod:`app.cart.merge`) — the same three-way clamp
that keeps Ocado's sync from touching what you put in yourself. What differs is
how a decision is carried out, and it differs in two ways worth knowing about.

**Quantities are absolute.** Ocado is told ``+1``; Sainsbury's is told "make it
three". The merge still thinks in deltas, because that is the honest description
of a change to a shared cart, and :meth:`CartMerge.goals` converts at the last
moment against the cart the merge actually ran against. Sending a delta where a
quantity belongs is a silent wrong order rather than an error, which is why the
conversion happens in one place.

**Lines are changed one at a time.** There is no bulk apply: a product not in
the trolley is added, one already there is updated through its own line id, and
a removal is an update to zero. So a push is one request per changed product
rather than one for the lot. That also means a refusal is per-product and the
rest of the push still lands — which is the better failure, and is why each line
is attempted independently rather than abandoning the batch at the first error.
"""
from __future__ import annotations

import logging
from typing import Callable

from app.cart.merge import (
    CartLedger,
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
from app.planner.basket import Basket
from app.sainsburys.client import BasketError, BasketLine, SainsburysClient

log = logging.getLogger("holafresca.sainsburys")

RETAILER = "sainsburys"


def plan_push(
    client: SainsburysClient,
    basket: Basket,
    *,
    ledger: CartLedger | None = None,
    owned_item_keys: set[str] | None = None,
) -> PushPlan:
    """The same merge a push runs, reported instead of applied.

    Reads the trolley and nothing else - no stock check, no writes.
    """
    ledger = ledger or CartLedger()
    current = _quantities(client)
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


def push_basket(
    client: SainsburysClient,
    basket: Basket,
    *,
    ledger: CartLedger | None = None,
    owned_item_keys: set[str] | None = None,
    recover: Callable[[list[str]], Basket | None] | None = None,
) -> PushResult:
    """Merge the basket into the trolley, substituting around anything refused.

    ``recover`` is handed the SKUs the trolley would not take and returns a
    basket covered without them - which is where the substitution actually
    happens, in the planner, with the whole week's demand in view. It is called
    at most once: a second refusal is reported rather than chased, because by
    then the useful thing is to say what is missing.
    """
    ledger = ledger or CartLedger()
    result = _push_once(client, basket, ledger=ledger, owned_item_keys=owned_item_keys)
    if not result.dropped or recover is None:
        return result

    revised = recover([line.sku for line in result.dropped])
    if revised is None:
        return result

    # The retry merges against the ledger the first round produced, not the one
    # it started from: the trolley has already moved, and re-using the stale
    # ledger would read HF's own additions as yours and refuse to touch them.
    retry = _push_once(
        client, revised, ledger=result.ledger, owned_item_keys=owned_item_keys
    )
    return PushResult(
        applied=retry.applied,
        dropped=retry.dropped,
        unmapped=retry.unmapped,
        deltas=merge_deltas(result.deltas, retry.deltas),
        yours=retry.yours,
        restored=merge_lines(result.restored, retry.restored),
        removed=merge_lines(result.removed, retry.removed),
        basket=revised,
        retried=True,
        ledger=retry.ledger,
    )


def _push_once(
    client: SainsburysClient,
    basket: Basket,
    *,
    ledger: CartLedger,
    owned_item_keys: set[str] | None = None,
) -> PushResult:
    lines = {line.sku: line for line in client.lines()}
    current = {sku: line.quantity for sku, line in lines.items() if line.quantity > 0}
    targets, names, origins, unmapped = basket_targets(
        basket, owned_item_keys=owned_item_keys
    )
    merge = merge_cart(ledger.quantities, current, targets, synced=ledger.synced)
    mine = merge.yours

    reasons = _apply(client, merge.goals(current), lines)

    after = _quantities(client)

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


def _apply(
    client: SainsburysClient,
    goals: dict[str, int],
    lines: dict[str, BasketLine],
) -> dict[str, str]:
    """Set each changed product to its goal, and say why any of them would not.

    Reductions go first. A trolley has a size limit and a per-product cap, and
    clearing what the week no longer needs before adding what it does is what
    keeps a big swap from being refused for the space its own leftovers are
    taking.
    """

    def growing(sku: str) -> bool:
        held = lines[sku].quantity if sku in lines else 0
        return goals[sku] > held

    reasons: dict[str, str] = {}
    for sku in sorted(goals, key=lambda sku: (growing(sku), sku)):
        existing = lines.get(sku) or BasketLine(sku=sku, quantity=0)
        try:
            client.set_quantity(existing, goals[sku])
        except BasketError as exc:
            # One product the shop will not sell is not a reason to abandon the
            # rest of the week's shopping.
            log.info("Sainsbury's would not set %s to %s: %s", sku, goals[sku], exc)
            reasons[sku] = _reason_text(exc)
    return reasons


def _reason_text(error: BasketError) -> str:
    """Sainsbury's own words for a refusal, where it gives any.

    The basket API answers with an ``errors`` list carrying codes like
    ``PRODUCT_NOT_ORDERABLE`` and ``BASKET_ITEM_QUANTITY_EXCEEDED``. Read
    defensively: an unrecognised shape still yields something better than
    silence.
    """
    payload = error.payload
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list):
            for entry in errors:
                if not isinstance(entry, dict):
                    continue
                for key in ("detail", "message", "title", "code"):
                    value = entry.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip().replace("_", " ").lower()
    if error.status == 409:
        return "quantity limited"
    return "refused"


def _quantities(client: SainsburysClient) -> dict[str, int]:
    return {line.sku: line.quantity for line in client.lines() if line.quantity > 0}
