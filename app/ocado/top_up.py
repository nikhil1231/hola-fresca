"""Adding things to the next Ocado order by name: "add oat milk to the shopping".

Noodle hears the request and sends the item as words. Everything after that is
here, because turning words into a product is the question the mappings already
answer for recipes: which ingredient this is (following aliases), which of its
accepted products to buy (a standing pack choice first, then the one the
household has bought most, then the reviewed rank, sold out ones skipped), and
the order it goes on.

The mappings come from recipe ingredients, so "milk" and "chopped tomatoes" are
in them and "toilet roll" is not. Something that isn't mapped is reported back
rather than guessed at from a search: a wrong product on a confirmed order is
worse than being told to add it by hand.

Nothing is bought on a guess. An exact name (or alias) wins outright, and so does
the only name holding every word of the request. Otherwise a model picks from a
shortlist of the likeliest names, and may answer that none of them is it; with no
model to ask, the request is reported back unmatched.
"""
from __future__ import annotations

import difflib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.canonicalize import normalize_name
from app.db.models import (
    IngredientMapping,
    IngredientMappingProduct,
    Product,
    RetailerAccount,
)
from app.mapping import service
from app.mapping.openai_client import Completer, OpenAIJSONClient
from app.ocado import amend, purchases
from app.ocado.client import OcadoClient
from app.planner.basket import MATCH_TYPE_PREFERENCE
from app.planner.preferences import pack_preferences

log = logging.getLogger(__name__)

RETAILER = "ocado"
#: A misheard "a hundred eggs" shouldn't reach the order.
MAX_QUANTITY = 10
#: How many mapping names the model chooses between.
SHORTLIST = 20
#: Words too common to make two names alike.
FILLER = frozenset({"a", "and", "the", "of", "with", "in", "fresh", "british", "free", "range", "pack"})
#: Products checked live before one is chosen: the top of the list is nearly always in stock.
STOCK_CHECK = 5

#: ``{sku: in stock}`` for the skus it could ask about.
StockCheck = Callable[[list[str]], dict[str, bool]]


class NoOrder(RuntimeError):
    """There is no coming order that can still be changed."""


@dataclass(frozen=True, slots=True)
class Want:
    text: str
    #: ``None`` when no number was said: then something already on the order is left alone.
    quantity: int | None = None


@dataclass(frozen=True, slots=True)
class Match:
    key: str
    name: str


@dataclass(frozen=True, slots=True)
class Pick:
    want: Want
    match: Match
    product: Product
    #: Every product accepted for the ingredient, so "already on the order" counts any of them.
    skus: frozenset[str]


# --- which ingredient --------------------------------------------------------


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("oes"):
        return word[:-2]
    if len(word) > 4 and (word.endswith(("ches", "shes")) or word.endswith("es") and word[-3] in "sxz"):
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(text: str) -> tuple[str, ...]:
    return tuple(_stem(w) for w in normalize_name(text).split())


def _buyable_roots(session: Session, keys: Iterable[str], approved: dict[str, IngredientMapping]) -> list[str]:
    """``keys`` with aliases followed, deduplicated, keeping only what can be bought."""
    roots: list[str] = []
    for key in keys:
        root = service.resolve_alias(session, key, RETAILER)
        if root in approved and root not in roots:
            roots.append(root)
    return roots


def match(session: Session, text: str, *, completer: Completer | None = None) -> Match | list[str]:
    """The approved mapping ``text`` means; failing that, the names it could have
    meant (several hold every word), which is empty when it is nothing known."""
    wanted = _words(text)
    if not wanted:
        return []
    rows = session.scalars(
        select(IngredientMapping).where(
            IngredientMapping.retailer == RETAILER, IngredientMapping.status.in_(("approved", "alias"))
        )
    ).all()
    approved = {row.ingredient_key: row for row in rows if row.status == "approved"}
    words = {row.ingredient_key: _words(row.name) for row in rows}

    def found(key: str) -> Match:
        return Match(key=key, name=approved[key].name)

    exact = _buyable_roots(session, (k for k, w in words.items() if w == wanted), approved)
    if exact:
        return found(exact[0])

    by_use = sorted(rows, key=lambda row: -(row.line_count or 0))
    holding = _buyable_roots(
        session, (row.ingredient_key for row in by_use if set(wanted) <= set(words[row.ingredient_key])), approved
    )
    if len(holding) == 1:
        return found(holding[0])
    could_be = [approved[k].name for k in holding[:4]]
    if completer is None:  # nothing to decide between them with
        return could_be

    # The model's shortlist: names holding every word, then names sharing the most words
    # ("free-range eggs" is Egg), then near spellings.
    def shared(row: IngredientMapping) -> int:
        return len(set(wanted) & set(words[row.ingredient_key]) - FILLER)

    overlapping = sorted((row for row in by_use if shared(row)), key=lambda row: -shared(row))
    names = {" ".join(w): k for k, w in words.items()}
    close = difflib.get_close_matches(" ".join(wanted), list(names), n=SHORTLIST, cutoff=0.6)
    shortlist = _buyable_roots(
        session, [*holding, *(row.ingredient_key for row in overlapping), *(names[n] for n in close)], approved
    )[:SHORTLIST]
    if not shortlist:
        return could_be
    chosen = _choose(text, [approved[k] for k in shortlist], completer)
    return found(chosen) if chosen else could_be


def _choose(text: str, options: list[IngredientMapping], completer: Completer) -> str | None:
    """The option the model says ``text`` means, or ``None``."""
    labels = [str(i) for i in range(1, len(options) + 1)]
    listing = "\n".join(f"{label}. {option.name}" for label, option in zip(labels, options))
    system = ("Someone asked to add an item to their grocery order. Pick the product type they mean from the "
              "numbered list. A generic word takes its most ordinary form (\"milk\" is plain milk, not milk "
              "chocolate). Answer \"none\" unless one is plainly the same thing they asked for.")
    schema = {
        "type": "object",
        "properties": {"choice": {"type": "string", "enum": [*labels, "none"]}},
        "required": ["choice"],
        "additionalProperties": False,
    }
    try:
        answer = completer(system, f"They asked for: {text}\n\n{listing}", schema).get("choice")
    except Exception as exc:  # noqa: BLE001 - no model is the same as no confident answer
        log.warning("top-up: couldn't ask the model about %r: %s", text, exc)
        return None
    return options[labels.index(answer)].ingredient_key if answer in labels else None


def default_completer() -> Completer | None:
    try:
        return OpenAIJSONClient()
    except Exception as exc:  # noqa: BLE001 - matching still works on names alone
        log.info("top-up: no model for ambiguous names: %s", exc)
        return None


# --- which product -----------------------------------------------------------


def pick(session: Session, found: Match, user_id: int, *, stock: StockCheck | None = None,
         household: Iterable[int] = ()) -> tuple[Product | None, frozenset[str]]:
    """The product to buy for ``found``, and every product accepted for it.

    A standing pack choice wins; then whatever ``household`` (user ids whose orders
    count) has bought most often; then the reviewed rank.
    """
    mapping = session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == RETAILER, IngredientMapping.ingredient_key == found.key
        )
    )
    if mapping is None:
        return None, frozenset()
    rows = session.execute(
        select(IngredientMappingProduct, Product)
        .join(Product, (Product.sku == IngredientMappingProduct.sku) & (Product.retailer == RETAILER))
        .where(IngredientMappingProduct.mapping_id == mapping.id)
    ).all()
    skus = frozenset(link.sku for link, _ in rows)
    preferred = pack_preferences(session, user_id, retailer=RETAILER).get(found.key)
    tiers = {tier: i for i, tier in enumerate(MATCH_TYPE_PREFERENCE)}
    times = {sku: b.orders for sku, b in purchases.bought(session, household, skus).items()} if household else {}
    ordered = sorted(
        ((link, product) for link, product in rows if product.in_stock != 0),
        key=lambda pair: (pair[1].sku != preferred, -times.get(pair[1].sku, 0),
                          tiers.get(pair[0].match_type, len(tiers)), pair[0].rank),
    )
    products = [product for _, product in ordered]
    if stock is not None and products:
        try:
            live = stock([p.sku for p in products[:STOCK_CHECK]])
        except Exception as exc:  # noqa: BLE001 - the catalogue's word will do
            log.info("top-up: live stock check failed: %s", exc)
            live = {}
        products = [p for p in products if live.get(p.sku, True)]
    return (products[0] if products else None), skus


# --- which order -------------------------------------------------------------


def household_ids(accounts: Iterable[tuple[RetailerAccount, Any]]) -> list[int]:
    """Whose orders count as "what we buy": the owners of the accounts being shopped on."""
    return list(dict.fromkeys(account.user_id for account, _ in accounts))



def next_order(accounts: list[tuple[RetailerAccount, OcadoClient]]) -> tuple[RetailerAccount, OcadoClient, dict[str, Any]]:
    """The soonest coming delivery that can still be changed, on any of ``accounts``."""
    coming: list[tuple[str, bool, RetailerAccount, OcadoClient, dict]] = []
    for account, client in accounts:
        try:
            orders = client.orders(pending=True)
        except Exception as exc:  # noqa: BLE001 - a dead session is one account, not all of them
            log.info("top-up: orders for %s not read: %s", account.key, exc)
            continue
        for order in orders:
            if order.get("status") == "CANCELLED":
                continue
            start = (order.get("dates") or {}).get("deliveryStartDate") or ""
            coming.append((start, order.get("editStatus") == "EDITABLE", account, client, order))
    coming.sort(key=lambda row: row[0])
    editable = next((row for row in coming if row[1]), None)
    if editable is None:
        if coming:
            raise NoOrder("The coming Ocado orders are past their edit cutoff; add it on ocado.com once the next one is booked")
        raise NoOrder("There's no coming Ocado order to add to")
    _, _, account, client, order = editable
    return account, client, order


def order_quantities(client: OcadoClient, order_id: str) -> dict[str, int]:
    items = client.get_order(order_id).get("items") or []
    return {str(item["product"]): int(item["quantity"]) for item in items if item.get("quantity")}


# --- the whole request -------------------------------------------------------


def add(session: Session, user_id: int, wants: list[Want], accounts: list[tuple[RetailerAccount, OcadoClient]], *,
        completer: Completer | None = None, stock: StockCheck | None = None) -> dict[str, Any]:
    """Add ``wants`` to the next editable order and check it out.

    Returns what was added and what wasn't: each skipped item has a ``code`` (``unknown``,
    ``ambiguous``, ``sold_out``, ``on_order``) and a ``reason`` to show. Raises :class:`NoOrder`, or
    :class:`app.ocado.amend.OrderChangeError` when Ocado won't take the change.
    """
    picks: list[Pick] = []
    skipped: list[dict[str, str]] = []
    for want in wants:
        found = match(session, want.text, completer=completer)
        if isinstance(found, list):
            reason = (f"which one? {', '.join(found)}" if found else "not something HolaFresca knows how to buy")
            skipped.append({"text": want.text, "code": "ambiguous" if found else "unknown", "reason": reason})
            continue
        product, skus = pick(session, found, user_id, stock=stock, household=household_ids(accounts))
        if product is None:
            skipped.append({"text": want.text, "code": "sold_out", "reason": f"nothing for {found.name} is in stock"})
            continue
        picks.append(Pick(want, found, product, skus))
    if not picks:
        return {"order": None, "added": [], "skipped": skipped}

    account, client, order = next_order(accounts)
    order_id = str(order["orderId"])
    have = order_quantities(client, order_id)
    target: dict[str, int] = {}
    added: list[dict[str, Any]] = []
    for p in picks:
        on_order = sum(have.get(sku, 0) for sku in p.skus)
        if p.want.quantity is None and on_order:
            skipped.append({"text": p.want.text, "code": "on_order", "reason": f"already on the order (×{on_order})"})
            continue
        quantity = max(1, min(p.want.quantity or 1, MAX_QUANTITY))
        target[p.product.sku] = target.get(p.product.sku, have.get(p.product.sku, 0)) + quantity
        added.append({"text": p.want.text, "item": p.match.name, "sku": p.product.sku, "name": p.product.name,
                      "quantity": quantity, "price": p.product.price})
    if target:
        amend.change_order(client, order_id, quantities=target)
    dates = order.get("dates") or {}
    return {
        "order": {"order_id": order_id, "account": account.email, "delivery_start": dates.get("deliveryStartDate"),
                  "delivery_end": dates.get("deliveryEndDate")},
        "added": added,
        "skipped": skipped,
    }


def take_back(order_id: str, lines: dict[str, int], accounts: list[tuple[RetailerAccount, OcadoClient]]) -> dict[str, Any]:
    """Undo an :func:`add` (or a resolver plan): take ``lines`` (sku → how many were added;
    negative for how many were taken off) back off the order, or put them back.

    Subtracts rather than restoring the old quantities, so anything changed on the
    order since stays changed.
    """
    for account, client in accounts:
        try:
            order = amend.find_order(client, order_id)
        except Exception as exc:  # noqa: BLE001
            log.info("top-up: orders for %s not read: %s", account.key, exc)
            continue
        if order is None:
            continue
        have = order_quantities(client, order_id)
        target = {sku: max(0, min(have.get(sku, 0) - qty, MAX_QUANTITY * 2)) for sku, qty in lines.items()
                  if qty and (have.get(sku) or qty < 0)}
        target = {sku: q for sku, q in target.items() if q != have.get(sku, 0)}
        if target:
            amend.change_order(client, order_id, quantities=target)
        return {"order_id": order_id, "removed": {sku: have.get(sku, 0) - qty for sku, qty in target.items()}}
    raise NoOrder(f"Order {order_id} isn't a coming order any more")


def live_stock(client: OcadoClient) -> StockCheck:
    from app.ocado.availability import fetch_statuses

    def check(skus: list[str]) -> dict[str, bool]:
        return {sku: status.available for sku, status in fetch_statuses(skus, session=client.session).items()}

    return check

