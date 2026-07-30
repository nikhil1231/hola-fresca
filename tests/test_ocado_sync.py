from __future__ import annotations

from app.ocado.sync import push_basket
from app.planner.basket import Basket, BasketLine, Cover, PackChoice
from app.planner.index import Pack


class FakeClient:
    def __init__(self, before, after):
        self.views = [before, after]
        self.deltas = None

    def cart_view(self):
        return self.views.pop(0)

    def apply_quantity(self, deltas):
        self.deltas = deltas
        return {"ok": True}


def _pack(sku, name, *, external=False):
    return Pack(
        sku=sku,
        product_name=name,
        capacity_g=100,
        price=1,
        salvage=0,
        rank=1,
        match_type="exact",
        retailer="manual" if external else "ocado",
    )


def _cover(*choices):
    return Cover(
        choices=choices,
        need_g=100,
        capacity_g=100,
        cost=1,
        leftover_g=0,
        waste_gbp=0,
    )


def test_push_basket_uses_deltas_and_detects_dropped_products():
    basket = Basket(
        lines=[
            BasketLine(
                key="potato",
                name="Potatoes",
                need_g=100,
                cover=_cover(PackChoice(_pack("sku-a", "Potatoes"), 2)),
            ),
            BasketLine(
                key="onion",
                name="Onion",
                need_g=100,
                cover=_cover(PackChoice(_pack("sku-b", "Onion"), 1)),
            ),
            BasketLine(
                key="spice",
                name="Spice",
                need_g=100,
                cover=_cover(PackChoice(_pack("manual-1", "Spice", external=True), 1)),
            ),
        ],
        unmapped=["mystery"],
    )
    client = FakeClient(
        {"items": [{"sku": "sku-a", "quantity": 1}, {"sku": "stale", "quantity": 3}]},
        {"items": [{"sku": "sku-a", "quantity": 2}]},
    )

    result = push_basket(client, basket)

    assert client.deltas == {"sku-a": 1, "sku-b": 1, "stale": -3}
    assert [(line.sku, line.quantity) for line in result.applied] == [("sku-a", 2)]
    assert [(line.sku, line.quantity) for line in result.dropped] == [("sku-b", 1)]
    assert result.unmapped == ["mystery"]


def test_push_basket_skips_owned_items():
    basket = Basket(
        lines=[
            BasketLine(
                key="potato",
                name="Potatoes",
                need_g=100,
                cover=_cover(PackChoice(_pack("sku-a", "Potatoes"), 2)),
            ),
            BasketLine(
                key="onion",
                name="Onion",
                need_g=100,
                cover=_cover(PackChoice(_pack("sku-b", "Onion"), 1)),
            ),
        ],
    )
    client = FakeClient(
        {"items": [{"sku": "sku-b", "quantity": 1}]},
        {"items": [{"sku": "sku-a", "quantity": 2}]},
    )

    result = push_basket(client, basket, owned_item_keys={"onion"})

    assert client.deltas == {"sku-a": 2, "sku-b": -1}
    assert [(line.sku, line.quantity) for line in result.applied] == [("sku-a", 2)]
    assert result.dropped == []
