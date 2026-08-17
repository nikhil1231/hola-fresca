"""The shared cart routes, exercised for every shop that has a cart.

These used to be Ocado's alone at ``/api/ocado/*``. The point of most of what
follows is that they are now written once: the same request against
``/api/cart/ocado`` and ``/api/cart/sainsburys`` should behave the same way, and
a shop with no cart integration should be told apart from a typo.

The other half of what is asserted here is *whose* trolley a request reaches.
No endpoint takes an account id any more, so every one of these has to resolve
the caller's own account — and a caller with none connected must be refused
rather than served somebody else's.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import main
from app import schedule as sched
from app.api import cart as cart_api
from app.api.cart import get_cart_adapter
from app.api.deps import get_current_user, get_session_factory
from app.cart.adapters import AuthStatus, CartAdapter, CartItem, CartSnapshot
from app.db.models import RetailerAccount, User
from app.cart.merge import CartLedger, PushPlan
from app.planner.basket import Basket

SHOPS = ["ocado", "sainsburys"]


class FakeAdapter(CartAdapter):
    """A shop that answers everything without a network or a login."""

    def __init__(self, retailer="ocado"):
        self.retailer = retailer
        self.pushed: list[str] = []
        self.logged_out: list[str] = []
        #: Every account key the router asked this adapter about, so a test can
        #: assert which account a request actually reached.
        self.seen: list[str] = []

    def status(self, account_id):
        self.seen.append(account_id)
        return AuthStatus(account_id=account_id, status="logged_out", stage="idle")

    def ensure_authenticated(self, account_id, *, email=None, password=None):
        self.seen.append(account_id)
        return AuthStatus(
            account_id=account_id,
            status="ready" if email and password else "needs_password",
        )

    def submit_otp(self, code, account_id):
        self.seen.append(account_id)
        return AuthStatus(account_id=account_id, status="ready")

    def logout(self, account_id):
        self.logged_out.append(account_id)
        return AuthStatus(account_id=account_id, status="logged_out", stage="idle")

    def cart(self, account_id):
        self.seen.append(account_id)
        return CartSnapshot(
            items=(CartItem(sku="sku-a", quantity=2, cost=3.5),), raw={"items": ["raw"]}
        )

    def plan_push(self, basket, **kwargs):
        return PushPlan()

    def push_basket(self, basket, **kwargs):
        self.pushed.append(self.retailer)
        from app.cart.merge import PushResult

        return PushResult(ledger=CartLedger())

    def clear_personal(self, account_id, *, ledger):
        self.seen.append(account_id)
        return {}


def connect(retailer, *, user_id=None, key=None, email="shopper@example.com"):
    """Give a user a connected account at ``retailer``, with a key of our choosing.

    Adopts the row if there already is one, because ``init_db`` seeds the
    bootstrap user an Ocado account from the legacy config — and one account per
    user per shop is a constraint, so a second insert would fail rather than give
    this test a second account to confuse itself with.
    """
    with get_session_factory()() as session:
        if user_id is None:
            user_id = session.scalar(select(User.id).order_by(User.id).limit(1))
        account = session.scalar(
            select(RetailerAccount).where(
                RetailerAccount.user_id == user_id,
                RetailerAccount.retailer == retailer,
            )
        )
        if account is None:
            account = RetailerAccount(user_id=user_id, retailer=retailer)
            session.add(account)
        account.key = key or f"{retailer}-key"
        account.email = email
        session.commit()
        return account.key


@pytest.fixture
def client():
    adapters: dict[str, FakeAdapter] = {}

    def resolve(retailer: str = "ocado"):
        if retailer not in SHOPS:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"No cart integration for: {retailer}")
        return adapters.setdefault(retailer, FakeAdapter(retailer))

    main.app.dependency_overrides[get_cart_adapter] = resolve
    with TestClient(main.app) as test_client:
        test_client.adapters = adapters
        yield test_client
    main.app.dependency_overrides.clear()


@pytest.fixture
def connected(client):
    """A client whose user has an account connected at both shops."""
    keys = {retailer: connect(retailer, key=f"{retailer}-mine") for retailer in SHOPS}
    client.keys = keys
    return client


@pytest.mark.parametrize("retailer", SHOPS)
def test_status_reports_the_ladder_state(connected, retailer):
    response = connected.get(f"/api/cart/{retailer}/status")

    assert response.status_code == 200
    assert response.json()["status"] in {
        "logged_out", "needs_password", "awaiting_otp", "ready"
    }


@pytest.mark.parametrize("retailer", SHOPS)
def test_status_also_reports_the_stage(connected, retailer):
    """What the page polls for while a login request is still blocked."""
    assert connected.get(f"/api/cart/{retailer}/status").json()["stage"] == "idle"


@pytest.mark.parametrize("retailer", SHOPS)
def test_status_is_logged_out_rather_than_404_before_connecting(client, retailer):
    """The page asks this in order to decide whether to offer the form."""
    body = client.get(f"/api/cart/{retailer}/status").json()

    assert body["status"] == "logged_out"
    assert body["email"] is None


@pytest.mark.parametrize("retailer", SHOPS)
def test_status_hands_back_the_address_to_fill_the_form_in_with(connected, retailer):
    assert connected.get(f"/api/cart/{retailer}/status").json()["email"] == (
        "shopper@example.com"
    )


@pytest.mark.parametrize("retailer", SHOPS)
def test_no_response_carries_the_account_key(connected, retailer):
    """It names a cookie jar on disk, and the client has no business with it."""
    body = connected.get(f"/api/cart/{retailer}/status").json()

    assert "account_id" not in body
    assert connected.keys[retailer] not in connected.get(
        f"/api/cart/{retailer}/status"
    ).text


@pytest.mark.parametrize("retailer", SHOPS)
def test_the_basket_comes_back_for_either_shop(connected, retailer):
    response = connected.get(f"/api/cart/{retailer}/basket")

    assert response.status_code == 200
    assert response.json()["raw"] == {"items": ["raw"]}


@pytest.mark.parametrize("retailer", SHOPS)
def test_otp_rejects_an_empty_code(connected, retailer):
    assert connected.post(f"/api/cart/{retailer}/otp", json={"code": ""}).status_code == 422


@pytest.mark.parametrize("retailer", SHOPS)
def test_a_quiet_refresh_never_climbs_to_the_password(connected, retailer):
    """The rung that would email somebody a code, for opening a page."""
    body = connected.post(f"/api/cart/{retailer}/session/refresh").json()

    assert body["status"] == "needs_password"
    login = connected.post(
        f"/api/cart/{retailer}/login",
        json={"email": "a@example.com", "password": "secret"},
    )
    assert login.json()["status"] == "ready"


@pytest.mark.parametrize("retailer", SHOPS)
def test_logout_forgets_the_callers_session(connected, retailer):
    response = connected.post(f"/api/cart/{retailer}/logout")

    assert response.status_code == 200
    assert response.json()["status"] == "logged_out"
    assert connected.adapters[retailer].logged_out == [connected.keys[retailer]]


@pytest.mark.parametrize(
    "method, path, payload",
    [
        ("get", "basket", None),
        ("post", "basket/plan", {"selections": []}),
        ("post", "basket/push", {"selections": []}),
        ("post", "otp", {"code": "123456"}),
        ("post", "logout", None),
    ],
)
def test_touching_a_trolley_without_connecting_one_is_refused(client, method, path, payload):
    """The alternative would be serving whichever account happened to exist."""
    call = getattr(client, method)
    response = call(f"/api/cart/sainsburys/{path}", **({"json": payload} if payload else {}))

    assert response.status_code == 404
    assert "connected" in response.json()["detail"]


def test_a_request_reaches_the_callers_own_account_and_no_other(client):
    """The regression this whole change exists for.

    Two people, each with an Ocado account. Whichever one is signed in, the
    adapter must be asked about their key — and there is no longer any parameter
    with which to ask for the other's.
    """
    mine = connect("ocado", key="mine")
    with get_session_factory()() as session:
        other = User(email="second@example.com")
        session.add(other)
        session.flush()
        other_id = other.id
        session.commit()
    theirs = connect("ocado", user_id=other_id, key="theirs")

    client.get("/api/cart/ocado/basket")
    assert client.adapters["ocado"].seen == [mine]

    main.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=other_id)
    client.get("/api/cart/ocado/basket")

    assert client.adapters["ocado"].seen == [mine, theirs]


def test_logging_in_connects_an_account_the_first_time(client):
    """A user with no row gets one, which is how anybody ever connects a shop."""
    assert client.get("/api/cart/sainsburys/status").json()["status"] == "logged_out"

    response = client.post(
        "/api/cart/sainsburys/login",
        json={"email": "new@example.com", "password": "secret"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready", "stage": "idle", "email": "new@example.com"
    }
    with get_session_factory()() as session:
        row = session.scalar(
            select(RetailerAccount).where(RetailerAccount.retailer == "sainsburys")
        )
        assert row.status == "connected"
        assert row.last_ok_at is not None


def test_logging_in_again_keeps_the_same_account_key(connected):
    """The key names a browser profile Ocado has learned to trust. It survives."""
    connected.post(
        "/api/cart/ocado/login",
        json={"email": "changed@example.com", "password": "secret"},
    )

    with get_session_factory()() as session:
        rows = list(
            session.scalars(
                select(RetailerAccount).where(RetailerAccount.retailer == "ocado")
            )
        )
    assert [row.key for row in rows] == [connected.keys["ocado"]]
    assert rows[0].email == "changed@example.com"


def test_logging_out_keeps_the_row_so_the_browser_profile_survives(connected):
    connected.post("/api/cart/ocado/logout")

    with get_session_factory()() as session:
        row = session.scalar(
            select(RetailerAccount).where(RetailerAccount.retailer == "ocado")
        )
    assert row is not None, "disconnecting must not throw away the account key"
    assert row.status == "never"
    assert row.last_ok_at is None


def test_login_requires_credentials_without_echoing_the_password(client):
    response = client.post(
        "/api/cart/ocado/login",
        json={"email": "", "password": "do-not-echo"},
    )

    assert response.status_code == 400
    assert "do-not-echo" not in response.text


def test_a_short_password_is_not_echoed_back_by_a_validation_error(client):
    """Why the field carries no length constraint: a 422 quotes the input."""
    response = client.post("/api/cart/ocado/login", json={"email": "a@b.c", "password": "x"})

    assert "x" not in response.json().get("detail", "")


def test_a_shop_with_no_cart_integration_is_a_404(client):
    assert client.get("/api/cart/waitrose/status").status_code == 404


def test_each_shop_gets_its_own_adapter(connected):
    """The retailer in the path is what decides whose trolley is written to."""
    connected.post("/api/cart/ocado/basket/push", json={"selections": []})
    connected.post("/api/cart/sainsburys/basket/push", json={"selections": []})

    assert connected.adapters["ocado"].pushed == ["ocado"]
    assert connected.adapters["sainsburys"].pushed == ["sainsburys"]


def test_plan_exposes_checkout_items_and_accepts_an_empty_week(client, monkeypatch):
    monkeypatch.setattr(cart_api, "_rebuild", lambda *args, **kwargs: (None, Basket()))
    monkeypatch.setattr(cart_api, "read_ledger", lambda *args, **kwargs: CartLedger())
    monkeypatch.setattr(
        cart_api,
        "_checkout_items",
        lambda *args, **kwargs: [
            {
                "sku": "stale",
                "name": "Stale product",
                "desired_quantity": 0,
                "synced_quantity": 1,
                "cart_quantity": 1,
                "cost": 2.5,
                "cost_source": "live",
                "status": "changed",
            }
        ],
    )

    response = client.post("/api/cart/ocado/basket/plan", json={"selections": []})

    assert response.status_code == 200
    assert response.json()["checkout_items"] == [
        {
            "sku": "stale",
            "name": "Stale product",
            "url": None,
            "pack_size_raw": None,
            "desired_quantity": 0,
            "synced_quantity": 1,
            "cart_quantity": 1,
            "cost": 2.5,
            "cost_source": "live",
            "status": "changed",
        }
    ]


def test_rebuild_prices_the_week_at_the_shop_it_will_be_pushed_to(monkeypatch):
    """Not the user's active shop: the SKUs have to exist in the cart's catalogue."""
    requested = []
    index = SimpleNamespace()
    basket = Basket()

    def load_index(_factory, recipe_ids, _csv_path, retailer):
        requested.append((recipe_ids, retailer))
        return index

    monkeypatch.setattr(cart_api, "_load_planner_index", load_index)
    monkeypatch.setattr(cart_api, "build_basket", lambda *args, **kwargs: basket)

    assert cart_api._rebuild(object(), "sainsburys", [31, 32], None, []) == (index, basket)
    assert requested == [([31, 32], "sainsburys")]


# --- what a push leaves in the cupboard ---------------------------------------

def _pantry_basket():
    """One ambient line and one chilled one, both bought with a remainder."""
    from app.planner.basket import BasketContribution, BasketLine, Cover, PackChoice
    from app.planner.index import Pack

    def line(key, name, salvage):
        chosen = Pack(
            sku=f"sku-{key}", product_name=name, capacity_g=1000.0, price=2.0,
            salvage=salvage, rank=1, match_type="exact", pack_size_raw="1kg",
        )
        return BasketLine(
            key=key, name=name, need_g=300.0,
            cover=Cover(
                choices=(PackChoice(pack=chosen, count=1),), need_g=300.0,
                capacity_g=1000.0, cost=2.0, leftover_g=700.0, waste_gbp=0.1,
                salvage=salvage,
            ),
            contributions=(
                BasketContribution(
                    recipe_id=7, recipe_name="Rice Bowl", grams=300.0,
                    quantity=None, quantity_unit="g",
                ),
            ),
        )

    return Basket(lines=[
        line("name:rice", "Basmati Rice", 0.85),
        line("name:chicken", "Chicken Thighs", 0.15),
    ])


def test_a_push_records_the_shop_and_stocks_the_cupboard(connected, monkeypatch):
    """The push is the evidence: nothing here watches a delivery, so a basket
    reaching a real trolley is the last observable point in the chain."""
    from app.db.models import PantryLot, PlanWeekPush

    basket = _pantry_basket()
    monkeypatch.setattr(cart_api, "_load_planner_index", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cart_api, "build_basket", lambda *a, **k: basket)
    week = sched.format_date(sched.upcoming_week_start())

    response = connected.post(
        "/api/cart/ocado/basket/push", json={"selections": [], "week_start": week}
    )
    assert response.status_code == 200

    with get_session_factory()() as session:
        pushes = session.scalars(select(PlanWeekPush)).all()
        lots = session.scalars(select(PantryLot)).all()

    assert [(p.retailer, p.week_start) for p in pushes] == [("ocado", week)]
    # Only the ambient line: the chiller would drift faster than it would save.
    assert [(lot.ingredient_key, lot.available_g) for lot in lots] == [
        ("name:rice", 1000.0)
    ]


def test_a_push_with_no_week_leaves_the_cupboard_alone(connected, monkeypatch):
    """A draw and a deposit have to move together, and neither can be attributed
    to a shop that has no week."""
    from app.db.models import PantryLot

    monkeypatch.setattr(cart_api, "_load_planner_index", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cart_api, "build_basket", lambda *a, **k: _pantry_basket())

    connected.post("/api/cart/ocado/basket/push", json={"selections": []})

    with get_session_factory()() as session:
        assert session.scalars(select(PantryLot)).all() == []


def test_an_owned_line_is_not_stocked(connected, monkeypatch):
    """"I already have it" says nothing about how much."""
    from app.db.models import PantryLot

    monkeypatch.setattr(cart_api, "_load_planner_index", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cart_api, "build_basket", lambda *a, **k: _pantry_basket())
    week = sched.format_date(sched.upcoming_week_start())

    connected.post(
        "/api/cart/ocado/basket/push",
        json={"selections": [], "week_start": week, "owned_item_keys": ["name:rice"]},
    )

    with get_session_factory()() as session:
        assert session.scalars(select(PantryLot)).all() == []
