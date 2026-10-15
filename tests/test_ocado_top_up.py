"""Adding things to the next Ocado order by name. Ocado and the model are faked."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import main
from app import config
from app.api import noodle_feed
from app.db.models import Product, RetailerAccount
from app.mapping import service
from app.mapping.candidates import gather_candidates
from app.ocado import amend, top_up
from app.planner.preferences import set_pack_preference
from tests.conftest import seed_candidates, user_id

MILK = [{"sku": "milk-4pt", "name": "Cravendale Semi Skimmed 2L", "price": 2.9},
        {"sku": "milk-1pt", "name": "Ocado Semi Skimmed 1 Pint", "price": 0.95}]
COCONUT = [{"sku": "coco", "name": "Coconut Milk 400ml", "price": 1.2}]
TOMATOES = [{"sku": "chopped", "name": "Chopped Tomatoes 400g", "price": 0.5}]
CHERRY = [{"sku": "cherry", "name": "Cherry Tomatoes 250g", "price": 1.1}]


def _approve(s, key, name, products, *, line_count=100, status="approved"):
    seed_candidates(s, key, name, products, line_count=line_count)
    service.save_decision(s, gather_candidates(s, key), service.DecisionInput(
        status=status, accepted=[service.AcceptedInput(sku=p["sku"], rank=i) for i, p in enumerate(products, 1)]))


@pytest.fixture
def seeded(factory):
    with factory() as s:
        _approve(s, "name:semi skimmed milk", "Semi-Skimmed Milk", MILK, line_count=300)
        _approve(s, "name:coconut milk", "Coconut Milk", COCONUT, line_count=50)
        _approve(s, "name:chopped tomatoes", "Chopped Tomatoes", TOMATOES, line_count=200)
        _approve(s, "name:cherry tomatoes", "Cherry Tomatoes", CHERRY, line_count=80)
        _approve(s, "name:milk", "Milk", MILK, line_count=10)
        service.set_alias(s, "name:milk", "name:semi skimmed milk")
    return factory


def test_exact_names_and_aliases_win_outright(seeded):
    with seeded() as s:
        assert top_up.match(s, "Milk").key == "name:semi skimmed milk"  # through the alias
        assert top_up.match(s, "chopped tomato").key == "name:chopped tomatoes"  # plural or not


def test_a_single_name_holding_every_word_needs_no_model(seeded):
    with seeded() as s:
        assert top_up.match(s, "coconut", completer=_never).key == "name:coconut milk"


def test_several_candidates_go_to_the_model_which_may_refuse(seeded):
    seen = []

    def completer(system, user, schema):
        seen.append(user)
        return {"choice": "2"}

    with seeded() as s:
        assert top_up.match(s, "tomatoes", completer=completer).key == "name:cherry tomatoes"
        assert "1. Chopped Tomatoes" in seen[0]  # most used first
        assert top_up.match(s, "tomatoes", completer=lambda *a: {"choice": "none"}) == ["Chopped Tomatoes",
                                                                                          "Cherry Tomatoes"]
        assert top_up.match(s, "tomatoes") == ["Chopped Tomatoes", "Cherry Tomatoes"]  # no model, no guess


def test_a_more_specific_request_reaches_the_plainer_name(seeded):
    offered = []

    def completer(system, user, schema):
        offered.append(user)
        return {"choice": "1"}

    with seeded() as s:
        assert top_up.match(s, "organic semi skimmed milk", completer=completer).key == "name:semi skimmed milk"
    assert "1. Semi-Skimmed Milk" in offered[0]


def test_unmapped_things_are_not_guessed(seeded):
    with seeded() as s:
        assert top_up.match(s, "toilet roll", completer=_never) == []


def test_pick_takes_the_preferred_pack_then_rank_and_skips_sold_out(seeded):
    found = top_up.Match("name:semi skimmed milk", "Semi-Skimmed Milk")
    with seeded() as s:
        product, skus = top_up.pick(s, found, user_id(s))
        assert product.sku == "milk-4pt" and skus == {"milk-4pt", "milk-1pt"}
        set_pack_preference(s, user_id(s), "name:semi skimmed milk", "milk-1pt")
        s.commit()
        assert top_up.pick(s, found, user_id(s))[0].sku == "milk-1pt"
        assert top_up.pick(s, found, user_id(s), stock=lambda skus: {"milk-1pt": False})[0].sku == "milk-4pt"
        for p in s.scalars(select(Product)):
            p.in_stock = 0
        s.commit()
        assert top_up.pick(s, found, user_id(s))[0] is None


def _never(*_):
    raise AssertionError("the model wasn't needed")


class FakeOcado:
    def __init__(self, orders, items=None):
        self._orders, self.items, self.changes = orders, dict(items or {}), []

    def orders(self, pending=False):
        return self._orders

    def get_order(self, order_id):
        return {"items": [{"product": sku, "quantity": q} for sku, q in self.items.items()]}


def _order(order_id, start, editable=True):
    return {"orderId": order_id, "status": "PLACED", "editStatus": "EDITABLE" if editable else "NOT_EDITABLE",
            "dates": {"deliveryStartDate": start, "deliveryEndDate": start}}


@pytest.fixture
def changes(monkeypatch):
    def change(client, order_id, quantities=None, slot_id=None):
        client.changes.append((order_id, quantities))
        client.items.update(quantities)
        return {}
    monkeypatch.setattr(amend, "change_order", change)


def _account(email):
    return RetailerAccount(retailer="ocado", key=email, email=email)


def test_add_goes_on_the_soonest_editable_order_across_the_household(seeded, changes):
    past_cutoff = FakeOcado([_order("1", "2030-01-01T10:00", editable=False)])
    hers = FakeOcado([_order("2", "2030-01-03T10:00")], items={"chopped": 2})
    mine = FakeOcado([_order("3", "2030-01-05T10:00")])
    accounts = [(_account("me"), mine), (_account("her"), past_cutoff), (_account("her2"), hers)]
    with seeded() as s:
        out = top_up.add(s, user_id(s), [top_up.Want("milk"), top_up.Want("chopped tomatoes", 2),
                                         top_up.Want("chopped tomatoes"), top_up.Want("toilet roll"),
                                         top_up.Want("tomatoes")], accounts)
    assert hers.changes == [("2", {"milk-4pt": 1, "chopped": 4})] and not mine.changes
    assert out["order"]["order_id"] == "2" and out["order"]["account"] == "her2"
    assert [(a["sku"], a["quantity"]) for a in out["added"]] == [("milk-4pt", 1), ("chopped", 2)]
    assert [s["reason"] for s in out["skipped"]] == ["not something HolaFresca knows how to buy",
                                                     "which one? Chopped Tomatoes, Cherry Tomatoes",
                                                     "already on the order (×2)"]


def test_nothing_recognised_touches_no_order(seeded, changes):
    with seeded() as s:
        out = top_up.add(s, user_id(s), [top_up.Want("toilet roll")], [])
    assert out["order"] is None and out["added"] == []


def test_no_editable_order_says_why(seeded):
    with seeded() as s, pytest.raises(top_up.NoOrder, match="past their edit cutoff"):
        top_up.add(s, user_id(s), [top_up.Want("milk")],
                   [(_account("me"), FakeOcado([_order("1", "2030-01-01", editable=False)]))])


def test_take_back_subtracts_what_was_added(changes, monkeypatch):
    client = FakeOcado([_order("2", "2030-01-03")], items={"milk-4pt": 3, "chopped": 2})
    monkeypatch.setattr(amend, "find_order", lambda c, order_id: c._orders[0])
    out = top_up.take_back("2", {"milk-4pt": 1, "chopped": 2, "gone": 1}, [(_account("me"), client)])
    assert client.changes == [("2", {"milk-4pt": 2, "chopped": 0})] and out["removed"] == {"milk-4pt": 1, "chopped": 2}


def test_endpoint_needs_the_feed_token_and_reports_a_blocked_order(monkeypatch):
    monkeypatch.setattr(config, "NOODLE_FEED_TOKEN", "tok")
    monkeypatch.setattr(noodle_feed, "_ocado_accounts", lambda session, user: [(_account("me"), object())])
    monkeypatch.setattr(top_up, "default_completer", lambda: None)

    def blocked(*args, **kwargs):
        raise top_up.NoOrder("There's no coming Ocado order to add to")
    monkeypatch.setattr(top_up, "add", blocked)
    with TestClient(main.app) as client:
        body = {"items": [{"text": "milk"}]}
        assert client.post("/api/noodle/shopping", json=body).status_code == 401
        response = client.post("/api/noodle/shopping", json=body, headers={"Authorization": "Bearer tok"})
        assert response.status_code == 409 and "no coming Ocado order" in response.json()["detail"]
        too_many = {"items": [{"text": "eggs", "quantity": 100}]}
        assert client.post("/api/noodle/shopping", json=too_many, headers={"Authorization": "Bearer tok"}).status_code == 422
