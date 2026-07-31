"""Syncing a week into a cart that is not only ours, end to end.

The cart holds the rest of the week's shopping alongside the recipes, so a sync
is a three-way merge rather than an overwrite: the ledger says what the last one
put in, and anything above that claim is yours. These drive the whole stack -
real planner, real ledger table, only the shop is faked - because the bugs worth
catching here are ones where the ledger and the cart drift apart between calls.

The unit-level rules live in tests/test_ocado_sync.py.
"""
from __future__ import annotations

from app.api.deps import get_session_factory
from app.ocado.ledger import read_ledger
from main import app
from tests.test_ocado_stock import CHEAP, _selections, _shelves, stock_client  # noqa: F401

BEER = "99999999-9999-9999-9999-999999999999"


def _ledger():
    return read_ledger(app.dependency_overrides[get_session_factory]()).quantities


def test_a_first_push_claims_what_it_bought(stock_client, monkeypatch):
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)

    client.post("/api/ocado/basket/push", json=_selections(recipe_id))

    assert cart.quantities == {CHEAP: 1}
    assert _ledger() == {CHEAP: 1}


def test_shopping_you_add_yourself_survives_the_next_sync(stock_client, monkeypatch):
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    client.post("/api/ocado/basket/push", json=_selections(recipe_id))

    cart.quantities[BEER] = 6
    result = client.post("/api/ocado/basket/push", json=_selections(recipe_id)).json()

    assert cart.quantities == {CHEAP: 1, BEER: 6}
    assert [(l["sku"], l["quantity"]) for l in result["yours"]] == [(BEER, 6)]
    assert BEER not in _ledger(), "never claim what it did not buy"


def test_a_cart_you_had_already_started_is_added_to(stock_client, monkeypatch):
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    cart.quantities[BEER] = 6

    client.post("/api/ocado/basket/push", json=_selections(recipe_id))

    assert cart.quantities == {BEER: 6, CHEAP: 1}


def test_deleting_what_hf_bought_puts_it_back(stock_client, monkeypatch):
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    client.post("/api/ocado/basket/push", json=_selections(recipe_id))

    cart.quantities.pop(CHEAP)
    result = client.post("/api/ocado/basket/push", json=_selections(recipe_id)).json()

    assert cart.quantities == {CHEAP: 1}
    assert [(l["sku"], l["quantity"]) for l in result["restored"]] == [(CHEAP, 1)]


def test_buying_more_of_an_hf_item_is_left_alone(stock_client, monkeypatch):
    """You may want four bags of rice. HF only ever puts its own one back."""
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    client.post("/api/ocado/basket/push", json=_selections(recipe_id))

    cart.quantities[CHEAP] = 4
    result = client.post("/api/ocado/basket/push", json=_selections(recipe_id)).json()

    assert cart.quantities == {CHEAP: 4}
    assert [(l["sku"], l["quantity"]) for l in result["yours"]] == [(CHEAP, 3)]
    assert _ledger() == {CHEAP: 1}


def test_dropping_the_recipe_takes_out_only_what_hf_put_in(stock_client, monkeypatch):
    """The tricky one: change the week, and the beer must not go with the rice."""
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    client.post("/api/ocado/basket/push", json=_selections(recipe_id))
    cart.quantities[BEER] = 6

    result = client.post("/api/ocado/basket/push", json={"selections": []}).json()

    assert cart.quantities == {BEER: 6}
    assert [(l["sku"], l["quantity"]) for l in result["removed"]] == [(CHEAP, 1)]
    assert result["removed"][0]["ingredient"] == "Rice", "named by what it was for"
    assert _ledger() == {}


def test_a_partly_owned_product_gives_back_only_hfs_share(stock_client, monkeypatch):
    """Both of you bought the same rice. Dropping the week returns one bag."""
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    client.post("/api/ocado/basket/push", json=_selections(recipe_id))
    cart.quantities[CHEAP] = 3  # your two, on top of HF's one

    client.post("/api/ocado/basket/push", json={"selections": []})

    assert cart.quantities == {CHEAP: 2}
    assert _ledger() == {}


def test_the_plan_says_what_the_push_will_do_without_doing_it(stock_client, monkeypatch):
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    cart.quantities[BEER] = 6

    plan = client.post("/api/ocado/basket/plan", json=_selections(recipe_id)).json()

    assert cart.applied == [], "a plan never touches the cart"
    assert plan["synced"] is False, "nothing has been pushed yet"
    assert [(l["sku"], l["quantity"]) for l in plan["added"]] == [(CHEAP, 1)]
    assert [(l["sku"], l["quantity"]) for l in plan["yours"]] == [(BEER, 6)]
    assert plan["removed"] == []

    body = {**_selections(recipe_id), "week_start": "2026-08-03"}
    client.post("/api/ocado/basket/push", json=body)
    after = client.post("/api/ocado/basket/plan", json=_selections(recipe_id)).json()

    assert after["synced"] is True
    assert after["added"] == [], "the week is already in the cart"
    assert [(l["sku"], l["quantity"]) for l in after["yours"]] == [(BEER, 6)]
    # A plan proposing to empty half the cart reads very differently once you
    # can see which week the claims it is acting on were made for.
    assert after["synced_week_start"] == "2026-08-03"
    assert after["synced_at"] is not None


def test_a_push_names_your_items_from_the_catalogue_where_it_can(stock_client, monkeypatch):
    """Your own items come from no basket line, so nothing upstream names them."""
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    cart.quantities[CHEAP] = 2  # a product HF knows, bought by you

    plan = client.post("/api/ocado/basket/plan", json={"selections": []}).json()

    assert [(l["name"], l["quantity"]) for l in plan["yours"]] == [("Value Rice 500g", 2)]


def test_a_first_sync_adopts_a_cart_left_by_a_push_from_before_the_ledger(
    stock_client, monkeypatch
):
    """The migration case, which is what the ``synced`` flag exists for."""
    client, recipe_id, cart = stock_client
    _shelves(monkeypatch)
    cart.quantities[CHEAP] = 1  # as an older, ledger-less push would have left it

    client.post("/api/ocado/basket/push", json=_selections(recipe_id))

    assert cart.quantities == {CHEAP: 1}, "adopted, not bought a second time"
    assert _ledger() == {CHEAP: 1}
