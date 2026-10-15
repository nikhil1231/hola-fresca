"""From what was said to a planned change, and the rules the code holds the model to. Ocado and the model are faked."""
from __future__ import annotations

from datetime import date

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import main
from app import config
from app.api import noodle_feed
from app.db.models import ProductDescription, Purchase, RetailerAccount
from app.ocado import amend, resolver, top_up
from tests.conftest import seed_candidates, user_id

JASONS, WARBURTONS, MILK, BANANAS, TROUT, SALMON = "jasons", "warburtons", "milk", "bananas", "trout", "salmon"
BREAD = [{"sku": JASONS, "name": "Jason's Seeded Protein Loaf 450g", "brand": "Jason's", "category": "Bread"},
         {"sku": WARBURTONS, "name": "Warburtons Protein Power Loaf 400g", "brand": "Warburtons", "category": "Bread"}]
OTHER = [{"sku": MILK, "name": "Yeo Valley Organic Whole Milk 2L"}, {"sku": BANANAS, "name": "Ocado Bananas x5"},
         {"sku": SALMON, "name": "Ocado Scottish Salmon Fillets x2"}, {"sku": TROUT, "name": "Ocado Trout Fillets x2"}]


class FakeOcado:
    def __init__(self, items):
        self.items, self.changes = dict(items), []

    def orders(self, pending=False):
        return [{"orderId": "o9", "status": "PLACED", "editStatus": "EDITABLE",
                 "dates": {"deliveryStartDate": "2030-10-17T10:00:00+01:00",
                           "confirmOrderChangesBy": "2030-10-15T23:00:00+01:00"}}]

    def get_order(self, order_id):
        return {"items": [{"product": sku, "quantity": q} for sku, q in self.items.items()]}


@pytest.fixture
def db(factory):
    with factory() as s:
        seed_candidates(s, "name:bread", "Bread", BREAD)
        seed_candidates(s, "name:other", "Other", OTHER)
        bought = {JASONS: 13, WARBURTONS: 3, MILK: 20, BANANAS: 15, SALMON: 4}
        for sku, times in bought.items():
            s.add_all(Purchase(user_id=user_id(s), retailer="ocado", account_key="anuja", order_id=f"o{i}", sku=sku,
                               quantity=1, delivered_on=f"2026-{4 + i % 6:02d}-1{i % 9}") for i in range(times))
        s.add(ProductDescription(retailer="ocado", sku=WARBURTONS, packaging="green and white bag, Warburtons logo"))
        s.commit()
        yield s


def accounts(s, items=None):
    account = s.scalar(select(RetailerAccount).where(RetailerAccount.retailer == "ocado"))
    return [(account, FakeOcado(items if items is not None else {MILK: 1, BANANAS: 1, SALMON: 1}))]


def scripted(*steps):
    """A model that makes the given tool calls, one per turn, then answers with the last step."""
    turns = []

    def model(messages, tools, schema):
        turns.append(list(messages))
        step = steps[len(turns) - 1]
        if "tool" in step:
            return {"content": None, "tool_calls": [{"id": f"c{len(turns)}", "name": step["tool"],
                                                     "arguments": json.dumps(step.get("args", {}))}]}
        return {"content": json.dumps({"question": None, "options": [], "summary": "", **step}), "tool_calls": []}

    model.turns = turns
    return model


def change(op, sku, quantity=None, replaces=None):
    return {"op": op, "sku": sku, "quantity": quantity, "replaces": replaces}


def test_the_other_protein_bread_comes_from_the_history(db):
    model = scripted({"tool": "history", "args": {"query": "protein bread", "since": None}},
                     {"changes": [change("add", "p2", 1)], "confidence": "sure",
                      "summary": "Warburtons Protein Power: your other protein loaf (3 orders), green pack"})
    out = resolver.plan(db, user_id(db), accounts(db), ["add some protein bread, not the usual one, the other one"],
                        model=model)
    shown = json.loads(model.turns[1][-1]["content"])
    assert [(p["id"], p["name"][:5], p["orders"]) for p in shown["products"]][:2] == [("p1", "Jason", 13),
                                                                                       ("p2", "Warbu", 3)]
    assert shown["products"][1]["packaging"] == "green and white bag, Warburtons logo"
    assert out["confidence"] == "sure" and out["question"] is None
    assert out["changes"] == [{**change("add", WARBURTONS, 1), "name": "Warburtons Protein Power Loaf 400g",
                               "replaces_name": None}]
    assert out["said"] == "Warburtons Protein Power Loaf 400g"
    assert out["order"]["order_id"] == "o9" and out["order"]["edit_until"] == "2030-10-15T23:00:00+01:00"


def test_a_product_no_tool_showed_is_refused(db):
    model = scripted({"tool": "product", "args": {"id": "made-up"}},
                     {"changes": [change("add", "made-up", 1)], "confidence": "sure"})
    out = resolver.plan(db, user_id(db), accounts(db), ["add some crumpets"], model=model)
    assert out["confidence"] == "unsure" and out["changes"] == [] and "made up" in out["question"]


def test_lines_that_werent_mentioned_are_left_alone(db):
    model = scripted({"tool": "order"}, {"changes": [change("remove", BANANAS), change("remove", MILK)],
                                         "confidence": "sure"})
    out = resolver.plan(db, user_id(db), accounts(db), ["take the bananas off"], model=model)
    assert out["confidence"] == "unsure" and out["changes"] == []
    assert out["refused"] == ["changing Yeo Valley Organic Whole Milk 2L, which you didn't mention or isn't on the order"]


def test_set_swap_and_remove_on_mentioned_lines(db):
    model = scripted({"tool": "order"}, {"tool": "product", "args": {"id": TROUT}},
                     {"changes": [change("set", MILK, 3), change("swap", TROUT, None, SALMON), change("remove", BANANAS)],
                      "confidence": "sure"})
    out = resolver.plan(db, user_id(db), accounts(db), ["make it three milks, swap the salmon for trout and no bananas"],
                        model=model)
    assert out["confidence"] == "sure", out["refused"]
    assert out["said"] == ("Yeo Valley Organic Whole Milk 2L to 3, Ocado Scottish Salmon Fillets x2 → Ocado Trout "
                           "Fillets x2, Ocado Bananas x5 off")


def test_quantities_and_line_counts_are_capped(db):
    big = scripted({"tool": "order"}, {"changes": [change("set", MILK, 50)], "confidence": "sure"})
    assert resolver.plan(db, user_id(db), accounts(db), ["fifty milks"], model=big)["confidence"] == "unsure"
    zero = scripted({"tool": "order"}, {"changes": [change("set", MILK, 0)], "confidence": "sure"})
    assert resolver.plan(db, user_id(db), accounts(db), ["no milk"], model=zero)["changes"][0]["op"] == "remove"
    many = scripted({"tool": "history", "args": {"query": None, "since": None}},
                    {"changes": [change("add", sku, 1) for sku in (JASONS, WARBURTONS, MILK, BANANAS, SALMON)] * 2,
                     "confidence": "sure"})
    out = resolver.plan(db, user_id(db), accounts(db), ["the usual"], model=many)
    assert out["confidence"] == "unsure" and "more than 8" in out["question"]


def test_unsure_keeps_the_pending_plan_and_offers_whole_plans(db):
    pending = [change("add", BANANAS, 1)]
    model = scripted({"tool": "history", "args": {"query": "bread", "since": None}},
                     {"changes": [], "confidence": "unsure", "question": "Which bread?", "options": [
                         {"label": "Jason's", "changes": [*pending, change("add", JASONS, 1)]},
                         {"label": "Warburtons", "changes": [*pending, change("add", WARBURTONS, 1)]}]})
    out = resolver.plan(db, user_id(db), accounts(db), ["bananas", "and some bread"], pending=pending, model=model)
    assert out["question"] == "Which bread?" and [c["sku"] for c in out["changes"]] == [BANANAS]
    assert [[c["sku"] for c in o["changes"]] for o in out["options"]] == [[BANANAS, JASONS], [BANANAS, WARBURTONS]]
    assert "Pending changes" in model.turns[0][1]["content"] and BANANAS in model.turns[0][1]["content"]


def test_a_likely_plan_goes_ahead_and_searches_are_capped(db, monkeypatch):
    monkeypatch.setattr(resolver, "_searches", __import__("collections").deque())
    searched = []

    class Searching(FakeOcado):
        def search(self, term, size=20):
            searched.append(term)
            return {"products": [{"productId": "crumpet", "name": "Warburtons Crumpets x6", "price": 1.1}]}

    accts = accounts(db)
    accts = [(accts[0][0], Searching(accts[0][1].items))]
    model = scripted(*[{"tool": "search", "args": {"term": "crumpets"}}] * 3,
                     {"changes": [change("add", "crumpet", 2)], "confidence": "likely"})
    out = resolver.plan(db, user_id(db), accts, ["two packs of crumpets"], model=model)
    assert searched == ["crumpets", "crumpets"] and "no searches left" in model.turns[3][-1]["content"]
    assert out["confidence"] == "likely" and out["said"] == "Warburtons Crumpets x6 ×2"


def test_targets_turn_the_plan_into_absolute_quantities():
    have = {MILK: 1, SALMON: 2, BANANAS: 1}
    plan = [change("add", MILK, 2), change("swap", TROUT, None, SALMON), change("remove", BANANAS),
            change("set", JASONS, 1), change("add", JASONS, 1)]
    assert resolver.targets(plan, have) == {MILK: 3, SALMON: 0, TROUT: 2, BANANAS: 0, JASONS: 2}
    assert resolver.targets([change("remove", "not-there")], have) == {}


def test_apply_makes_the_whole_plan_in_one_checkout(db, monkeypatch):
    def change_order(client, order_id, quantities=None, slot_id=None):
        client.changes.append((order_id, quantities))
        return {}

    monkeypatch.setattr(amend, "change_order", change_order)
    monkeypatch.setattr(amend, "find_order", lambda client, order_id: client.orders()[0])
    accts = accounts(db)
    out = resolver.apply(db, accts, [change("add", MILK, 2), change("remove", BANANAS)], order_id="o9")
    assert accts[0][1].changes == [("o9", {MILK: 3, BANANAS: 0})]
    assert out["applied"] == [{"sku": MILK, "name": "Yeo Valley Organic Whole Milk 2L", "before": 1, "after": 3},
                              {"sku": BANANAS, "name": "Ocado Bananas x5", "before": 1, "after": 0}]
    with pytest.raises(ValueError):
        resolver.apply(db, accts, [change("swap", TROUT)])


def test_undo_puts_back_what_was_taken_off(monkeypatch):
    client = FakeOcado({MILK: 3})
    monkeypatch.setattr(amend, "find_order", lambda c, order_id: c.orders()[0])
    monkeypatch.setattr(amend, "change_order", lambda c, oid, quantities=None, slot_id=None: c.changes.append(quantities))
    top_up.take_back("o9", {MILK: 2, BANANAS: -1}, [(RetailerAccount(key="a"), client)])
    assert client.changes == [{MILK: 1, BANANAS: 1}]


def test_endpoints(monkeypatch):
    monkeypatch.setattr(config, "NOODLE_FEED_TOKEN", "tok")
    monkeypatch.setattr(noodle_feed, "_ocado_accounts", lambda session, user: [(RetailerAccount(key="a"), object())])

    def no_order(*args, **kwargs):
        raise top_up.NoOrder("There's no coming Ocado order to add to")

    monkeypatch.setattr(resolver, "plan", no_order)
    auth = {"Authorization": "Bearer tok"}
    with TestClient(main.app) as client:
        body = {"instructions": ["the other protein bread"]}
        assert client.post("/api/noodle/shopping/plan", json=body).status_code == 401
        response = client.post("/api/noodle/shopping/plan", json=body, headers=auth)
        assert response.status_code == 409 and "no coming Ocado order" in response.json()["detail"]
        bad = {"changes": [{"op": "eat", "sku": "x"}]}
        assert client.post("/api/noodle/shopping/apply", json=bad, headers=auth).status_code == 422


DELIVERY = {"slot_id": "mon-10", "start": "2030-10-14T10:00:00+01:00", "end": "2030-10-14T11:00:00+01:00"}


def test_delivery_plan_uses_available_slots_and_keeps_product_changes(db, monkeypatch):
    from app.ocado.client import Slot
    monkeypatch.setattr(amend, "slots_for", lambda *a, **kw: [
        Slot(DELIVERY["slot_id"], DELIVERY["start"], DELIVERY["end"], available=True),
        Slot("unavailable", DELIVERY["start"], DELIVERY["end"], available=False)])
    model = scripted({"tool": "delivery_slots"},
                     {"changes": [change("add", MILK, 1)], "slot_id": "mon-10", "confidence": "sure"})
    out = resolver.plan(db, user_id(db), accounts(db), ["add milk", "move delivery to Monday at the same time"],
                        pending=[change("add", MILK, 1)], model=model, today=date(2030, 10, 10))
    assert out["delivery"] == DELIVERY
    assert out["changes"][0]["sku"] == MILK
    assert "Mon 14 Oct 10:00" in out["said"]
    assert len(json.loads(model.turns[1][-1]["content"])["slots"]) == 1


def test_invented_slot_is_held_for_clarification(db):
    model = scripted({"changes": [], "slot_id": "invented", "confidence": "sure"})
    out = resolver.plan(db, user_id(db), accounts(db), ["move delivery to Monday"], model=model)
    assert out["confidence"] == "unsure" and out["delivery"] is None
    assert "verified" in out["question"]


@pytest.mark.parametrize("changes", [[], [change("add", MILK, 1)]])
def test_delivery_and_products_use_one_checkout_and_report_saved_delivery(db, monkeypatch, changes):
    calls = []
    accts = accounts(db)
    raw = {**accts[0][1].orders()[0], "slotId": "old-slot"}
    monkeypatch.setattr(amend, "find_order", lambda *a: raw)
    def change_order(client, oid, quantities=None, slot_id=None):
        calls.append((quantities, slot_id))
        return {**raw, "slotId": slot_id, "dates": {"deliveryStartDate": DELIVERY["start"],
                                                    "deliveryEndDate": DELIVERY["end"]}}
    monkeypatch.setattr(amend, "change_order", change_order)
    out = resolver.apply(db, accts, changes, order_id="o9", delivery=DELIVERY)
    assert calls == [({MILK: 2} if changes else {}, "mon-10")]
    assert out["delivery_changed"] and out["previous_slot_id"] == "old-slot"
    assert out["order"]["delivery_start"] == DELIVERY["start"]


def test_delivery_undo_can_restore_slot_without_product_changes(monkeypatch):
    client = FakeOcado({MILK: 1})
    monkeypatch.setattr(amend, "find_order", lambda c, oid: c.orders()[0])
    calls = []
    monkeypatch.setattr(amend, "change_order", lambda c, oid, **kw: calls.append(kw))
    top_up.take_back("o9", {}, [(RetailerAccount(key="a"), client)], slot_id="old-slot")
    assert calls == [{"quantities": {}, "slot_id": "old-slot"}]


def test_delivery_request_cannot_silently_become_product_only(db):
    model = scripted({"changes": [change("add", MILK, 1)], "confidence": "sure"})
    out = resolver.plan(db, user_id(db), accounts(db), ["move that shopping order to Monday instead of Tuesday"],
                        pending=[change("add", MILK, 1)], model=model)
    assert out["confidence"] == "unsure"
    assert "delivery slot" in out["question"]


def test_product_followup_does_not_drop_pending_delivery(db):
    model = scripted({"changes": [change("add", MILK, 1)], "confidence": "sure"})
    out = resolver.plan(db, user_id(db), accounts(db), ["and add milk"], model=model, pending_delivery=DELIVERY)
    assert out["delivery"] == DELIVERY


def test_slot_overview_is_bounded_and_keeps_requested_day_visible(db, monkeypatch):
    from app.ocado.client import Slot
    slots = [Slot(f'{day}-{hour}', f'2030-10-{day:02d}T{hour:02d}:00:00+01:00',
                  f'2030-10-{day:02d}T{hour+1:02d}:00:00+01:00', available=True)
             for day in range(11, 25) for hour in range(6, 23)]
    monkeypatch.setattr(amend, 'slots_for', lambda *a, **kw: list(reversed(slots)))
    ctx = resolver.Context(db, user_id(db), accounts(db), today=date(2030, 10, 10))
    overview = resolver.tool_slots(ctx)
    assert len(json.dumps(overview)) < 12000
    assert len(overview['slots']) == 28
    assert any(s['start'].startswith('2030-10-14') for s in overview['slots'])
    monday = resolver.tool_slots(ctx, day='2030-10-14')
    assert len(monday['slots']) == 12
    assert all(s['start'].startswith('2030-10-14') for s in monday['slots'])
    assert monday['slots'][0]['start'] == '2030-10-14T10:00:00+01:00'


def test_delivery_only_payload_reaches_plan_apply_and_undo(monkeypatch):
    monkeypatch.setattr(config, 'NOODLE_FEED_TOKEN', 'tok')
    monkeypatch.setattr(noodle_feed, '_ocado_accounts', lambda *a: [])
    seen = []
    def plan(*args, **kwargs):
        seen.append(('plan', kwargs['pending_delivery']))
        return {'delivery': kwargs['pending_delivery']}
    def apply(*args, **kwargs):
        seen.append(('apply', kwargs['delivery']))
        return {'delivery_changed': True}
    def undo(*args, **kwargs):
        seen.append(('undo', kwargs['slot_id']))
        return {'order_id': 'o9'}
    monkeypatch.setattr(resolver, 'plan', plan)
    monkeypatch.setattr(resolver, 'apply', apply)
    monkeypatch.setattr(top_up, 'take_back', undo)
    headers = {'Authorization': 'Bearer tok'}
    with TestClient(main.app) as client:
        for endpoint, body in [
            ('plan', {'instructions': ['and bread'], 'pending_delivery': DELIVERY}),
            ('apply', {'order_id': 'o9', 'delivery': DELIVERY}),
            ('undo', {'order_id': 'o9', 'slot_id': 'old-slot'})]:
            response = client.post('/api/noodle/shopping/' + endpoint, json=body, headers=headers)
            assert response.status_code == 200, response.text
    assert seen == [('plan', DELIVERY), ('apply', DELIVERY), ('undo', 'old-slot')]
