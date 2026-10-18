"""Shared shopping and durable queue regressions; no browser or network calls."""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config
from app.api import deps, ocado_shared
from app.cart.adapters import AuthStatus
from app.db.models import OcadoSharedQueue, PlanSettings, Product, RetailerAccount, User
from app.ocado import shared
from app.ocado.client import Slot
from app.ocado.shared_queue import QueueStore, Worker

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def booked(order_id="order", day=2):
    return {"orderId": order_id, "status": "FUTURE", "editStatus": "EDITABLE",
            "regionId": "region", "deliveryDestinationId": "address", "slotId": "old-slot",
            "dates": {"deliveryStartDate": f"2030-01-{day:02}T10:00:00Z",
                      "deliveryEndDate": f"2030-01-{day:02}T11:00:00Z",
                      "confirmOrderChangesBy": f"2030-01-{day - 1:02}T23:00:00Z"},
            "orderTotals": {"finalPrice": {"amount": "45.00", "currency": "GBP"}},
            "items": [{"product": "milk", "quantity": 2, "totalPrice": {"amount": "3.00"}}]}


class FakeSharedClient:
    """Reusable upstream fake for shared-app integration smoke tests."""

    def __init__(self):
        self.coming = []
        self.quantities = {"milk": 2}
        self.calls = []
        self.editing = None
        self.card_items = [{"walletItemId": "card", "paymentMethodType": "CARDS", "expired": False}]
        self.checkout_type = "checkout-result-response"
        self.restrictions = []
        self.fail_checkout = False
        self.checkout_body = None
        self.saved_order = None
        self.reserved = None

    def orders(self, pending=False):
        return copy.deepcopy(self.coming)

    def get_order(self, order_id):
        self.calls.append("get_order")
        return copy.deepcopy(next((o for o in self.coming if str(o["orderId"]) == order_id), {}))

    def cart_view(self):
        self.calls.append("cart_view")
        return {"activeCheckoutGroup": {"canCheckout": True, "checkoutRestrictions": []},
                "totals": {"itemPriceAfterPromos": {"amount": "3.00"}},
                "checkoutGroups": {"assignedCheckoutGroups": [{"itemGroups": [{"items": [
                    {"productId": sku, "quantity": quantity, "itemType": "BasketItem",
                     "totalPrices": {"finalPrice": {"amount": "3.00"}}}
                    for sku, quantity in self.quantities.items() if quantity
                ]}]}]}}

    def set_quantities(self, quantities):
        self.calls.append("quantities")
        self.quantities.update(quantities)

    def slots(self, ddid=None, region=None, days=7):
        self.calls.append("slots")
        return [Slot("new-slot", start="2030-01-03T10:00:00Z", available=True)]

    def reserve(self, slot_id, ddid=None, region=None):
        self.calls.append("reserve")
        self.reserved = slot_id
        return {"slot": {"slotId": slot_id}}

    def start_order_edit(self, order_id, region):
        self.calls.append("start_edit")
        self.editing = order_id

    def cancel_order_edit(self, order_id, region):
        self.calls.append("cancel_edit")
        self.editing = None

    def wallet_items(self):
        self.calls.append("wallet")
        return self.card_items

    def order_payments(self, order_id):
        return {"payments": [{"paymentMethod": {"type": "CARDS", "group": "PAY_NOW"}}]}

    def default_billing_address_id(self):
        return "billing"

    def checkout_start(self):
        self.calls.append("checkout_start")

    def checkout_summary(self):
        self.calls.append("summary")
        return {"activeCheckoutGroup": {"canCheckout": not self.restrictions,
                                        "checkoutRestrictions": self.restrictions}}

    def checkout(self, body, order_id=None):
        self.calls.append("checkout")
        self.checkout_body = body
        if self.fail_checkout:
            raise TimeoutError("Payment response lost")
        if self.checkout_type != "checkout-result-response":
            return {"type": self.checkout_type}
        order_id = self.editing or "new-order"
        order = next((o for o in self.coming if str(o["orderId"]) == order_id), None)
        if order is None:
            order = booked(order_id)
            self.coming.append(order)
        order["items"] = [{"product": sku, "quantity": qty} for sku, qty in self.quantities.items() if qty]
        if self.reserved:
            order["slotId"] = self.reserved
        self.saved_order = order_id
        self.editing = None
        return {"type": self.checkout_type, "orderId": order_id}

    def search(self, query, size=20):
        return {}


class FakeAdapter:
    def __init__(self):
        self.keys = []
        self.state = "logged_out"

    def status(self, key):
        self.keys.append(key)
        return AuthStatus(key, self.state, "idle")

    def ensure_authenticated(self, key, **credentials):
        self.keys.append(key)
        self.state = "ready"
        return AuthStatus(key, self.state, "ready")

    def submit_otp(self, code, key):
        return self.ensure_authenticated(key)

    def logout(self, key):
        self.keys.append(key)
        self.state = "logged_out"
        return AuthStatus(key, self.state, "idle")


@pytest.fixture
def setup_shared(factory, monkeypatch):
    monkeypatch.setattr(config, "OCADO_SHARED_ACCOUNT_KEY", "household-shared")
    with factory() as db:
        first = db.scalar(select(User).order_by(User.id))
        second = User(email="second@example.com", name="Second")
        db.add(second)
        db.commit()
        people = (first.id, second.id)
        with shared.account_lock():
            account = shared.connect_account(db, "shared@shop.example")
        owner_id = account.user_id
    return factory, people, owner_id


@pytest.fixture
def shared_api(factory, monkeypatch):
    monkeypatch.setattr(config, "OCADO_SHARED_ACCOUNT_KEY", "household-shared")
    fake, adapter = FakeSharedClient(), FakeAdapter()
    monkeypatch.setattr(shared, "make_client", lambda key: fake)
    with factory() as db:
        first = db.scalar(select(User).order_by(User.id))
        second = User(email="other@example.com")
        db.add(second)
        db.commit()
        people = [first.id, second.id]
    current = [people[0]]

    def session():
        with factory() as db:
            yield db

    def person():
        with factory() as db:
            return db.get(User, current[0])

    app = FastAPI()
    app.include_router(ocado_shared.router)
    app.dependency_overrides[deps.get_session] = session
    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_current_user] = person
    app.dependency_overrides[ocado_shared.get_shared_adapter] = lambda: adapter
    with TestClient(app) as api:
        yield api, fake, adapter, current, people


def _login(api):
    return api.post("/api/ocado-shared/login", json={"email": "shared@shop.example", "password": "secret"})


def _quantity(sku="milk", quantity=3):
    return {"kind": "quantity", "sku": sku, "quantity": quantity}


def test_reads_do_not_create_or_choose_an_arbitrary_personal_account(shared_api, factory):
    api, _, adapter, _, _ = shared_api
    with factory() as db:
        before = db.scalar(select(func.count()).select_from(User))
    assert api.get("/api/ocado-shared/status").json()["status"] == "logged_out"
    assert api.get("/api/ocado-shared/state").status_code == 404
    assert api.get("/api/ocado-shared/queue").json()["status"] == "idle"
    with factory() as db:
        assert shared.find_account(db) is None
        assert db.scalar(select(func.count()).select_from(User)) == before
        assert db.scalar(select(RetailerAccount).where(RetailerAccount.key == "default")) is not None
    assert adapter.keys == []


def test_all_users_login_and_read_exact_configured_account_with_dedicated_owner(shared_api, factory):
    api, _, adapter, current, people = shared_api
    assert _login(api).status_code == 200
    current[0] = people[1]
    assert api.get("/api/ocado-shared/status").json()["email"] == "shared@shop.example"
    assert api.post("/api/ocado-shared/session/refresh", json={}).status_code == 200
    with factory() as db:
        account = shared.find_account(db)
        assert account.key == "household-shared"
        assert account.user_id not in people
        owner = db.get(User, account.user_id)
        assert owner.email and owner.name and not owner.is_admin
        assert db.scalar(select(func.count()).select_from(RetailerAccount).where(
            RetailerAccount.key == "household-shared")) == 1
    assert set(adapter.keys) == {"household-shared"}


def test_booked_delivery_is_earliest_upcoming_and_read_does_not_open_edit(shared_api):
    api, fake, _, _, _ = shared_api
    _login(api)
    fake.coming = [booked("later", 5), booked("sooner", 3)]
    state = api.get("/api/ocado-shared/state").json()
    assert state["order"]["order_id"] == "sooner"
    assert state["order"]["edit_until"] == "2030-01-02T23:00:00Z"
    assert state["queue"]["target_id"] == "sooner"
    assert state["basket"]["lines"][0]["quantity"] == 2
    assert "start_edit" not in fake.calls
    assert api.get("/api/ocado-shared/slots").status_code == 200
    assert fake.calls[-3:] == ["start_edit", "slots", "cancel_edit"]
    assert fake.editing is None


def test_shared_projected_quantities_and_cancel_are_visible_across_people(shared_api):
    api, _, _, current, people = shared_api
    _login(api)
    first = api.post("/api/ocado-shared/actions", json={"target_id": "cart", **_quantity("milk", 4)}).json()
    current[0] = people[1]
    second = api.post("/api/ocado-shared/actions", json={"target_id": "cart", **_quantity("bread", 2), "name": "Bread"}).json()
    assert {a["user_id"] for a in second["actions"]} == set(people)
    state = api.get("/api/ocado-shared/state").json()
    assert {line["sku"]: line["quantity"] for line in state["basket"]["lines"]} == {"milk": 4, "bread": 2}
    assert all(line["cost"] is None for line in state["basket"]["lines"])
    assert state["basket"]["total"] == 3.0  # live estimate, not fabricated projected pricing
    assert api.get("/api/ocado-shared/queue").json() == second
    assert second["due_at"] >= first["due_at"]
    assert api.post("/api/ocado-shared/queue/cancel", json={"target_id": "cart"}).json()["actions"] == []
    assert api.get("/api/ocado-shared/state").json()["basket"]["lines"][0]["quantity"] == 2


def test_stale_target_rejected_without_touching_upstream_and_pending_target_stays_pinned(shared_api):
    api, fake, _, _, _ = shared_api
    _login(api)
    fake.coming = [booked("pinned", 5)]
    assert api.post("/api/ocado-shared/actions", json={"target_id": "cart", **_quantity()}).status_code == 409
    assert api.post("/api/ocado-shared/actions", json={"target_id": "pinned", **_quantity()}).status_code == 200
    fake.coming.append(booked("new-earlier", 3))
    assert api.get("/api/ocado-shared/state").json()["order"]["order_id"] == "pinned"
    fake.coming = [booked("new-earlier", 3)]
    assert api.post("/api/ocado-shared/actions", json={"target_id": "pinned", **_quantity()}).status_code == 409
    assert "quantities" not in fake.calls


@pytest.mark.parametrize("body", [
    {"target_id": "cart", "kind": "quantity", "sku": "milk", "quantity": -1},
    {"target_id": "cart", "kind": "quantity", "sku": "milk", "quantity": 51},
    {"target_id": "cart", "kind": "quantity", "sku": "milk", "quantity": True},
    {"kind": "quantity", "sku": "milk", "quantity": 2},
    {"target_id": "cart", "kind": "checkout", "confirm": False},
    {"target_id": "cart", "kind": "checkout"},
    {"target_id": "cart", "kind": "quantity", "sku": "milk", "quantity": 2, "account_key": "default"},
])
def test_actions_validate_target_confirmation_and_absolute_quantity(shared_api, body):
    api, fake, _, _, _ = shared_api
    _login(api)
    assert api.post("/api/ocado-shared/actions", json=body).status_code == 422
    assert "quantities" not in fake.calls and "checkout" not in fake.calls


def test_global_debounce_last_quantity_and_slot_win_at_exact_boundary(setup_shared):
    factory, people, owner_id = setup_shared
    store = QueueStore(factory)
    store.enqueue("cart", "cart", _quantity(quantity=3), people[0], now=NOW)
    store.enqueue("cart", "cart", {"kind": "slot", "slot_id": "first"}, people[0], now=NOW + timedelta(seconds=5))
    store.enqueue("cart", "cart", _quantity(quantity=5), people[1], now=NOW + timedelta(seconds=30))
    final = store.enqueue("cart", "cart", {"kind": "slot", "slot_id": "last"}, people[1], now=NOW + timedelta(seconds=60))
    assert final["due_at"] == (NOW + timedelta(seconds=240)).isoformat()
    assert len(final["actions"]) == 2
    assert {a["user_id"] for a in final["actions"]} == {people[1]}
    assert next(a for a in final["actions"] if a["kind"] == "quantity")["quantity"] == 5
    assert next(a for a in final["actions"] if a["kind"] == "slot")["slot_id"] == "last"
    assert store.claim(now=NOW + timedelta(seconds=239, microseconds=999999)) is None
    assert store.claim(now=NOW + timedelta(seconds=240)) is not None
    with factory() as db:
        assert db.scalar(select(OcadoSharedQueue.user_id)) == owner_id


def test_atomic_claim_has_only_one_winner_and_does_not_accept_new_actions(setup_shared):
    factory, people, _ = setup_shared
    store = QueueStore(factory)
    store.enqueue("cart", "cart", _quantity(), people[0], now=NOW)
    gate = threading.Barrier(2)
    claims = []

    def claim():
        gate.wait()
        claims.append(QueueStore(factory).claim(now=NOW + timedelta(seconds=180)))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(batch is not None for batch in claims) == 1
    with pytest.raises(shared.SharedError, match="running"):
        store.enqueue("cart", "cart", _quantity(), people[1], now=NOW + timedelta(seconds=180))
    with pytest.raises(shared.SharedError, match="executing"):
        store.cancel()


def test_claim_cannot_win_after_other_user_extends_timer(setup_shared):
    factory, people, _ = setup_shared
    first, second = QueueStore(factory), QueueStore(factory)
    first.enqueue("cart", "cart", _quantity(), people[0], now=NOW)
    second.enqueue("cart", "cart", _quantity(quantity=4), people[1], now=NOW + timedelta(seconds=179))
    assert first.claim(now=NOW + timedelta(seconds=180)) is None
    batch = first.claim(now=NOW + timedelta(seconds=359))
    assert batch["actions"][0]["quantity"] == 4


def test_restart_keeps_pending_but_never_replays_orphaned_running_batch(setup_shared):
    factory, people, _ = setup_shared
    store = QueueStore(factory)
    store.enqueue("cart", "cart", _quantity(), people[0], now=NOW)
    assert Worker(factory).step(now=NOW + timedelta(seconds=179)) is False
    batch = store.claim(now=NOW + timedelta(seconds=180))
    assert batch
    constructed = []
    worker = Worker(factory, client_factory=lambda key: constructed.append(key))
    assert worker.step(now=NOW + timedelta(seconds=181)) is True
    failed = store.snapshot()
    assert failed["status"] == "failed" and failed["actions"]
    assert "may have completed" in failed["error"]
    assert constructed == []
    assert worker.step(now=NOW + timedelta(days=1)) is False
    with pytest.raises(shared.SharedError, match="cancel"):
        store.enqueue("cart", "cart", _quantity(), people[1], now=NOW)
    assert store.cancel()["status"] == "idle"
    assert store.enqueue("cart", "cart", _quantity(), people[1], now=NOW)["status"] == "pending"


def test_active_executor_blocks_another_process_worker_and_mutating_requests(shared_api, factory):
    api, fake, _, _, _ = shared_api
    _login(api)
    api.post("/api/ocado-shared/actions", json={"target_id": "cart", **_quantity()})
    due = datetime.fromisoformat(QueueStore(factory).snapshot()["due_at"])
    entered, release = threading.Event(), threading.Event()
    original = fake.set_quantities

    def pause(quantities):
        entered.set()
        assert release.wait(5)
        return original(quantities)

    fake.set_quantities = pause
    worker = Worker(factory, client_factory=lambda key: fake)
    thread = threading.Thread(target=lambda: worker.step(now=due))
    thread.start()
    try:
        assert entered.wait(5)
        assert api.get("/api/ocado-shared/status").json()["status"] == "ready"
        assert api.get("/api/ocado-shared/queue").json()["status"] == "running"
        assert Worker(factory, client_factory=lambda key: pytest.fail("double execution")).step(now=due) is False
        assert api.post("/api/ocado-shared/actions", json={"target_id": "cart", **_quantity()}).status_code == 409
        assert api.post("/api/ocado-shared/queue/cancel", json={}).status_code == 409
    finally:
        release.set()
        thread.join()
    assert QueueStore(factory).snapshot()["status"] == "succeeded"


def test_cart_quantities_then_slot_then_real_saved_card_checkout(setup_shared):
    factory, people, _ = setup_shared
    fake, store = FakeSharedClient(), QueueStore(factory)
    # Enqueue checkout first: execution order is semantic, not insertion order.
    store.enqueue("cart", "cart", {"kind": "checkout"}, people[0], now=NOW)
    store.enqueue("cart", "cart", {"kind": "slot", "slot_id": "new-slot"}, people[1], now=NOW)
    store.enqueue("cart", "cart", _quantity(quantity=4), people[1], now=NOW)
    assert Worker(factory, client_factory=lambda key: fake).step(now=NOW + timedelta(seconds=180))
    assert store.snapshot()["status"] == "succeeded"
    assert fake.calls.index("quantities") < fake.calls.index("reserve") < fake.calls.index("checkout_start") < fake.calls.index("checkout")
    assert fake.calls.count("checkout") == 1
    assert fake.checkout_body["paymentMethod"] == {"group": "PAY_NOW", "type": "CARDS", "toSave": False, "walletItemId": "card"}
    assert fake.checkout_body["billingAddressId"] == "billing"
    assert fake.saved_order == "new-order"
    assert fake.quantities["milk"] == 4


def test_booked_changes_use_one_existing_order_amend_and_recheckout(setup_shared):
    factory, people, _ = setup_shared
    fake, store = FakeSharedClient(), QueueStore(factory)
    fake.coming = [booked()]
    for action in [{"kind": "checkout"}, _quantity(quantity=4), {"kind": "slot", "slot_id": "new-slot"}]:
        store.enqueue("order", "order", action, people[0], now=NOW)
    Worker(factory, client_factory=lambda key: fake).step(now=NOW + timedelta(seconds=180))
    assert store.snapshot()["status"] == "succeeded"
    assert fake.calls.count("start_edit") == 1 and fake.calls.count("checkout") == 1
    assert fake.saved_order == "order"
    assert fake.coming[0]["slotId"] == "new-slot"


@pytest.mark.parametrize("failure", ["challenge", "uncertain", "restrictions", "multiple-cards"])
def test_checkout_failure_is_retained_visible_and_never_replayed(setup_shared, failure):
    factory, people, _ = setup_shared
    fake, store = FakeSharedClient(), QueueStore(factory)
    if failure == "challenge":
        fake.checkout_type = "payment-session-response"
    elif failure == "uncertain":
        fake.fail_checkout = True
    elif failure == "restrictions":
        fake.restrictions = ["MISSING_SLOT"]
    else:
        fake.card_items.append({"walletItemId": "other", "paymentMethodType": "CARDS"})
    store.enqueue("cart", "cart", {"kind": "checkout"}, people[0], now=NOW)
    worker = Worker(factory, client_factory=lambda key: fake)
    worker.step(now=NOW + timedelta(seconds=180))
    queue = store.snapshot()
    assert queue["status"] == "failed" and queue["error"] and queue["actions"]
    calls = list(fake.calls)
    assert worker.step(now=NOW + timedelta(days=1)) is False
    assert fake.calls == calls
    if failure in {"restrictions", "multiple-cards"}:
        assert "checkout" not in fake.calls


def test_vanished_pinned_delivery_fails_without_touching_replacement_cart(setup_shared):
    factory, people, _ = setup_shared
    fake, store = FakeSharedClient(), QueueStore(factory)
    store.enqueue("vanished", "vanished", _quantity(), people[0], now=NOW)
    fake.coming = [booked("replacement")]
    Worker(factory, client_factory=lambda key: fake).step(now=NOW + timedelta(seconds=180))
    assert store.snapshot()["status"] == "failed"
    assert "no longer available" in store.snapshot()["error"]
    assert fake.calls == []


def test_queue_survives_database_reopen(setup_shared):
    from app.db.session import make_engine, make_session_factory

    factory, people, _ = setup_shared
    QueueStore(factory).enqueue("cart", "cart", _quantity(quantity=6), people[1], now=NOW)
    second_factory = make_session_factory(make_engine(Path(factory.kw["bind"].url.database)))
    assert QueueStore(second_factory).snapshot()["actions"][0]["quantity"] == 6


def test_older_queue_snapshot_cannot_overwrite_a_new_generation(setup_shared):
    factory, people, _ = setup_shared
    store = QueueStore(factory)
    store.enqueue("cart", "cart", _quantity(), people[0], now=NOW)
    with factory() as stale:
        old = store._row(stale)
        store.enqueue("cart", "cart", _quantity(quantity=7), people[1], now=NOW + timedelta(seconds=1))
        with pytest.raises(shared.SharedError, match="changed"):
            store._cas(stale, old, status="running")
    assert store.snapshot()["actions"][0]["quantity"] == 7


def test_other_process_holding_upstream_lock_prevents_claim(setup_shared):
    import multiprocessing

    factory, people, _ = setup_shared
    store = QueueStore(factory)
    store.enqueue("cart", "cart", _quantity(), people[0], now=NOW)
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()

    def hold_lock():
        with shared.account_lock():
            entered.set()
            release.wait(10)

    process = context.Process(target=hold_lock)
    process.start()
    try:
        assert entered.wait(5)
        worker = Worker(factory, client_factory=lambda key: pytest.fail("executed while another process owns the account"))
        assert worker.step(now=NOW + timedelta(seconds=180)) is False
        assert store.snapshot()["status"] == "pending"
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0


def test_queue_polling_never_reads_upstream(shared_api):
    api, fake, _, _, _ = shared_api
    _login(api)
    api.get("/api/ocado-shared/state")
    before = list(fake.calls)
    assert api.get("/api/ocado-shared/queue").json()["target_id"] == "cart"
    assert fake.calls == before


def test_slot_fetch_failure_still_closes_booked_order_edit(shared_api):
    api, fake, _, _, _ = shared_api
    _login(api)
    fake.coming = [booked()]

    def unavailable(**kwargs):
        raise RuntimeError("Slots unavailable")

    fake.slots = unavailable
    assert api.get("/api/ocado-shared/slots").status_code == 502
    assert fake.calls[-2:] == ["start_edit", "cancel_edit"]
    assert fake.editing is None


def test_new_booking_after_cart_selection_does_not_mutate_active_cart(setup_shared):
    factory, people, _ = setup_shared
    fake, store = FakeSharedClient(), QueueStore(factory)
    store.enqueue("cart", "cart", _quantity(), people[0], now=NOW)
    fake.coming = [booked("externally-booked")]
    Worker(factory, client_factory=lambda key: fake).step(now=NOW + timedelta(seconds=180))
    assert store.snapshot()["status"] == "failed"
    assert "booked since" in store.snapshot()["error"]
    assert fake.calls == []


def test_unverified_checkout_never_reports_success(setup_shared):
    factory, people, _ = setup_shared
    fake, store = FakeSharedClient(), QueueStore(factory)
    fake.get_order = lambda order_id: {}
    store.enqueue("cart", "cart", {"kind": "checkout"}, people[0], now=NOW)
    Worker(factory, client_factory=lambda key: fake).step(now=NOW + timedelta(seconds=180))
    assert fake.calls.count("checkout") == 1
    assert store.snapshot()["status"] == "failed"
    assert "could not be verified" in store.snapshot()["error"]


def test_shared_queue_migration_accepts_durable_actions_on_existing_database(setup_shared):
    from sqlalchemy import text
    from app.db.session import init_db

    factory, people, _ = setup_shared
    engine = factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE ocado_shared_queue"))
        connection.execute(text("UPDATE alembic_version SET version_num = '0017_purchases'"))
    init_db(engine)
    store = QueueStore(factory)
    accepted = store.enqueue("cart", "cart", _quantity(quantity=8), people[1], now=NOW)
    assert QueueStore(factory).snapshot() == accepted
    assert accepted["actions"][0]["user_id"] == people[1]


def test_planner_push_lock_prevents_shared_execution(setup_shared):
    from app.api.cart import _push_lock

    factory, people, _ = setup_shared
    store = QueueStore(factory)
    store.enqueue("cart", "cart", _quantity(), people[0], now=NOW)
    worker = Worker(factory, client_factory=lambda key: pytest.fail("overlapped planner push"))
    with _push_lock("ocado"):
        assert worker.step(now=NOW + timedelta(seconds=180)) is False
    assert store.snapshot()["status"] == "pending"


def test_shared_basket_uses_ocado_without_changing_personal_retailer(shared_api, factory):
    api, _, _, current, _ = shared_api
    _login(api)
    with factory() as db:
        db.add(PlanSettings(user_id=current[0], retailer="sainsburys", anchor_week_start="2030-01-07"))
        db.add_all([
            Product(retailer="ocado", sku="milk", name="Ocado whole milk"),
            Product(retailer="sainsburys", sku="milk", name="Different shop's milk"),
        ])
        db.commit()
    assert api.get("/api/ocado-shared/state").json()["basket"]["lines"][0]["name"] == "Ocado whole milk"
    with factory() as db:
        assert db.scalar(select(PlanSettings.retailer).where(PlanSettings.user_id == current[0])) == "sainsburys"
