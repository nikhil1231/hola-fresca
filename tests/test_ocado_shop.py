"""Shopping Ocado by hand: search results, the named trolley, and absolute quantity changes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from app.api import ocado as api
from app.ocado import shop
from app.ocado.client import OcadoClient

FIXTURES = Path(__file__).parent / "fixtures"
CART = json.loads((FIXTURES / "ocado" / "cart_view.json").read_text())
SKU = "9f24fc1d-281f-4c16-b7a0-94004918a720"


def test_basket_names_lines_and_passes_on_ocados_verdict():
    basket = shop.basket(CART, lambda skus: {SKU: "Bananas"})
    line = next(l for l in basket.lines if l.sku == SKU)
    assert (line.name, line.quantity, line.cost) == ("Bananas", 3, 2.97)
    assert basket.total == 7.72 and basket.minimum == 40.0
    assert not basket.can_checkout and set(basket.restrictions) == {"MISSING_SLOT", "NOT_REACHED_THRESHOLD"}


def test_search_results_are_normalised_products():
    payload = json.loads((FIXTURES / "ocado_search_potatoes.json").read_text())
    found = shop.search_results(payload, 1)
    assert [f.name for f in found] == ["Ocado White Potatoes 2kg"]


class FakeSession:
    def __init__(self):
        self.sent = []

    def request(self, method, path, **kwargs):
        self.sent.append((method, path, kwargs.get("json")))

        class R:
            content = b"{}"

            def raise_for_status(self):
                pass

            def json(self):
                return CART if "cart-view" in path else {}
        return R()


def test_set_quantities_sends_only_the_difference():
    session = FakeSession()
    OcadoClient(session).set_quantities({SKU: 1, "new": 2})
    assert session.sent[-1][2] == [{"productId": SKU, "quantity": -2, "meta": {}},
                                   {"productId": "new", "quantity": 2, "meta": {}}]
    session.sent.clear()
    OcadoClient(session).set_quantities({SKU: 3})
    assert [s[0] for s in session.sent] == ["GET"]  # already at 3: nothing written


@pytest.fixture
def client():
    session = FakeSession()
    main.app.dependency_overrides[api.get_ocado_client] = lambda: OcadoClient(session)
    try:
        with TestClient(main.app) as test_client:
            yield session, test_client
    finally:
        main.app.dependency_overrides.clear()


def test_basket_endpoints(client, monkeypatch):
    session, http = client
    monkeypatch.setattr(api, "fetch_statuses", lambda skus, session=None: {})
    assert http.get("/api/ocado/basket").json()["total"] == 7.72
    assert http.post("/api/ocado/basket/items", json={"items": []}).status_code == 422
    assert http.post("/api/ocado/basket/items", json={"items": [{"sku": SKU, "quantity": -1}]}).status_code == 422
    assert http.post("/api/ocado/basket/items", json={"items": [{"sku": SKU, "quantity": 0}]}).status_code == 200
    assert ("POST", "/api/cart/v1/carts/active/apply-quantity",
            [{"productId": SKU, "quantity": -3, "meta": {}}]) in session.sent
    assert http.get("/api/ocado/search?q=%20").status_code == 400
