"""Session and basket-push endpoints, for whichever shop has a cart.

Served under ``/api/cart/{retailer}``. This was ``/api/ocado/*`` and reached
into :mod:`app.ocado` directly; what differs per shop now lives behind
:mod:`app.cart.adapters`, so everything here is written once. The endpoints that
really are Ocado's alone - the auth-event log and delivery slots - stayed at
``/api/ocado`` rather than being given a retailer they do not have.

The retailer is a path segment rather than a query parameter because it decides
*which cart is written to*. A typo in a query parameter that silently defaults is
tolerable when it picks a catalogue to read; it is not when it picks whose
trolley the week's shopping lands in.

**Which account is never named by the caller.** It is looked up from the signed-in
user and the retailer in the path, through :func:`_owned_account`. The endpoints
used to take an ``account_id`` from the query string or the request body and hand
it straight to the adapter, which meant anyone who could reach the API could read,
fill and empty anybody else's trolley, and sign them out of their shop. There is
now no parameter to abuse: the registry answers "whose account is this" and the
question the client used to answer is not asked.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import schedule as sched
from app.api.deps import (
    get_current_user,
    get_planner_csv_path,
    get_session,
    get_session_factory,
)
from app.api.planner import (
    _load_planner_index,
    _planner_selection,
    _require_curated,
    _round_money,
    _stock_checked_at,
    candidate_skus,
)
from app.api.schedule import pack_shortfall_tolerance_pct
from app.api.schemas import (
    BasketIn,
    CartBasketOut,
    CartLoginIn,
    CartLoginOut,
    CartOtpIn,
    CheckoutItemOut,
    PushLineOut,
    PushPlanOut,
    PushResultOut,
    SwapOut,
)
from app.cart.adapters import CartAdapter, CartSnapshot, get_adapter
from app.cart.ledger import read_ledger, write_ledger
from app.cart.merge import CartLedger, PushLine, basket_targets
from app.db import retailer_accounts
from app.db.models import Product, RetailerAccount, User
from app.planner.basket import Basket, Selection, build_basket
from app.planner.index import PlanIndex

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cart/{retailer}", tags=["cart"])

#: Read cart -> merge -> write cart is not atomic, and the ledger written at the
#: end describes the cart as the *last* writer left it. One lock per shop rather
#: than one overall: two retailers' pushes touch different carts and different
#: ledger rows, so serialising them against each other would only make the
#: second person wait for no reason.
_PUSH_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _push_lock(retailer: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _PUSH_LOCKS.setdefault(retailer, threading.Lock())


def get_cart_adapter(
    retailer: str = PathParam(description="which shop's cart"),
) -> CartAdapter:
    """The adapter for the shop in the path, or 404.

    A retailer that exists but has no cart integration is a 404 here rather than
    a 500 later: "this shop is a shopping list you take to the shop yourself" is
    a fact about the shop, and the UI already knows it from
    :attr:`app.retailers.Retailer.shoppable`.
    """
    try:
        return get_adapter(retailer)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"No cart integration for retailer: {retailer}"
        ) from None


def _login_out(status, account: RetailerAccount | None = None) -> CartLoginOut:
    return CartLoginOut(
        status=status.status,
        stage=status.stage or "idle",
        email=account.email if account is not None else None,
    )


def _account_row(
    session: Session, user: User, adapter: CartAdapter
) -> RetailerAccount | None:
    """The caller's account at this shop, or ``None`` if they have not connected."""
    return retailer_accounts.find(session, user.id, adapter.retailer)


def _owned_account(
    session: Session, user: User, adapter: CartAdapter
) -> RetailerAccount:
    """The caller's account at this shop, or 404.

    Everything that touches a trolley goes through here. There is no fallback to
    "the only account configured": a shop nobody has connected has no basket to
    read, and inventing one would mean serving somebody else's.
    """
    account = _account_row(session, user, adapter)
    if account is None:
        raise HTTPException(
            status_code=404,
            detail=f"No {adapter.retailer} account is connected for this user",
        )
    return account


# --- accounts and sessions ---------------------------------------------------


@router.get("/status", response_model=CartLoginOut)
def status(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    adapter: CartAdapter = Depends(get_cart_adapter),
) -> CartLoginOut:
    """Where the caller stands with this shop.

    Not connecting one is an ordinary answer rather than a 404: the page asks
    this before anybody has signed in anywhere, and "logged out" is exactly what
    it needs to hear in order to offer the form.
    """
    account = _account_row(session, user, adapter)
    if account is None:
        return CartLoginOut(status="logged_out")
    return _login_out(adapter.status(account.key), account)


@router.post("/login", response_model=CartLoginOut)
def login(
    body: CartLoginIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    adapter: CartAdapter = Depends(get_cart_adapter),
) -> CartLoginOut:
    """Sign in to this shop as the caller, and connect the account if it is new.

    The credentials live for this request. They are handed to the adapter, which
    passes them to the ladder's login rung, and nothing writes them anywhere: the
    registry has no password column, and what survives the request is the session
    the login produced.
    """
    email = body.email.strip()
    password = body.password.get_secret_value()
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    account = _account_row(session, user, adapter) or retailer_accounts.connect(
        session, user.id, adapter.retailer, email=email
    )
    try:
        auth = adapter.ensure_authenticated(
            account.key, email=email, password=password
        )
    except Exception as exc:  # noqa: BLE001 - login failures surface as bad gateway
        raise HTTPException(
            status_code=502, detail=f"{adapter.retailer} login failed: {exc}"
        ) from exc
    finally:
        del password
    retailer_accounts.record_status(
        session, account, auth.status, email=email, after_login=True
    )
    return _login_out(auth, account)


@router.post("/session/refresh", response_model=CartLoginOut)
def refresh_session(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    adapter: CartAdapter = Depends(get_cart_adapter),
) -> CartLoginOut:
    """Become ready if that is possible without asking the user anything.

    Safe to call automatically on page load: it stops before the password step,
    which is what would email a code to someone who only opened the page.
    """
    account = _account_row(session, user, adapter)
    if account is None:
        return CartLoginOut(status="logged_out")
    try:
        auth = adapter.ensure_authenticated(account.key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"{adapter.retailer} session refresh failed: {exc}"
        ) from exc
    retailer_accounts.record_status(session, account, auth.status)
    return _login_out(auth, account)


@router.post("/otp", response_model=CartLoginOut)
def otp(
    body: CartOtpIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    adapter: CartAdapter = Depends(get_cart_adapter),
) -> CartLoginOut:
    account = _owned_account(session, user, adapter)
    try:
        auth = adapter.submit_otp(body.code, account.key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"{adapter.retailer} OTP failed: {exc}"
        ) from exc
    retailer_accounts.record_status(session, account, auth.status, after_login=True)
    return _login_out(auth, account)


@router.post("/logout", response_model=CartLoginOut)
def logout(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    adapter: CartAdapter = Depends(get_cart_adapter),
) -> CartLoginOut:
    """Disconnect the current user's account at the retailer in the path."""
    account = _owned_account(session, user, adapter)
    try:
        auth = adapter.logout(account.key)
    except Exception as exc:  # noqa: BLE001 - filesystem/session failures surface cleanly
        raise HTTPException(
            status_code=502, detail=f"{adapter.retailer} logout failed: {exc}"
        ) from exc
    retailer_accounts.disconnect(session, account)
    return _login_out(auth, account)


# --- shared basket machinery -------------------------------------------------


def _rebuild(
    factory: sessionmaker[Session],
    retailer: str,
    recipe_ids: list[int],
    csv_path: Path | None,
    selections: list[Selection],
    overrides: dict[str, str] | None = None,
    snap_overrides: dict[str, bool] | None = None,
    shortfall_tolerance_pct: float = 10.0,
) -> tuple[PlanIndex, Basket]:
    """Price the week at the shop it is about to be pushed to.

    Deliberately the path's retailer rather than the user's active one: pricing a
    Sainsbury's basket and pushing its SKUs into an Ocado cart would be nonsense,
    and the SKUs would not exist there anyway.
    """
    index: PlanIndex = _load_planner_index(factory, recipe_ids, csv_path, retailer)
    return index, build_basket(
        index,
        selections,
        pack_overrides=overrides,
        snap_overrides=snap_overrides,
        pack_shortfall_tolerance_pct=shortfall_tolerance_pct,
    )


def _refresh_basket_stock(
    adapter: CartAdapter,
    factory: sessionmaker[Session],
    index: PlanIndex,
    basket: Basket,
    account_id: str,
) -> None:
    """Best-effort live stock read. A failure here must not block the push.

    The shop being unreachable is a reason to shop from a stale catalogue, not a
    reason to refuse to shop - the push that follows will find out the hard way
    and recover from that instead.
    """
    try:
        adapter.refresh_stock(factory, candidate_skus(index, basket), account_id=account_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "%s stock refresh failed, pushing from the cached catalogue: %s",
            adapter.retailer,
            exc,
        )


def _swaps(basket: Basket) -> list[SwapOut]:
    return [
        SwapOut(
            ingredient=line.name,
            ingredient_key=line.key,
            from_products=list(line.substitution.displaced),
            to_products=[choice.pack.product_name for choice in line.cover.choices],
            cost_delta=_round_money(line.substitution.cost_delta),
            tier_changed=line.substitution.tier_changed,
        )
        for line in basket.substituted_lines
        if line.cover is not None and line.substitution is not None
    ]


def _out_lines(
    factory: sessionmaker[Session], retailer: str, lines: list[PushLine]
) -> list[PushLineOut]:
    """Push lines with a product name filled in wherever HF has none.

    Which is exactly your own items: they never came out of a basket cover, so
    nothing upstream knows what they are called. The catalogue usually does, and
    "2 x Cathedral City Mature" beats a UUID in a report whose whole job is to
    show you what was left alone.
    """
    missing = sorted({line.sku for line in lines if not line.name})
    names: dict[str, str] = {}
    if missing:
        with factory() as session:
            names = dict(
                session.execute(
                    select(Product.sku, Product.name)
                    .where(Product.sku.in_(missing))
                    .where(Product.retailer == retailer)
                ).all()
            )
    # asdict, not vars: these dataclasses use slots and so have no __dict__.
    return [
        PushLineOut(**{**asdict(line), "name": line.name or names.get(line.sku)})
        for line in lines
    ]


def _checkout_items(
    factory: sessionmaker[Session],
    retailer: str,
    basket: Basket,
    ledger: CartLedger,
    snapshot: CartSnapshot,
    *,
    owned_item_keys: set[str],
) -> list[CheckoutItemOut]:
    """Normalize the target, ledger and live cart into display-ready rows."""
    targets, target_names, _, _ = basket_targets(basket, owned_item_keys=owned_item_keys)
    ledger_quantities = ledger.quantities
    cart = snapshot.quantities
    live_costs = snapshot.costs
    # A historical claim that is wanted by neither this week nor the live cart
    # has no action left to take. Keeping it produces an ever-growing checkout
    # table of already-completed removals, each misleadingly priced at £0.
    skus = set(targets) | {
        sku for sku in ledger_quantities if cart.get(sku, 0) > 0
    }
    if not skus:
        return []

    planned: dict[str, dict] = {}
    for line in basket.lines:
        if line.key in owned_item_keys or line.external or line.cover is None:
            continue
        for choice in line.cover.choices:
            planned.setdefault(
                choice.pack.sku,
                {
                    "name": choice.pack.product_name,
                    "url": choice.pack.url,
                    "pack_size_raw": choice.pack.pack_size_raw,
                    "price": choice.pack.price,
                },
            )

    with factory() as session:
        products = {
            product.sku: product
            for product in session.execute(
                select(Product)
                .where(Product.retailer == retailer)
                .where(Product.sku.in_(skus))
            ).scalars()
        }

    ledger_lines = {line.sku: line for line in ledger.lines}
    rows: list[CheckoutItemOut] = []
    for sku in skus:
        desired = targets.get(sku, 0)
        synced = ledger_quantities.get(sku, 0)
        current = cart.get(sku, 0)
        if not ledger.synced:
            row_status = "not_synced"
        elif desired != synced:
            row_status = "changed"
        elif current < synced:
            row_status = "deleted"
        elif current > synced:
            row_status = "extra"
        else:
            row_status = "synced"

        product = products.get(sku)
        plan = planned.get(sku, {})
        ledger_line = ledger_lines.get(sku)
        name = (
            plan.get("name")
            or (product.name if product else None)
            or (ledger_line.name if ledger_line else None)
            or target_names.get(sku)
            or sku
        )
        live_cost = live_costs.get(sku)
        if live_cost is not None:
            cost = live_cost
            cost_source = "live"
        else:
            price = plan.get("price")
            if price is None and product is not None:
                price = product.price
            quantity = desired if desired > 0 else current
            cost = price * quantity if price is not None else None
            cost_source = "planned"

        rows.append(
            CheckoutItemOut(
                sku=sku,
                name=name,
                url=plan.get("url") or (product.url if product else None),
                pack_size_raw=plan.get("pack_size_raw")
                or (product.pack_size_raw if product else None),
                desired_quantity=desired,
                synced_quantity=synced,
                cart_quantity=current,
                cost=_round_money(cost) if cost is not None else None,
                cost_source=cost_source,
                status=row_status,
            )
        )

    return sorted(rows, key=lambda row: (row.desired_quantity == 0, row.name.casefold(), row.sku))


def _refuse_past_week(week_start: str | None) -> None:
    """Refuse to shop for a week that has already happened.

    Old baskets are worth opening — they are the record of what was bought — but
    pushing one would fill the cart with a shop that is already eaten, on top of
    whatever is in there for the week you are actually planning. An unparseable
    or absent week is let through: it is only a label on the ledger, and the push
    predates it.
    """
    if not week_start:
        return
    try:
        parsed = sched.parse_date(week_start)
    except ValueError:
        return
    if parsed < sched.upcoming_week_start():
        raise HTTPException(
            status_code=409,
            detail=f"The week of {week_start} has been and gone, and cannot be shopped for",
        )


# --- plan and push -----------------------------------------------------------


@router.post("/basket/plan", response_model=PushPlanOut)
def plan(
    body: BasketIn,
    adapter: CartAdapter = Depends(get_cart_adapter),
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
) -> PushPlanOut:
    """What a push would change, without changing it.

    Deliberately cheaper than the push: no stock check, so it covers from the
    cached catalogue and can name a pack the push then substitutes away from.
    The question it answers - what of yours gets touched - does not depend on
    which pack of sesame seeds wins.
    """
    account_id = _owned_account(session, user, adapter).key
    recipe_ids = list(dict.fromkeys(s.recipe_id for s in body.selections))
    _require_curated(session, recipe_ids)
    selections = [_planner_selection(selection) for selection in body.selections]
    tolerance = pack_shortfall_tolerance_pct(session, user.id)
    _, basket = _rebuild(
        factory, adapter.retailer, recipe_ids, csv_path, selections,
        body.pack_overrides, body.snap_overrides, tolerance,
    )
    ledger = read_ledger(factory, account_id=account_id, retailer=adapter.retailer)
    owned_item_keys = set(body.owned_item_keys)
    try:
        snapshot = adapter.cart(account_id)
        result = adapter.plan_push(
            basket,
            ledger=ledger,
            owned_item_keys=owned_item_keys,
            account_id=account_id,
            snapshot=snapshot,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"{adapter.retailer} basket plan failed: {exc}"
        ) from exc
    return PushPlanOut(
        added=_out_lines(factory, adapter.retailer, result.added),
        removed=_out_lines(factory, adapter.retailer, result.removed),
        restored=_out_lines(factory, adapter.retailer, result.restored),
        yours=_out_lines(factory, adapter.retailer, result.yours),
        unmapped=result.unmapped,
        deltas=result.deltas,
        synced=ledger.synced,
        synced_at=ledger.synced_at,
        synced_week_start=ledger.week_start,
        checkout_items=_checkout_items(
            factory,
            adapter.retailer,
            basket,
            ledger,
            snapshot,
            owned_item_keys=owned_item_keys,
        ),
    )


@router.post("/basket/push", response_model=PushResultOut)
def push(
    body: BasketIn,
    adapter: CartAdapter = Depends(get_cart_adapter),
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
) -> PushResultOut:
    _refuse_past_week(body.week_start)
    account_id = _owned_account(session, user, adapter).key
    recipe_ids = list(dict.fromkeys(s.recipe_id for s in body.selections))
    _require_curated(session, recipe_ids)
    selections = [_planner_selection(selection) for selection in body.selections]
    tolerance = pack_shortfall_tolerance_pct(session, user.id)

    def rebuild() -> tuple[PlanIndex, Basket]:
        return _rebuild(
            factory, adapter.retailer, recipe_ids, csv_path, selections,
            body.pack_overrides, body.snap_overrides, tolerance,
        )

    index, basket = rebuild()

    # Check the shelves before filling the trolley. The write-back moves the
    # database file, which is what makes the rebuild below see the new stock and
    # cover around anything that has sold out since the last scrape.
    _refresh_basket_stock(adapter, factory, index, basket, account_id)
    index, basket = rebuild()

    def recover(skus: list[str]) -> Basket | None:
        """Believe the cart over the catalogue, then cover the week again."""
        if not adapter.mark_unavailable(factory, skus):
            return None
        return rebuild()[1]

    with _push_lock(adapter.retailer):
        try:
            result = adapter.push_basket(
                basket,
                ledger=read_ledger(
                    factory, account_id=account_id, retailer=adapter.retailer
                ),
                owned_item_keys=set(body.owned_item_keys),
                account_id=account_id,
                recover=recover,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502, detail=f"{adapter.retailer} basket push failed: {exc}"
            ) from exc
        # Inside the lock: the ledger describes the cart the push just left, and
        # a second push reading it before this write would merge against a stale
        # claim and buy the week twice.
        write_ledger(
            factory,
            result.ledger,
            account_id=account_id,
            retailer=adapter.retailer,
            week_start=body.week_start,
        )

    pushed = result.basket or basket
    return PushResultOut(
        applied=_out_lines(factory, adapter.retailer, result.applied),
        dropped=_out_lines(factory, adapter.retailer, result.dropped),
        unmapped=result.unmapped,
        deltas=result.deltas,
        yours=_out_lines(factory, adapter.retailer, result.yours),
        restored=_out_lines(factory, adapter.retailer, result.restored),
        removed=_out_lines(factory, adapter.retailer, result.removed),
        swaps=_swaps(pushed),
        sold_out=pushed.sold_out,
        stock_checked_at=_stock_checked_at(pushed),
    )


@router.get("/basket", response_model=CartBasketOut)
def basket(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
    adapter: CartAdapter = Depends(get_cart_adapter),
) -> CartBasketOut:
    snapshot = adapter.cart(_owned_account(session, user, adapter).key)
    raw = snapshot.raw if isinstance(snapshot.raw, dict) else {"items": list(snapshot.raw or [])}
    return CartBasketOut(raw=raw)
