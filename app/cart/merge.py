"""Reconcile a planner basket into a retailer's cart quantities.

The cart is shared: the week's recipes go in alongside whatever else you are
buying, and a sync has to leave that alone. So this is a three-way merge, not an
overwrite. ``L`` is what the last sync recorded putting in (the ledger), ``C`` is
the cart now, ``T`` is what the week wants now::

    mine  = max(0, C - L)        # anything above HF's claim is yours
    goal  = mine + T
    delta = goal - C

Every behaviour falls out of that one clamp: items you added are untouched
(``T`` says nothing about them, so ``goal == C``), items you deleted come back
(``mine`` is 0, so the full ``T`` is re-added), items you bought more of are left
alone (``mine`` absorbs the excess), and dropping a recipe removes only the packs
the ledger claims. See :func:`merge_cart`.

Nothing here knows which shop it is reconciling. The merge is arithmetic on
``{sku: quantity}``; what differs between retailers is only how the cart is read
and how a change is expressed — Ocado takes deltas (:mod:`app.ocado.sync`),
Sainsbury's takes absolute quantities (:mod:`app.sainsburys.sync`) — and both
drive the same merge to decide what those changes should be.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.planner.basket import Basket


@dataclass(frozen=True, slots=True)
class LedgerLine:
    """One product the last sync put in the cart, in the terms it was bought in."""

    sku: str
    quantity: int
    name: str | None = None
    ingredient: str | None = None
    ingredient_key: str | None = None


@dataclass(frozen=True, slots=True)
class CartLedger:
    """What the last sync believes it contributed, and whether there was one.

    ``synced`` is not decoration: an empty ledger means "HF owns nothing in this
    cart", which is true after a checkout and false before the first sync ever
    ran. Told apart, the first sync can assume the packs already in the cart that
    it was about to buy are its own from a pre-ledger push, instead of doubling
    them. See :func:`merge_cart`.
    """

    lines: tuple[LedgerLine, ...] = ()
    synced: bool = False
    #: When the last sync ran and which week it was for. Carried for reporting
    #: only - a claim is no less true for being three weeks old, and the merge
    #: never looks at these.
    synced_at: datetime | None = None
    week_start: str | None = None

    @property
    def quantities(self) -> dict[str, int]:
        return {line.sku: line.quantity for line in self.lines}

    def line(self, sku: str) -> LedgerLine | None:
        return next((line for line in self.lines if line.sku == sku), None)


@dataclass(frozen=True, slots=True)
class CartMerge:
    """The three-way merge, before anything is written."""

    #: What to change, as a signed delta per SKU. Never touches a product HF does
    #: not claim. A retailer whose API takes absolute quantities adds these to
    #: what the cart already holds - see :meth:`goals`.
    deltas: dict[str, int]
    #: ``{sku: quantity}`` the merge attributes to you, and leaves alone.
    yours: dict[str, int]
    #: ``{sku: (was, becomes)}`` for HF items you deleted or reduced, being put
    #: back. Reported rather than done silently - the merge cannot tell "I
    #: deleted this" from "I only wanted one".
    restored: dict[str, tuple[int, int]]
    #: The ledger the merge actually ran against, seeded if this is a first sync.
    ledger: dict[str, int]

    def goals(self, cart: dict[str, int]) -> dict[str, int]:
        """The absolute quantity each changed SKU should end up at.

        For the retailers whose baskets are set rather than nudged. Derived from
        the cart the merge ran against, so it must be handed the same one.
        """
        return {sku: cart.get(sku, 0) + delta for sku, delta in self.deltas.items()}


def merge_cart(
    ledger: dict[str, int],
    cart: dict[str, int],
    targets: dict[str, int],
    *,
    synced: bool = True,
) -> CartMerge:
    """Merge the week's targets into a cart that is not only ours.

    A first sync (``synced=False``) seeds the ledger with ``min(cart, targets)``:
    without it, packs an older pre-ledger push already put in the cart read as
    yours and get bought a second time. It is a one-off, and it is wrong only in
    the harmless direction - the worst case is HF adopting a pack you had chosen
    yourself, which it then keeps buying for you.
    """
    if not synced:
        ledger = {
            sku: min(cart[sku], want) for sku, want in targets.items() if cart.get(sku)
        }
    skus = set(ledger) | set(cart) | set(targets)
    mine = {sku: max(0, cart.get(sku, 0) - ledger.get(sku, 0)) for sku in skus}
    goals = {sku: mine[sku] + targets.get(sku, 0) for sku in skus}
    deltas = {
        sku: goals[sku] - cart.get(sku, 0)
        for sku in sorted(skus)
        if goals[sku] != cart.get(sku, 0)
    }
    restored = {
        sku: (cart.get(sku, 0), goals[sku])
        for sku in sorted(skus)
        # Only a shortfall against what HF had already put in is a restoration;
        # a product the week has newly asked for is just an addition.
        if targets.get(sku, 0) and cart.get(sku, 0) < ledger.get(sku, 0)
    }
    return CartMerge(
        deltas=deltas,
        yours={sku: qty for sku, qty in sorted(mine.items()) if qty},
        restored=restored,
        ledger=dict(ledger),
    )


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
    #: The net change the push made per SKU, whether the retailer's API was told
    #: in deltas or in absolute quantities.
    deltas: dict[str, int] = field(default_factory=dict)
    #: Products the merge attributed to you and did not touch. Saying so is what
    #: makes a sync that deletes things trustworthy without checking up on it.
    yours: list[PushLine] = field(default_factory=list)
    #: HF items you had deleted or reduced, put back to what the week needs.
    restored: list[PushLine] = field(default_factory=list)
    #: HF items the week no longer needs, taken back out.
    removed: list[PushLine] = field(default_factory=list)
    #: The basket the last push actually worked from - a re-covered one if the
    #: first attempt hit something the shop would not sell.
    basket: Basket | None = None
    retried: bool = False
    #: What HF now claims in the cart, read back from it rather than assumed.
    #: The caller persists this; a ledger that over-claims is the only way this
    #: design deletes something of yours.
    ledger: CartLedger = field(default_factory=CartLedger)


@dataclass(frozen=True, slots=True)
class PushPlan:
    """What a push would do to the cart, before it does it."""

    added: list[PushLine] = field(default_factory=list)
    removed: list[PushLine] = field(default_factory=list)
    restored: list[PushLine] = field(default_factory=list)
    yours: list[PushLine] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    deltas: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LineRef:
    """Which basket line a SKU is being bought for."""

    key: str
    name: str


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


def claims_from_cart(
    after: dict[str, int],
    mine: dict[str, int],
    targets: dict[str, int],
    ledger: dict[str, int],
) -> dict[str, int]:
    """What HF still claims of each product once the cart has been re-read.

    Read back from the cart rather than assumed from intent, so a refusal or a
    partial fill leaves a truthful ledger. Capped by what was asked for as well
    as by what is there: a ledger that over-claims is the one failure that makes
    the next sync delete something of yours, so it is never allowed to grow past
    its own request. A product the week has dropped is capped at the old claim,
    which keeps a *removal* the cart refused HF's own problem to retry rather
    than silently reassigning it to you.
    """
    claims = {}
    for sku in sorted(set(targets) | set(ledger)):
        cap = targets.get(sku, ledger.get(sku, 0))
        quantity = max(0, min(cap, after.get(sku, 0) - mine.get(sku, 0)))
        if quantity:
            claims[sku] = quantity
    return claims


def ledger_from_claims(
    claims: dict[str, int],
    names: dict[str, str],
    origins: dict[str, LineRef],
    previous: CartLedger,
) -> CartLedger:
    lines = []
    for sku, quantity in claims.items():
        origin = origins.get(sku)
        was = previous.line(sku)
        lines.append(
            LedgerLine(
                sku=sku,
                quantity=quantity,
                name=names.get(sku) or (was.name if was else None),
                ingredient=origin.name if origin else (was.ingredient if was else None),
                ingredient_key=origin.key if origin else (was.ingredient_key if was else None),
            )
        )
    return CartLedger(lines=tuple(lines), synced=True)


def ledger_line(ledger: CartLedger, sku: str, quantity: int) -> PushLine:
    """A push line described from the ledger, for products no longer in the plan.

    "Removed 1 x Chorizo Ring" says nothing about why; "for Chorizo, which you
    dropped with the paella" is the whole reason the ledger carries the
    ingredient it was bought for.
    """
    was = ledger.line(sku)
    return PushLine(
        sku=sku,
        quantity=quantity,
        name=was.name if was else None,
        ingredient=was.ingredient if was else None,
        ingredient_key=was.ingredient_key if was else None,
    )


def merge_deltas(first: dict[str, int], second: dict[str, int]) -> dict[str, int]:
    merged = dict(first)
    for sku, delta in second.items():
        merged[sku] = merged.get(sku, 0) + delta
    return {sku: delta for sku, delta in sorted(merged.items()) if delta}


def merge_lines(first: list[PushLine], second: list[PushLine]) -> list[PushLine]:
    """Both rounds' lines, the retry's version of a SKU winning."""
    merged = {line.sku: line for line in first}
    merged.update({line.sku: line for line in second})
    return [merged[sku] for sku in sorted(merged)]


def plan_from_merge(
    merge: CartMerge,
    ledger: CartLedger,
    *,
    targets: dict[str, int],
    current: dict[str, int],
    names: dict[str, str],
    origins: dict[str, LineRef],
    unmapped: list[str],
) -> PushPlan:
    """Describe a merge as the plan it implies, without touching the cart.

    Worth having only because a sync now deletes things: "adds 4, removes 1,
    leaves your 6 alone" is the sentence that makes that safe to press.
    """

    def target_line(sku: str, quantity: int) -> PushLine:
        origin = origins.get(sku)
        return PushLine(
            sku=sku,
            quantity=quantity,
            name=names.get(sku),
            ingredient=origin.name if origin else None,
            ingredient_key=origin.key if origin else None,
            wanted=targets.get(sku),
            got=current.get(sku, 0),
        )

    return PushPlan(
        added=[
            target_line(sku, delta)
            for sku, delta in merge.deltas.items()
            if delta > 0 and sku not in merge.restored
        ],
        removed=[
            ledger_line(ledger, sku, -delta)
            for sku, delta in merge.deltas.items()
            if delta < 0
        ],
        restored=[
            ledger_line(ledger, sku, becomes - was)
            for sku, (was, becomes) in merge.restored.items()
        ],
        yours=[
            PushLine(sku=sku, quantity=qty, name=names.get(sku))
            for sku, qty in merge.yours.items()
        ],
        unmapped=unmapped,
        deltas=merge.deltas,
    )
