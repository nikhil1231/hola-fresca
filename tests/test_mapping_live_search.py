"""Live re-search: results merge into the candidate pool without losing existing ones.

The browser is faked — these tests never touch the network.
"""
from __future__ import annotations

from app.mapping import live_search, service
from app.mapping.candidates import gather_candidates

from tests.conftest import seed_candidates

EXISTING = [
    {"sku": "old1", "name": "Knorr Vegetable Stock Pot", "price": 1.95, "pack_value": 112, "pack_unit": "g"},
]


class FakeRunner:
    """Stands in for the Playwright-backed search runner."""

    def __init__(self, products):
        self.products = products
        self.terms = []

    def search(self, term, timeout=None):
        self.terms.append(term)
        return {"products": self.products}


def _payload(sku, name, price):
    return {
        "productId": sku,
        "retailerProductId": sku.replace("new", "9000"),
        "name": name,
        "price": {"amount": str(price), "currency": "GBP"},
        "packSizeDescription": "66g",
    }


def test_search_merges_new_candidates_keeping_existing(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(live_search.storage, "write_raw", lambda *a, **k: None)
    with factory() as s:
        seed_candidates(s, "name:vegetable stock paste", "Vegetable Stock Paste", EXISTING, line_count=120)

    runner = FakeRunner([
        _payload("new1", "Oxo 12 Vegetable Stock Cubes", 2.90),
        _payload("new2", "Kallo Vegetable Stock Cubes", 1.40),
    ])
    with factory() as s:
        added = live_search.search_and_store(s, "name:vegetable stock paste", "vegetable stock", runner=runner)

    assert added == 2
    assert runner.terms == ["vegetable stock"]

    with factory() as s:
        ic = gather_candidates(s, "name:vegetable stock paste")
        skus = {c.sku for c in ic.candidates}
        assert skus == {"old1", "new1", "new2"}  # existing candidate preserved
        by_sku = {c.sku: c for c in ic.candidates}
        # New candidates record the term that found them; the original does not.
        assert by_sku["new1"].search_term == "vegetable stock"
        assert by_sku["old1"].search_term != "vegetable stock"


def test_search_remembers_term_on_the_mapping(factory, monkeypatch):
    monkeypatch.setattr(live_search.storage, "write_raw", lambda *a, **k: None)
    with factory() as s:
        seed_candidates(s, "name:vegetable stock paste", "Vegetable Stock Paste", EXISTING, line_count=120)
        ic = gather_candidates(s, "name:vegetable stock paste")
        service.save_decision(
            s, ic, service.DecisionInput(status="proposed", accepted=[])
        )

    runner = FakeRunner([_payload("new1", "Oxo Vegetable Stock Cubes", 2.90)])
    with factory() as s:
        live_search.search_and_store(s, "name:vegetable stock paste", "vegetable stock", runner=runner)

    with factory() as s:
        detail = service.get_detail(s, gather_candidates(s, "name:vegetable stock paste"))
        assert detail.search_term == "vegetable stock"


def test_empty_term_is_rejected(factory):
    with factory() as s:
        seed_candidates(s, "name:vegetable stock paste", "Vegetable Stock Paste", EXISTING, line_count=120)
        try:
            live_search.search_and_store(s, "name:vegetable stock paste", "   ", runner=FakeRunner([]))
        except ValueError:
            return
    raise AssertionError("expected ValueError for a blank search term")
