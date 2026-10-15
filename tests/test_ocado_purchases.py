"""The purchase history: read gently from delivered orders, kept, and asked by the matcher."""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import Product, ProductDescription, Purchase, RetailerAccount
from app.ocado import purchases, top_up
from tests.conftest import seed_candidates, user_id

JASONS = "11111111-1111-1111-1111-111111111111"
WARBURTONS = "22222222-2222-2222-2222-222222222222"
UNKNOWN = "33333333-3333-3333-3333-333333333333"


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.content = payload, status, b"x"

    def json(self):
        return self.payload


class Session:
    def __init__(self):
        self.calls = []

    def request(self, method, path, json=None, **kwargs):
        self.calls.append((method, path, json))
        return Response([{"productId": sku, "name": "Mystery Oat Bar", "price": {"current": {"amount": 1.0}},
                          "imageUrl": "/img/m.jpg"} for sku in json])


class History:
    """An account's orders: ``{order_id: (status, day, {sku: quantity})}``."""

    def __init__(self, orders):
        self._orders, self.read, self.session = orders, [], Session()

    def orders(self, pending=False):
        return [{"orderId": oid, "status": status, "dates": {"deliveryStartDate": f"{day}T10:00:00+01:00"}}
                for oid, (status, day, _) in self._orders.items()]

    def get_order(self, order_id):
        self.read.append(order_id)
        return {"items": [{"product": sku, "quantity": q} for sku, q in self._orders[order_id][2].items()]}


def _account(s):
    """The test database's own Ocado account (init_db makes one)."""
    return s.scalar(select(RetailerAccount).where(RetailerAccount.retailer == "ocado"))


def test_delivered_orders_are_read_once_oldest_first_with_a_pause_between(factory):
    client = History({"o3": ("DELIVERED", "2026-10-05", {JASONS: 1, WARBURTONS: 2}),
                      "o1": ("DELIVERED", "2026-04-14", {JASONS: 1}),
                      "o2": ("CANCELLED", "2026-05-01", {JASONS: 9}),
                      "o4": ("PLACED", "2026-10-20", {JASONS: 1})})
    pauses = []
    with factory() as s:
        account = _account(s)
        assert purchases.read_orders(s, account, client, pause=lambda: pauses.append(1)) == 2
        assert client.read == ["o1", "o3"] and len(pauses) == 1
        assert purchases.read_orders(s, account, client, pause=lambda: pauses.append(1)) == 0  # nothing new
        rows = {(p.order_id, p.sku): (p.quantity, p.delivered_on) for p in s.scalars(select(Purchase))}
        assert rows == {("o1", JASONS): (1, "2026-04-14"), ("o3", JASONS): (1, "2026-10-05"),
                        ("o3", WARBURTONS): (2, "2026-10-05")}
        b = purchases.bought(s, [user_id(s)])
        assert (b[JASONS].orders, b[JASONS].first, b[JASONS].last) == (2, "2026-04-14", "2026-10-05")
        assert (b[WARBURTONS].orders, b[WARBURTONS].quantity) == (1, 2)
        assert purchases.order_count(s, [user_id(s)]) == 2


def test_a_run_reads_a_limited_number_of_orders(factory):
    client = History({f"o{i}": ("DELIVERED", f"2026-05-{i:02d}", {JASONS: 1}) for i in range(1, 6)})
    with factory() as s:
        account = _account(s)
        assert purchases.read_orders(s, account, client, pause=lambda: None, limit=3) == 3
        assert purchases.read_orders(s, account, client, pause=lambda: None, limit=3) == 2


def test_bought_products_missing_from_the_catalogue_are_filled_in_one_batch(factory):
    client = History({"o1": ("DELIVERED", "2026-04-14", {JASONS: 1, UNKNOWN: 1, "manual:x": 1})})
    with factory() as s:
        seed_candidates(s, "name:bread", "Bread", [{"sku": JASONS, "name": "Jason's Seeded Protein Loaf"}])
        s.commit()
        out = purchases.sync(s, [(_account(s), client)], pause=lambda: None)
        assert out == {"orders": 1, "products": 1, "described": 0}
        assert client.session.calls == [("PUT", "/api/webproductpagews/v6/products", [UNKNOWN])]
        assert s.scalar(select(Product.name).where(Product.sku == UNKNOWN)) == "Mystery Oat Bar"


def test_packaging_is_described_once_per_bought_product(factory):
    seen = []

    def describer(image, name):
        seen.append(name)
        return "green and white bag, Warburtons logo"

    with factory() as s:
        seed_candidates(s, "name:bread", "Bread", [{"sku": WARBURTONS, "name": "Warburtons Protein Power"},
                                                   {"sku": JASONS, "name": "Jason's Seeded Protein Loaf"}])
        for p in s.scalars(select(Product)):
            p.image_url = f"https://img/{p.sku}.jpg"
        s.add(Purchase(user_id=user_id(s), retailer="ocado", account_key="a", order_id="o1", sku=WARBURTONS,
                       quantity=1, delivered_on="2026-04-14"))
        s.commit()
        assert purchases.describe(s, describer) == 1  # only what was bought
        assert purchases.describe(s, describer) == 0
        assert seen == ["Warburtons Protein Power"]
        assert purchases.descriptions(s, [WARBURTONS, JASONS]) == {WARBURTONS: "green and white bag, Warburtons logo"}
        assert s.scalar(select(ProductDescription.packaging)) == "green and white bag, Warburtons logo"


def test_the_matcher_buys_what_the_household_buys_most(factory):
    from app.mapping import service
    from app.mapping.candidates import gather_candidates
    from app.planner.preferences import set_pack_preference

    with factory() as s:
        products = [{"sku": "cheap", "name": "Ocado Oat Drink 1L"}, {"sku": "oatly", "name": "Oatly Barista 1L"}]
        seed_candidates(s, "name:oat milk", "Oat Milk", products)
        service.save_decision(s, gather_candidates(s, "name:oat milk"), service.DecisionInput(
            status="approved", accepted=[service.AcceptedInput(sku=p["sku"], rank=i) for i, p in enumerate(products, 1)]))
        found = top_up.Match("name:oat milk", "Oat Milk")
        me = user_id(s)
        assert top_up.pick(s, found, me, household=[me])[0].sku == "cheap"  # the reviewed rank, with no history
        for i in range(3):
            s.add(Purchase(user_id=me, retailer="ocado", account_key="a", order_id=f"o{i}", sku="oatly", quantity=1,
                           delivered_on="2026-05-01"))
        s.commit()
        assert top_up.pick(s, found, me, household=[me])[0].sku == "oatly"
        set_pack_preference(s, me, "name:oat milk", "cheap")
        s.commit()
        assert top_up.pick(s, found, me, household=[me])[0].sku == "cheap"  # a standing choice still wins
