"""Ocado routes. The client is faked; nothing here reaches the network."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from app.api import ocado as ocado_api
from app.api.ocado import get_ocado_client
from app.ocado.auth import AuthStage, AuthState
from app.ocado.client import Slot, normalize_slots
from app.ocado.sync import CartLedger, PushPlan
from app.planner.basket import Basket

FIXTURES = Path(__file__).parent / "fixtures" / "ocado"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self):
        self.reserved = None

    def cart_view(self):
        return fixture("cart_view")

    def slots(self, ddid=None, region=None):
        return normalize_slots(fixture("slots"))

    def reserve(self, slot_id, ddid=None, region=None):
        self.reserved = (slot_id, ddid, region)
        return fixture("reservation")


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    main.app.dependency_overrides[get_ocado_client] = lambda: fake
    monkeypatch.setattr(ocado_api, "OcadoClient", lambda session: fake)
    monkeypatch.setattr(
        ocado_api,
        "_runtime",
        lambda account_id=None: SimpleNamespace(
            session=None,
            account=SimpleNamespace(id=account_id or "default"),
            auth=SimpleNamespace(state=AuthState.LOGGED_OUT, stage=AuthStage.IDLE),
        ),
    )
    with TestClient(main.app) as test_client:
        test_client.fake = fake
        yield test_client
    main.app.dependency_overrides.clear()


def test_status_reports_the_ladder_state(client):
    response = client.get("/api/ocado/status")

    assert response.status_code == 200
    assert response.json()["status"] in {"logged_out", "awaiting_otp", "ready"}
    assert response.json()["account_id"] == "default"


def test_status_also_reports_the_stage(client):
    """What the page polls for while a login request is still blocked."""
    response = client.get("/api/ocado/status")

    assert response.json()["stage"] == "idle"


def test_unknown_account_is_rejected():
    with TestClient(main.app) as test_client:
        response = test_client.get("/api/ocado/status?account_id=__missing__")

    assert response.status_code == 404


def test_slots_serialise_the_dataclass(client):
    """Slot and PushLine use slots=True, so vars() raises - asdict is required."""
    response = client.get("/api/ocado/slots")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 64
    assert sum(i["available"] for i in items) == 35
    assert sum(i["eco"] for i in items) == 12
    assert {"slot_id", "start", "end", "day", "available", "eco", "price"} <= set(items[0])


def test_basket_returns_the_raw_cart(client):
    response = client.get("/api/ocado/basket")

    assert response.status_code == 200
    assert "checkoutGroups" in response.json()["raw"]


def test_plan_exposes_checkout_items_and_accepts_an_empty_week(client, monkeypatch):
    monkeypatch.setattr(ocado_api, "_rebuild", lambda *args, **kwargs: (None, Basket()))
    monkeypatch.setattr(ocado_api, "read_ledger", lambda *args, **kwargs: CartLedger())
    monkeypatch.setattr(ocado_api, "plan_push", lambda *args, **kwargs: PushPlan())
    monkeypatch.setattr(
        ocado_api,
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

    response = client.post("/api/ocado/basket/plan", json={"selections": []})

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


def test_ocado_rebuild_honours_recipe_subset(monkeypatch):
    requested = []
    index = SimpleNamespace()
    basket = Basket()

    def load_index(_factory, recipe_ids, _csv_path, retailer):
        requested.append((recipe_ids, retailer))
        return index

    monkeypatch.setattr(ocado_api, "_load_planner_index", load_index)
    monkeypatch.setattr(ocado_api, "build_basket", lambda *args, **kwargs: basket)

    assert ocado_api._rebuild(object(), [31, 32], None, []) == (index, basket)
    assert requested == [([31, 32], ocado_api.RETAILER)]


def test_reserve_passes_the_slot_through(client):
    response = client.post("/api/ocado/slots/reserve", json={"slot_id": "slot-9"})

    assert response.status_code == 200
    assert client.fake.reserved == ("slot-9", None, None)
    assert response.json()["raw"]["slot"]["slotId"]


def test_a_client_failure_surfaces_as_bad_gateway(client):
    def boom(*args, **kwargs):
        raise RuntimeError("ocado is down")

    client.fake.slots = boom
    response = client.get("/api/ocado/slots")

    assert response.status_code == 502
    assert "ocado is down" in response.json()["detail"]


def test_otp_rejects_an_empty_code(client):
    response = client.post("/api/ocado/otp", json={"code": ""})

    assert response.status_code == 422


def test_serialising_a_slot_dataclass_does_not_use_vars():
    from dataclasses import asdict

    slot = Slot(slot_id="s", available=True)
    assert asdict(slot)["slot_id"] == "s"
    with pytest.raises(TypeError):
        vars(slot)
