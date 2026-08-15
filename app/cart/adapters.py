"""One interface over the shops a basket can actually be pushed to.

The API used to be ``/api/ocado/*`` and reach straight into :mod:`app.ocado`.
That was honest while Ocado was the only shop with a cart; it stopped being so
the moment Sainsbury's grew a login and a trolley push, and copying the router
would have been the "search for hardcoded ocado" the repository's own rules warn
against.

So the differences are named here, once, and everything above this line asks for
a capability rather than a shop. What actually differs is smaller than it looks:

* **accounts.** Ocado has several, each with its own cookie jar and ledger.
  Sainsbury's has one. The interface is always a list, so the caller does not
  branch; :attr:`CartAdapter.multi_account` exists only so the UI can decide
  whether an account picker is worth showing.
* **the auth ladder.** Ocado's drives a browser and can stop at an emailed code
  on any given day. Sainsbury's is HTTP and stops at a code roughly once ever.
  Both reduce to the same three states.
* **the cart payload.** Ocado nests lines three deep and states a live line
  price; Sainsbury's returns a flat list. :class:`CartSnapshot` is the shape the
  API renders from, so neither payload leaks upwards.
* **how a change is expressed.** Deltas at Ocado, absolute quantities at
  Sainsbury's - but that is settled inside each ``push_basket`` and never
  surfaces here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from sqlalchemy.orm import Session, sessionmaker

from app.cart.merge import CartLedger, PushPlan, PushResult
from app.planner.basket import Basket


@dataclass(frozen=True, slots=True)
class AccountInfo:
    id: str
    label: str
    email: str | None = None
    status: str = "logged_out"


@dataclass(frozen=True, slots=True)
class CartItem:
    """One line of the live cart, in the terms the API renders.

    ``cost`` is what the shop says this line costs *now*, where it says so at
    all. It is worth carrying separately from the planned price: a promotion that
    started since the plan was built is real money, and showing the planned
    figure next to a cart that will charge something else is the kind of small
    lie that makes a checkout screen untrustworthy.
    """

    sku: str
    quantity: int
    cost: float | None = None


@dataclass(frozen=True, slots=True)
class CartSnapshot:
    items: tuple[CartItem, ...] = ()
    #: The retailer's own payload, for the adapter that wants to hand it back to
    #: its own merge without paying for a second read. Never inspected above.
    raw: Any = None

    @property
    def quantities(self) -> dict[str, int]:
        return {item.sku: item.quantity for item in self.items if item.quantity > 0}

    @property
    def costs(self) -> dict[str, float]:
        return {item.sku: item.cost for item in self.items if item.cost is not None}


@dataclass(frozen=True, slots=True)
class AuthStatus:
    """Where the ladder stopped, in the three states every shop shares."""

    account_id: str
    #: ``logged_out`` | ``awaiting_otp`` | ``ready``
    status: str
    #: Free-text progress for a UI that shows what the ladder is doing. Ocado
    #: reports a rung; Sainsbury's climbs too fast for it to be worth watching.
    stage: str | None = None


class CartAdapter(ABC):
    """What the API needs of a shop that can be shopped at."""

    retailer: str
    #: Whether this shop has more than one account configured. Presentation only.
    multi_account: bool = False

    # --- accounts ---

    @abstractmethod
    def accounts(self) -> list[AccountInfo]:
        ...

    @property
    def default_account_id(self) -> str:
        return self.accounts()[0].id

    def resolve_account(self, account_id: str | None) -> str:
        """Normalise an account id, raising ``KeyError`` for one that is not ours."""
        if account_id is None:
            return self.default_account_id
        if account_id not in {account.id for account in self.accounts()}:
            raise KeyError(account_id)
        return account_id

    # --- authentication ---

    @abstractmethod
    def status(self, account_id: str | None = None) -> AuthStatus:
        ...

    @abstractmethod
    def ensure_authenticated(
        self, account_id: str | None = None, *, allow_login: bool = True
    ) -> AuthStatus:
        """Climb as far as possible.

        ``allow_login=False`` stops before anything that would email somebody a
        code, which is what makes this safe to call on page load.
        """

    @abstractmethod
    def submit_otp(self, code: str, account_id: str | None = None) -> AuthStatus:
        ...

    # --- the cart ---

    @abstractmethod
    def cart(self, account_id: str | None = None) -> CartSnapshot:
        ...

    @abstractmethod
    def plan_push(
        self,
        basket: Basket,
        *,
        ledger: CartLedger,
        owned_item_keys: set[str],
        account_id: str | None = None,
        snapshot: CartSnapshot | None = None,
    ) -> PushPlan:
        ...

    @abstractmethod
    def push_basket(
        self,
        basket: Basket,
        *,
        ledger: CartLedger,
        owned_item_keys: set[str],
        account_id: str | None = None,
        recover: Callable[[list[str]], Basket | None] | None = None,
    ) -> PushResult:
        ...

    # --- the catalogue behind it ---

    def refresh_stock(
        self,
        factory: sessionmaker[Session],
        skus: Sequence[str],
        *,
        account_id: str | None = None,
    ) -> None:
        """Best-effort live stock read before a push.

        Generic by default: :mod:`app.catalogue` already dispatches to whichever
        adapter the retailer registers. Ocado overrides it only to reuse the
        signed-in session it is already holding.
        """
        from app import catalogue

        catalogue.refresh_stock(factory, skus, retailer=self.retailer)

    def mark_unavailable(self, factory: sessionmaker[Session], skus: list[str]) -> int:
        from app import catalogue

        return catalogue.mark_unavailable(factory, skus, retailer=self.retailer)


class OcadoAdapter(CartAdapter):
    retailer = "ocado"
    multi_account = True

    def accounts(self) -> list[AccountInfo]:
        from app.ocado.session import list_account_runtimes

        return [
            AccountInfo(
                id=runtime.account.id,
                label=runtime.account.label,
                email=runtime.account.email,
                status=str(runtime.auth.state),
            )
            for runtime in list_account_runtimes()
        ]

    def _runtime(self, account_id: str | None):
        from app.ocado.session import get_account_runtime

        return get_account_runtime(account_id)

    def _client(self, account_id: str | None):
        from app.ocado.client import OcadoClient

        return OcadoClient(self._runtime(account_id).session)

    def status(self, account_id: str | None = None) -> AuthStatus:
        runtime = self._runtime(account_id)
        return AuthStatus(
            account_id=runtime.account.id,
            status=str(runtime.auth.state),
            stage=str(runtime.auth.stage),
        )

    def ensure_authenticated(
        self, account_id: str | None = None, *, allow_login: bool = True
    ) -> AuthStatus:
        runtime = self._runtime(account_id)
        state = runtime.auth.ensure_authenticated(runtime.session, allow_login=allow_login)
        return AuthStatus(
            account_id=runtime.account.id, status=str(state), stage=str(runtime.auth.stage)
        )

    def submit_otp(self, code: str, account_id: str | None = None) -> AuthStatus:
        runtime = self._runtime(account_id)
        state = runtime.auth.submit_otp(code)
        return AuthStatus(
            account_id=runtime.account.id, status=str(state), stage=str(runtime.auth.stage)
        )

    def cart(self, account_id: str | None = None) -> CartSnapshot:
        from app.ocado.cart_payload import snapshot

        return snapshot(self._client(account_id).cart_view())

    def plan_push(
        self,
        basket: Basket,
        *,
        ledger: CartLedger,
        owned_item_keys: set[str],
        account_id: str | None = None,
        snapshot: CartSnapshot | None = None,
    ) -> PushPlan:
        from app.ocado.sync import plan_push

        client = self._client(account_id)
        return plan_push(
            client,
            basket,
            ledger=ledger,
            owned_item_keys=owned_item_keys,
            cart_payload=snapshot.raw if snapshot is not None else None,
        )

    def push_basket(
        self,
        basket: Basket,
        *,
        ledger: CartLedger,
        owned_item_keys: set[str],
        account_id: str | None = None,
        recover: Callable[[list[str]], Basket | None] | None = None,
    ) -> PushResult:
        from app.ocado.sync import push_basket

        return push_basket(
            self._client(account_id),
            basket,
            ledger=ledger,
            owned_item_keys=owned_item_keys,
            recover=recover,
        )

    def refresh_stock(
        self,
        factory: sessionmaker[Session],
        skus: Sequence[str],
        *,
        account_id: str | None = None,
    ) -> None:
        from app.ocado.availability import refresh_stock

        # Uses this account's signed-in session rather than the shared one, so a
        # refresh cannot be answered for a different login than the push.
        refresh_stock(factory, skus, session=self._runtime(account_id).session)


class SainsburysAdapter(CartAdapter):
    retailer = "sainsburys"
    multi_account = False

    #: The single account's id. Sainsbury's has one login, but the ledger and the
    #: API are both keyed by account anyway, so it needs a name.
    ACCOUNT_ID = "default"

    def accounts(self) -> list[AccountInfo]:
        from app import config
        from app.sainsburys.session import get_shared_session

        session = get_shared_session()
        return [
            AccountInfo(
                id=self.ACCOUNT_ID,
                label="Sainsbury's",
                email=config.SAINSBURYS_EMAIL,
                status=str(session.state),
            )
        ]

    def _session(self):
        from app.sainsburys.session import get_shared_session

        return get_shared_session()

    def _client(self):
        from app.sainsburys.client import SainsburysClient

        return SainsburysClient(self._session())

    def status(self, account_id: str | None = None) -> AuthStatus:
        return AuthStatus(account_id=self.ACCOUNT_ID, status=str(self._session().state))

    def ensure_authenticated(
        self, account_id: str | None = None, *, allow_login: bool = True
    ) -> AuthStatus:
        session = self._session()
        if not allow_login:
            return AuthStatus(
                account_id=self.ACCOUNT_ID, status=str(session.refresh_quietly())
            )
        return AuthStatus(
            account_id=self.ACCOUNT_ID, status=str(session.ensure_authenticated())
        )

    def submit_otp(self, code: str, account_id: str | None = None) -> AuthStatus:
        return AuthStatus(
            account_id=self.ACCOUNT_ID, status=str(self._session().submit_otp(code))
        )

    def cart(self, account_id: str | None = None) -> CartSnapshot:
        client = self._client()
        payload = client.basket()
        from app.sainsburys.client import basket_lines

        return CartSnapshot(
            items=tuple(
                CartItem(sku=line.sku, quantity=line.quantity, cost=line.price)
                for line in basket_lines(payload)
            ),
            raw=payload,
        )

    def plan_push(
        self,
        basket: Basket,
        *,
        ledger: CartLedger,
        owned_item_keys: set[str],
        account_id: str | None = None,
        snapshot: CartSnapshot | None = None,
    ) -> PushPlan:
        from app.sainsburys.sync import plan_push

        return plan_push(
            self._client(), basket, ledger=ledger, owned_item_keys=owned_item_keys
        )

    def push_basket(
        self,
        basket: Basket,
        *,
        ledger: CartLedger,
        owned_item_keys: set[str],
        account_id: str | None = None,
        recover: Callable[[list[str]], Basket | None] | None = None,
    ) -> PushResult:
        from app.sainsburys.sync import push_basket

        return push_basket(
            self._client(),
            basket,
            ledger=ledger,
            owned_item_keys=owned_item_keys,
            recover=recover,
        )


_ADAPTERS: dict[str, CartAdapter] = {
    adapter.retailer: adapter for adapter in (OcadoAdapter(), SainsburysAdapter())
}


def get_adapter(retailer: str | None) -> CartAdapter:
    """The adapter for this shop.

    Raises ``KeyError`` for a retailer with no cart integration, which is the
    same answer :attr:`app.retailers.Retailer.shoppable` gives — a shop that can
    be priced but not pushed to is a shopping list, not a broken endpoint.
    """
    from app.retailers import DEFAULT_RETAILER

    return _ADAPTERS[retailer or DEFAULT_RETAILER]


def is_shoppable(retailer: str | None) -> bool:
    return (retailer or "") in _ADAPTERS
