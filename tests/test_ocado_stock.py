"""Checking stock, and pushing around what turns out to be sold out.

The point of these is the loop rather than any one endpoint: a live stock read
writes to the catalogue, the catalogue is what the planner covers from, so
refreshing stock is what moves the basket onto a different pack. Nothing here
fakes the planner - only the shop.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.cart import get_cart_adapter
from app.api.deps import get_planner_csv_path, get_session, get_session_factory
from app.api.planner import _reserve_price_refresh, _stock_checked_at
from app.db.models import Product, Recipe, RecipeIngredient, User
from app.db.session import init_db, make_engine, make_session_factory
from app.mapping import service
from app import config
from app.cart.adapters import AccountInfo, OcadoAdapter
from app.mapping.candidates import gather_candidates
from app.ocado import availability
from app.ocado.availability import ProductStatus
from app.planner.basket import Basket, BasketLine, Cover, PackChoice
from app.planner.cache import get_index
from app.planner.index import Pack
from main import app
from tests.conftest import seed_candidates
from tests.test_planner_basket import write_freq_csv

KEY_RICE = "name:rice"
SID_RICE = "sid-rice"
CHEAP = "11111111-1111-1111-1111-111111111111"
DEARER = "22222222-2222-2222-2222-222222222222"


class FakeCart:
    """A cart that takes every delta except for the SKUs it refuses."""

    def __init__(self, *, refuses=()):
        self.refuses = set(refuses)
        self.quantities: dict[str, int] = {}
        self.applied: list[dict[str, int]] = []

    def cart_view(self):
        return {
            "checkoutGroups": {
                "assignedCheckoutGroups": [
                    {
                        "itemGroups": [
                            {
                                "items": [
                                    {"productId": sku, "quantity": qty}
                                    for sku, qty in self.quantities.items()
                                ]
                            }
                        ]
                    }
                ]
            }
        }

    def apply_quantity(self, deltas):
        self.applied.append(dict(deltas))
        unavailable = []
        for sku, delta in deltas.items():
            if sku in self.refuses:
                unavailable.append({"productId": sku, "reason": "OUT_OF_STOCK"})
                continue
            self.quantities[sku] = max(0, self.quantities.get(sku, 0) + delta)
            if not self.quantities[sku]:
                self.quantities.pop(sku)
        return {"unavailableData": unavailable}


class FakeOcadoAdapter(OcadoAdapter):
    """The real Ocado adapter with a fake cart underneath it.

    Subclassed rather than reimplemented on purpose: everything the adapter does
    between the router and the cart - the delta merge, the ledger, the stock
    refresh - is exactly what these tests are for, so only the socket the
    network would be on gets replaced.
    """

    def __init__(self, cart):
        self.cart_client = cart

    def accounts(self):
        # The configured default, not a made-up id: the ledger is keyed by it,
        # so inventing one here would file the push's claims where nothing
        # reads them and every assertion about the ledger would see nothing.
        return [AccountInfo(id=config.DEFAULT_OCADO_ACCOUNT_ID, label="Ocado")]

    def _runtime(self, account_id=None):
        return SimpleNamespace(
            session=None,
            account=SimpleNamespace(id=config.DEFAULT_OCADO_ACCOUNT_ID),
        )

    def _client(self, account_id=None):
        return self.cart_client


@pytest.fixture
def stock_client(tmp_path):
    """One recipe wanting rice, with two approved packs to choose between."""
    engine = make_engine(tmp_path / "ocado-stock.db")
    init_db(engine)
    factory = make_session_factory(engine)
    csv_path = write_freq_csv(tmp_path / "ingredient_frequency.csv", [(KEY_RICE, SID_RICE, "Rice")])

    with factory() as s:
        seed_candidates(
            s,
            KEY_RICE,
            "Rice",
            [
                {"sku": CHEAP, "name": "Value Rice 500g", "price": 1.0,
                 "pack_value": 500, "pack_unit": "g"},
                {"sku": DEARER, "name": "Fancy Rice 500g", "price": 1.6,
                 "pack_value": 500, "pack_unit": "g"},
            ],
        )
        service.save_decision(
            s,
            gather_candidates(s, KEY_RICE),
            service.DecisionInput(
                status="approved",
                accepted=[
                    service.AcceptedInput(sku=CHEAP, rank=1),
                    service.AcceptedInput(sku=DEARER, rank=2),
                ],
            ),
        )
        recipe = Recipe(
            source="hellofresh",
            source_id="rice-bowl",
            url="https://example.com/recipe",
            name="Rice Bowl",
            curated=1,
            is_complete=1,
            base_yield=2,
            ingredients=[
                RecipeIngredient(
                    name="Rice", source_ingredient_id=SID_RICE, amount=300, unit="g", amount_g=300
                )
            ],
        )
        s.add(recipe)
        s.commit()
        recipe_id = recipe.id

    def _override_session():
        with factory() as session:
            yield session

    cart = FakeCart()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_planner_csv_path] = lambda: csv_path
    app.dependency_overrides[get_cart_adapter] = lambda: FakeOcadoAdapter(cart)
    yield TestClient(app), recipe_id, cart
    app.dependency_overrides.clear()


def _selections(recipe_id):
    return {"selections": [{"recipe_id": recipe_id, "portions": 2}]}


def _pack_line(name, *, stock_checked_at):
    pack = Pack(
        sku=name.lower(), product_name=name, capacity_g=100, price=1.0, salvage=0.0,
        rank=1, match_type="exact", stock_checked_at=stock_checked_at,
    )
    return BasketLine(
        key=f"name:{name.lower()}",
        name=name,
        need_g=100,
        cover=Cover(
            choices=(PackChoice(pack, 1),), need_g=100, capacity_g=100, cost=1.0,
            leftover_g=0, waste_gbp=0,
        ),
    )


def _shelves(monkeypatch, sold_out=()):
    """Stand in for the live products endpoint."""
    sold_out = set(sold_out)

    def fake(skus, session=None):
        return {
            sku: ProductStatus(sku=sku, available=sku not in sold_out, price=None)
            for sku in skus
        }

    monkeypatch.setattr(availability, "fetch_statuses", fake)


def test_a_basket_starts_on_the_cheapest_pack(stock_client):
    client, recipe_id, _ = stock_client
    data = client.post("/api/planner/basket", json=_selections(recipe_id)).json()

    assert data["lines"][0]["choices"][0]["sku"] == CHEAP
    assert data["lines"][0]["substitution"] is None
    assert data["stock_checked_at"] is None, "nothing has been checked live yet"


def test_nectar_identity_reaches_the_chosen_pack_and_line(stock_client):
    client, recipe_id, _ = stock_client
    factory = app.dependency_overrides[get_session_factory]()
    with factory() as session:
        product = session.scalar(select(Product).where(Product.sku == CHEAP))
        product.is_nectar_price = True
        session.commit()

    line = client.post("/api/planner/basket", json=_selections(recipe_id)).json()["lines"][0]

    assert line["cost"] == 1.0
    assert line["is_nectar_price"] is True
    assert line["choices"][0]["is_nectar_price"] is True


def test_price_refresh_is_debounced_before_a_second_retailer_call(stock_client, monkeypatch):
    client, recipe_id, _ = stock_client
    calls = []

    def shelves(skus, session=None):
        calls.append(list(skus))
        return {sku: ProductStatus(sku=sku, available=True) for sku in skus}

    monkeypatch.setattr(availability, "fetch_statuses", shelves)
    first = client.post("/api/planner/stock/refresh", json=_selections(recipe_id)).json()
    second = client.post("/api/planner/stock/refresh", json=_selections(recipe_id)).json()

    assert first["performed"] is True
    assert second["performed"] is False
    assert second["next_refresh_at"] > second["checked_at"]
    assert len(calls) == 1


def test_a_failed_price_refresh_releases_its_cooldown(stock_client, monkeypatch):
    client, recipe_id, _ = stock_client

    def unavailable(skus, session=None):
        raise RuntimeError("shop is down")

    monkeypatch.setattr(availability, "fetch_statuses", unavailable)
    assert client.post(
        "/api/planner/stock/refresh", json=_selections(recipe_id)
    ).status_code == 502

    _shelves(monkeypatch)
    retry = client.post("/api/planner/stock/refresh", json=_selections(recipe_id)).json()
    assert retry["performed"] is True


def test_refresh_cooldowns_are_atomic_and_scoped_by_user_and_retailer(stock_client):
    _, _, _ = stock_client
    factory = app.dependency_overrides[get_session_factory]()
    now = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    with factory() as session:
        mine = session.scalar(select(User.id).order_by(User.id).limit(1))
        other = User(name="Someone Else")
        session.add(other)
        session.commit()
        other_id = other.id

    outcomes = []
    failures = []
    start = threading.Barrier(2)

    def reserve() -> None:
        try:
            with factory() as session:
                start.wait()
                outcomes.append(_reserve_price_refresh(session, mine, "ocado", now)[0])
        except BaseException as exc:  # noqa: BLE001 - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    assert sorted(outcomes) == [False, True]
    with factory() as session:
        assert _reserve_price_refresh(session, mine, "sainsburys", now)[0] is True
    with factory() as session:
        assert _reserve_price_refresh(session, other_id, "ocado", now)[0] is True


def test_refreshing_stock_moves_the_basket_onto_what_is_in_stock(stock_client, monkeypatch):
    client, recipe_id, _ = stock_client
    _shelves(monkeypatch, sold_out=[CHEAP])

    refresh = client.post("/api/planner/stock/refresh", json=_selections(recipe_id)).json()
    assert refresh["checked"] == 2 and refresh["sold_out"] == [CHEAP]

    line = client.post("/api/planner/basket", json=_selections(recipe_id)).json()["lines"][0]
    assert line["choices"][0]["sku"] == DEARER, "the substitute the mapping already held"
    assert line["substitution"]["displaced"] == ["Value Rice 500g"]
    assert line["substitution"]["cost_delta"] == 0.6
    assert line["substitution"]["tier_changed"] is False


def test_the_basket_reports_how_fresh_its_stock_is(stock_client, monkeypatch):
    client, recipe_id, _ = stock_client
    _shelves(monkeypatch)

    client.post("/api/planner/stock/refresh", json=_selections(recipe_id))
    checked_at = client.post("/api/planner/basket", json=_selections(recipe_id)).json()[
        "stock_checked_at"
    ]

    # Naive, a browser reads this as local time and a fresh check looks hours old.
    assert checked_at is not None
    assert checked_at.endswith("Z") or "+00:00" in checked_at


def test_one_unchecked_pack_makes_the_whole_basket_unchecked():
    """There is no honest freshness to claim for a basket half of which is stale."""
    when = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    checked = _pack_line("Rice", stock_checked_at=when)
    older = _pack_line("Beans", stock_checked_at=datetime(2026, 7, 1, 12, tzinfo=timezone.utc))
    never = _pack_line("Salt", stock_checked_at=None)

    assert _stock_checked_at(Basket(lines=[checked, older])) == older.cover.choices[
        0
    ].pack.stock_checked_at
    assert _stock_checked_at(Basket(lines=[checked, never])) is None


def test_an_ingredient_with_nothing_in_stock_is_reported_apart(stock_client, monkeypatch):
    client, recipe_id, _ = stock_client
    _shelves(monkeypatch, sold_out=[CHEAP, DEARER])

    client.post("/api/planner/stock/refresh", json=_selections(recipe_id))
    data = client.post("/api/planner/basket", json=_selections(recipe_id)).json()

    assert data["sold_out"] == ["Rice"]
    assert data["unpriceable"] == [], "a shelf problem, not a mapping problem"
    assert data["lines"] == []


def test_a_pinned_pack_survives_the_round_trip_through_the_catalogue(stock_client):
    """Not about stock, but the same loop: a decision written to the mapping has
    to come back out through the index and change what the basket buys."""
    client, recipe_id, _ = stock_client

    client.put("/api/planner/preferences/pack", json={"ingredient_key": KEY_RICE, "sku": DEARER})
    line = client.post("/api/planner/basket", json=_selections(recipe_id)).json()["lines"][0]

    assert line["choices"][0]["sku"] == DEARER, "bought as asked, not as costed"
    assert [o["pinned"] for o in line["options"] if o["sku"] == DEARER] == [True]

    client.put("/api/planner/preferences/pack", json={"ingredient_key": KEY_RICE, "sku": None})
    line = client.post("/api/planner/basket", json=_selections(recipe_id)).json()["lines"][0]
    assert line["choices"][0]["sku"] == CHEAP


def test_a_pack_chosen_for_this_week_costs_no_write_and_reaches_the_push(stock_client, monkeypatch):
    """The whole point of the week scope: instant, and gone by the next shop."""
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    body = {**_selections(recipe_id), "pack_overrides": {KEY_RICE: DEARER}}

    line = client.post("/api/planner/basket", json=body).json()["lines"][0]
    assert line["choices"][0]["sku"] == DEARER
    assert [o["this_week"] for o in line["options"] if o["sku"] == DEARER] == [True]
    assert [o["pinned"] for o in line["options"]] == [False, False], "nothing was written"

    client.post("/api/cart/ocado/basket/push", json=body)
    assert cart.quantities == {DEARER: 1}, "pushes what the page showed"

    # And the next week, unasked, is back to the planner's own choice.
    plain = client.post("/api/planner/basket", json=_selections(recipe_id)).json()["lines"][0]
    assert plain["choices"][0]["sku"] == CHEAP


def test_setting_a_preference_does_not_throw_the_cached_index_away(stock_client):
    """One click used to cost a full index rebuild - seconds - for a change that
    touches a single ingredient.

    The preference is not in the index at all now that it belongs to a user
    rather than to the ingredient, so the index survives untouched; what the
    write must not do is move the database's mtime under the staleness check and
    trigger a rebuild anyway.
    """
    client, recipe_id, _ = stock_client
    factory = app.dependency_overrides[get_session_factory]()
    before = get_index(factory)

    client.put("/api/planner/preferences/pack", json={"ingredient_key": KEY_RICE, "sku": DEARER})

    assert get_index(factory) is before, "kept, not rebuilt"
    line = client.post("/api/planner/basket", json=_selections(recipe_id)).json()["lines"][0]
    assert line["choices"][0]["sku"] == DEARER
    assert [o["pinned"] for o in line["options"] if o["sku"] == DEARER] == [True]


def test_a_push_checks_the_shelves_before_filling_the_trolley(stock_client, monkeypatch):
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch, sold_out=[CHEAP])

    result = client.post("/api/cart/ocado/basket/push", json=_selections(recipe_id)).json()

    assert [line["sku"] for line in result["applied"]] == [DEARER]
    assert result["dropped"] == []
    assert cart.quantities == {DEARER: 1}, "the sold-out pack was never even offered"
    (swap,) = result["swaps"]
    assert swap["ingredient"] == "Rice"
    assert swap["from_products"] == ["Value Rice 500g"]
    assert swap["to_products"] == ["Fancy Rice 500g"]
    assert swap["cost_delta"] == 0.6


def test_a_refusal_at_the_till_is_recovered_from_and_remembered(stock_client, monkeypatch):
    """Stock said yes, the cart said no. The cart wins, and the week is re-covered."""
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    cart.refuses = {CHEAP}

    result = client.post("/api/cart/ocado/basket/push", json=_selections(recipe_id)).json()

    assert [line["sku"] for line in result["applied"]] == [DEARER]
    assert result["dropped"] == []
    assert cart.quantities == {DEARER: 1}
    # And the catalogue has learnt it, so the next basket never offers it again.
    data = client.post("/api/planner/basket", json=_selections(recipe_id)).json()
    assert data["lines"][0]["choices"][0]["sku"] == DEARER


def test_a_drop_that_cannot_be_recovered_names_its_ingredient(stock_client, monkeypatch):
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    cart.refuses = {CHEAP, DEARER}

    result = client.post("/api/cart/ocado/basket/push", json=_selections(recipe_id)).json()

    (dropped,) = result["dropped"]
    assert dropped["ingredient"] == "Rice", "not just the brand name of the pack"
    assert (dropped["wanted"], dropped["got"]) == (1, 0)
    assert dropped["reason"] == "out of stock"


def test_ocado_being_unreachable_does_not_block_the_push(stock_client, monkeypatch):
    """A shop that cannot be asked is a reason to trust the cache, not to refuse."""
    client, recipe_id, _ = stock_client

    def boom(skus, session=None):
        raise RuntimeError("ocado is down")

    monkeypatch.setattr(availability, "fetch_statuses", boom)

    result = client.post("/api/cart/ocado/basket/push", json=_selections(recipe_id)).json()

    assert [line["sku"] for line in result["applied"]] == [CHEAP]
