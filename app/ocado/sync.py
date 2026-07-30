"""Reconcile a planner basket into Ocado cart quantities."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.ocado.client import OcadoClient
from app.planner.basket import Basket


@dataclass(frozen=True, slots=True)
class PushLine:
    """One product's fate in the push, in the ingredient's terms as well as its own.

    A drop reported only as "Mitake Irigoma Shiro Roasted White Sesame Seeds" is
    a brand name and a shrug: it says nothing about which ingredient is now
    missing, or whether the shortfall was total. ``ingredient``, ``wanted`` and
    ``got`` are what make it actionable.
    """

    sku: str
    quantity: int
    name: str | None = None
    ingredient: str | None = None
    ingredient_key: str | None = None
    wanted: int | None = None
    got: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PushResult:
    applied: list[PushLine] = field(default_factory=list)
    dropped: list[PushLine] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    deltas: dict[str, int] = field(default_factory=dict)
    #: The basket the last push actually worked from - a re-covered one if the
    #: first attempt hit something Ocado would not sell.
    basket: Basket | None = None
    retried: bool = False


@dataclass(frozen=True, slots=True)
class LineRef:
    """Which basket line a SKU is being bought for."""

    key: str
    name: str


def push_basket(
    client: OcadoClient,
    basket: Basket,
    *,
    owned_item_keys: set[str] | None = None,
    recover: Callable[[list[str]], Basket | None] | None = None,
) -> PushResult:
    """Make the cart match the basket, substituting around anything it refuses.

    ``recover`` is handed the SKUs the cart would not take and returns a basket
    covered without them - which is where the substitution actually happens, in
    the planner, with the whole week's demand in view. It is called at most once:
    a second refusal is reported rather than chased, because by then the useful
    thing is to say what is missing, not to keep spending requests on it.
    """
    result = _push_once(client, basket, owned_item_keys=owned_item_keys)
    if not result.dropped or recover is None:
        return result

    revised = recover([line.sku for line in result.dropped])
    if revised is None:
        return result

    retry = _push_once(client, revised, owned_item_keys=owned_item_keys)
    return PushResult(
        applied=retry.applied,
        dropped=retry.dropped,
        unmapped=retry.unmapped,
        # Both rounds moved the cart, so the report has to cover both.
        deltas=_merge_deltas(result.deltas, retry.deltas),
        basket=revised,
        retried=True,
    )


def _push_once(
    client: OcadoClient,
    basket: Basket,
    *,
    owned_item_keys: set[str] | None = None,
) -> PushResult:
    current = cart_quantities(client.cart_view())
    targets, names, origins, unmapped = basket_targets(
        basket, owned_item_keys=owned_item_keys
    )
    deltas = {
        sku: targets.get(sku, 0) - current.get(sku, 0)
        for sku in sorted(set(current) | set(targets))
    }
    deltas = {sku: delta for sku, delta in deltas.items() if delta}
    reasons: dict[str, str] = {}
    if deltas:
        reasons = refusal_reasons(client.apply_quantity(deltas))
    after = cart_quantities(client.cart_view())

    def line(sku: str) -> PushLine:
        got = after.get(sku, 0)
        want = targets[sku]
        origin = origins.get(sku)
        return PushLine(
            sku=sku,
            # Applied lines report the cart quantity; shortfalls report what is
            # missing from it, which is the number worth acting on.
            quantity=got if got == want else want - got,
            name=names.get(sku),
            ingredient=origin.name if origin else None,
            ingredient_key=origin.key if origin else None,
            wanted=want,
            got=got,
            reason=reasons.get(sku) if got != want else None,
        )

    applied = [line(sku) for sku in sorted(targets) if after.get(sku, 0) == targets[sku]]
    dropped = [line(sku) for sku in sorted(targets) if after.get(sku, 0) != targets[sku]]
    return PushResult(
        applied=applied, dropped=dropped, unmapped=unmapped, deltas=deltas, basket=basket
    )


def _merge_deltas(first: dict[str, int], second: dict[str, int]) -> dict[str, int]:
    merged = dict(first)
    for sku, delta in second.items():
        merged[sku] = merged.get(sku, 0) + delta
    return {sku: delta for sku, delta in sorted(merged.items()) if delta}


def basket_targets(
    basket: Basket,
    *,
    owned_item_keys: set[str] | None = None,
) -> tuple[dict[str, int], dict[str, str], dict[str, LineRef], list[str]]:
    targets: dict[str, int] = {}
    names: dict[str, str] = {}
    origins: dict[str, LineRef] = {}
    # Sold-out ingredients are left out of ``unmapped`` deliberately: they are
    # reported on their own, as a fact about today's shelves rather than about
    # the mapping, and listing them twice reads as two different problems.
    unmapped = list(basket.unmapped) + list(basket.unpriceable)
    owned_item_keys = owned_item_keys or set()
    for line in basket.lines:
        if line.key in owned_item_keys:
            continue
        if line.external:
            continue
        if line.cover is None:
            unmapped.append(line.name)
            continue
        for choice in line.cover.choices:
            targets[choice.pack.sku] = targets.get(choice.pack.sku, 0) + choice.count
            names.setdefault(choice.pack.sku, choice.pack.product_name)
            origins.setdefault(choice.pack.sku, LineRef(key=line.key, name=line.name))
    return targets, names, origins, sorted(set(unmapped))


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
