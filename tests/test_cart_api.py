"""The shared cart routes, exercised for every shop that has a cart.

These used to be Ocado's alone at ``/api/ocado/*``. The point of most of what
follows is that they are now written once: the same request against
``/api/cart/ocado`` and ``/api/cart/sainsburys`` should behave the same way, and
a shop with no cart integration should be told apart from a typo.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from app.api import cart as cart_api
from app.api.cart import get_cart_adapter
from app.api.deps import get_current_user, get_session_factory
from app.cart.adapters import AccountInfo, AuthStatus, CartAdapter, CartItem, CartSnapshot
from app.db.models import RetailerAccount, User
from app.cart.merge import CartLedger, PushPlan
from app.planner.basket import Basket

SHOPS = ["ocado", "sainsburys"]


class FakeAdapter(CartAdapter):
    """A shop that answers everything without a network or a login."""

    def __init__(self, retailer="ocado", *, accounts=("default",)):
        self.retailer = retailer
        self._accounts = accounts
        self.pushed: list[str] = []
        self.logged_out: list[str] = []

    def accounts(self):
        return [AccountInfo(id=account, label=account.title()) for account in self._accounts]

    def status(self, account_id=None):
        return AuthStatus(account_id=account_id or "default", status="logged_out", stage="idle")

    def ensure_authenticated(self, account_id=None, *, email=None, password=None):
        return AuthStatus(
            account_id=account_id or "default",
            status="ready" if email and password else "needs_password",
        )

    def submit_otp(self, code, account_id=None):
        return AuthStatus(account_id=account_id or "default", status="ready")

    def logout(self, account_id=None):
        account_id = account_id or "default"
        self.logged_out.append(account_id)
        return AuthStatus(account_id=account_id, status="logged_out", stage="idle")

    def cart(self, account_id=None):
        return CartSnapshot(
            items=(CartItem(sku="sku-a", quantity=2, cost=3.5),), raw={"items": ["raw"]}
        )

    def plan_push(self, basket, **kwargs):
        return PushPlan()

    def push_basket(self, basket, **kwargs):
        self.pushed.append(self.retailer)
        from app.cart.merge import PushResult

        return PushResult(ledger=CartLedger())


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


@pytest.mark.parametrize("retailer", SHOPS)
def test_status_reports_the_ladder_state(client, retailer):
    response = client.get(f"/api/cart/{retailer}/status")

    assert response.status_code == 200
    assert response.json()["status"] in {
        "logged_out", "needs_password", "awaiting_otp", "ready"
    }


@pytest.mark.parametrize("retailer", SHOPS)
def test_status_also_reports_the_stage(client, retailer):
    """What the page polls for while a login request is still blocked."""
    assert client.get(f"/api/cart/{retailer}/status").json()["stage"] == "idle"


@pytest.mark.parametrize("retailer", SHOPS)
def test_accounts_are_listed_with_a_default(client, retailer):
    body = client.get(f"/api/cart/{retailer}/accounts").json()

    assert body["default_account_id"] == "default"
    assert [item["id"] for item in body["items"]] == ["default"]


@pytest.mark.parametrize("retailer", SHOPS)
def test_the_basket_comes_back_for_either_shop(client, retailer):
    response = client.get(f"/api/cart/{retailer}/basket")

    assert response.status_code == 200
    assert response.json()["raw"] == {"items": ["raw"]}


@pytest.mark.parametrize("retailer", SHOPS)
def test_otp_rejects_an_empty_code(client, retailer):
    assert client.post(f"/api/cart/{retailer}/otp", json={"code": ""}).status_code == 422


@pytest.mark.parametrize("retailer", SHOPS)
def test_a_quiet_refresh_never_climbs_to_the_password(client, retailer):
    """The rung that would email somebody a code, for opening a page."""
    body = client.post(f"/api/cart/{retailer}/session/refresh", json={}).json()

    assert body["status"] == "needs_password"
    login = client.post(
        f"/api/cart/{retailer}/login",
        json={"email": "a@example.com", "password": "secret"},
    )
    assert login.json()["status"] == "ready"


@pytest.mark.parametrize("retailer", SHOPS)
def test_logout_forgets_the_selected_retailers_session(client, retailer):
    response = client.post(f"/api/cart/{retailer}/logout")

    assert response.status_code == 200
    assert response.json()["status"] == "logged_out"
    assert client.adapters[retailer].logged_out == ["default"]


def test_logout_resolves_the_retailer_account_owned_by_the_current_user(client):
    with get_session_factory()() as session:
        user = User(email="second@example.com")
        session.add(user)
        session.flush()
        session.add(
            RetailerAccount(
                user_id=user.id,
                retailer="ocado",
                key="second",
                email="shopper@example.com",
            )
        )
        session.commit()
        user_id = user.id

    main.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=user_id)
    client.adapters["ocado"] = FakeAdapter("ocado", accounts=("default", "second"))

    response = client.post("/api/cart/ocado/logout")

    assert response.status_code == 200
    assert client.adapters["ocado"].logged_out == ["second"]


def test_login_requires_credentials_without_echoing_the_password(client):
    response = client.post(
        "/api/cart/ocado/login",
        json={"email": "", "password": "do-not-echo"},
    )

    assert response.status_code == 400
    assert "do-not-echo" not in response.text


def test_a_shop_with_no_cart_integration_is_a_404(client):
    assert client.get("/api/cart/waitrose/status").status_code == 404


def test_an_unknown_account_is_rejected(client):
    assert client.get("/api/cart/ocado/status?account_id=__missing__").status_code == 404


def test_each_shop_gets_its_own_adapter(client):
    """The retailer in the path is what decides whose trolley is written to."""
    client.post("/api/cart/ocado/basket/push", json={"selections": []})
    client.post("/api/cart/sainsburys/basket/push", json={"selections": []})

    assert client.adapters["ocado"].pushed == ["ocado"]
    assert client.adapters["sainsburys"].pushed == ["sainsburys"]


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
