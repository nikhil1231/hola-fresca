"""The feed Noodle pulls: token-gated, household-wide, and one broken account doesn't sink it."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from app import config
from app.api import noodle_feed
from app.api.deps import get_session
from app.db import retailer_accounts
from app.ocado import orders as ocado_orders

ORDERS = json.loads((Path(__file__).parent / "fixtures" / "ocado" / "orders.json").read_text())


class FakeOcado:
    def orders(self, pending=False):
        return [ORDERS["entities"]["order"][i] for i in ORDERS["result"]]


def test_order_summary():
    order = ocado_orders.summarise(FakeOcado().orders()[0])
    assert (order.total, order.items, order.editable, order.recurring) == ("92.34", 35, True, True)
    assert order.edit_until == "2030-10-19T17:25:00+01:00"


def test_ocado_section_lists_coming_orders_and_their_edit_deadline():
    lines, upcoming = noodle_feed.ocado_section(None, "anuja@x.com", client=FakeOcado())
    assert len(lines) == 1 and "35 items, £92.34" in lines[0] and "editable until" in lines[0]
    assert [u["kind"] for u in upcoming] == ["delivery", "deadline"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "NOODLE_FEED_TOKEN", "tok")
    with TestClient(main.app) as test_client:
        yield test_client


def test_feed_needs_its_token(client):
    assert client.get("/api/noodle/feed").status_code == 401
    assert client.get("/api/noodle/feed", headers={"Authorization": "Bearer tok"}).json() == {
        "context": "No shopping accounts connected.", "upcoming": []}


def test_feed_covers_every_connected_account_and_survives_a_broken_one(client, monkeypatch):
    session = next(main.app.dependency_overrides.get(get_session, get_session)())
    from app.db.models import User
    user = session.query(User).first()
    monkeypatch.setattr(config, "HOUSEHOLD", {(user.email or "").lower()} - {""})
    for retailer in ("ocado", "hellofresh"):
        account = retailer_accounts.connect(session, user.id, retailer, email=f"{retailer}@x.com")
        retailer_accounts.record_status(session, account, "ready")

    def broken(account, whose, client=None):
        raise RuntimeError("session expired")

    monkeypatch.setitem(noodle_feed.SECTIONS, "ocado",
                        lambda account, whose: noodle_feed.ocado_section(account, whose, client=FakeOcado()))
    monkeypatch.setitem(noodle_feed.SECTIONS, "hellofresh", broken)
    body = client.get("/api/noodle/feed", headers={"Authorization": "Bearer tok"}).json()
    assert "£92.34" in body["context"] and "hellofresh (hellofresh@x.com): unavailable (session expired)" in body["context"]
    assert [u["at"] for u in body["upcoming"]] == sorted(u["at"] for u in body["upcoming"])


def test_feed_leaves_out_accounts_outside_the_household(client, monkeypatch):
    session = next(main.app.dependency_overrides.get(get_session, get_session)())
    from app.db.models import User
    me = session.query(User).first()
    me.email = "me@x.com"
    friend = User(email="friend@x.com", name="Friend")
    session.add(friend)
    session.commit()
    for user in (me, friend):
        retailer_accounts.record_status(session, retailer_accounts.connect(session, user.id, "ocado", email=f"{user.id}@shop.com"), "ready")
    monkeypatch.setattr(config, "HOUSEHOLD", {"me@x.com"})
    seen = []
    monkeypatch.setitem(noodle_feed.SECTIONS, "ocado", lambda account, whose: (seen.append(whose) or [f"- {whose}"], []))
    client.get("/api/noodle/feed", headers={"Authorization": "Bearer tok"})
    assert len(seen) == 1 and seen[0] in (f"{me.id}@shop.com", f"user {me.id}")  # the friend's account isn't read
