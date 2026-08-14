"""Re-sorting stored proposals when the ordering rules change.

Storing the model's own ordering is what makes this possible: retuning the
balance between unit price and rating should cost a re-sort, not another pass
over the model.
"""
from __future__ import annotations

from app.mapping import propose as P
from app.mapping import service
from app.mapping.candidates import gather_candidates

from tests.conftest import seed_candidates

# Prices a long way apart, ratings close: a cheapest-first order and a
# best-rated-first order disagree, so a change of weights has something to move.
PRODUCTS = [
    {"sku": "cheap", "name": "Value Chorizo", "price": 1.5, "pack_value": 200, "pack_unit": "g",
     "unit_price": 7.5, "unit_basis": "kg", "rating": 3.9, "count": 120},
    {"sku": "dear", "name": "Deli Chorizo", "price": 4.5, "pack_value": 200, "pack_unit": "g",
     "unit_price": 22.5, "unit_basis": "kg", "rating": 4.6, "count": 300},
]


def _seed(factory):
    with factory() as s:
        seed_candidates(s, "name:chorizo", "Chorizo", PRODUCTS, line_count=40)


def _propose(factory, accepted):
    def fake_complete(system, user, schema):
        return {
            "accepted": accepted,
            "each_to_grams": None,
            "needs_substitution": False,
            "note": "ok",
        }

    return P.run_propose(factory, complete=fake_complete, model="test-model")


def _order(factory):
    with factory() as s:
        detail = service.get_detail(s, gather_candidates(s, "name:chorizo"))
        return [v.candidate.sku for v in detail.candidates if v.accepted]


def test_reorder_applies_new_weights_without_calling_the_model(factory, monkeypatch):
    _seed(factory)
    _propose(factory, [
        {"sku": "dear", "rank": 1, "match_type": "exact", "reason": "better"},
        {"sku": "cheap", "rank": 2, "match_type": "exact", "reason": "cheaper"},
    ])
    # A 3x price spread settles it, so the model's own preference is overturned.
    assert _order(factory) == ["cheap", "dear"]

    # Retune: rating alone decides, and nothing about the stored rows changes.
    monkeypatch.setattr(service.ordering, "WEIGHT_UNIT_PRICE", 0.0)
    monkeypatch.setattr(service.ordering, "WEIGHT_RATING", 1.0)
    monkeypatch.setattr(service.ordering, "WEIGHT_LLM_RANK", 0.0)

    with factory() as s:
        assert service.reorder_proposals(s) == 1
    assert _order(factory) == ["dear", "cheap"]


def test_reorder_is_idempotent_and_reports_nothing_moved(factory):
    _seed(factory)
    _propose(factory, [
        {"sku": "cheap", "rank": 1, "match_type": "exact", "reason": "cheaper"},
        {"sku": "dear", "rank": 2, "match_type": "exact", "reason": "better"},
    ])
    with factory() as s:
        assert service.reorder_proposals(s) == 0
    assert _order(factory) == ["cheap", "dear"]


def test_reorder_moves_the_spend_score_with_the_top_product(factory, monkeypatch):
    _seed(factory)
    _propose(factory, [
        {"sku": "dear", "rank": 1, "match_type": "exact", "reason": "better"},
        {"sku": "cheap", "rank": 2, "match_type": "exact", "reason": "cheaper"},
    ])
    with factory() as s:
        item = next(i for i in service.list_items(s) if i.ingredient_key == "name:chorizo")
        # The worklist takes line_count from the real frequency CSV, not the seed.
        lines = item.line_count
        assert item.spend_score == lines * 1.5

    monkeypatch.setattr(service.ordering, "WEIGHT_UNIT_PRICE", 0.0)
    monkeypatch.setattr(service.ordering, "WEIGHT_RATING", 1.0)
    monkeypatch.setattr(service.ordering, "WEIGHT_LLM_RANK", 0.0)
    with factory() as s:
        service.reorder_proposals(s)
        item = next(i for i in service.list_items(s) if i.ingredient_key == "name:chorizo")
        assert item.spend_score == lines * 4.5


def test_reorder_leaves_a_humans_order_alone(factory, monkeypatch):
    _seed(factory)
    _propose(factory, [
        {"sku": "cheap", "rank": 1, "match_type": "exact", "reason": "cheaper"},
        {"sku": "dear", "rank": 2, "match_type": "exact", "reason": "better"},
    ])
    # The reviewer decides they want the dearer one first and approves it.
    with factory() as s:
        service.save_decision(
            s,
            gather_candidates(s, "name:chorizo"),
            service.DecisionInput(
                status="approved",
                accepted=[
                    service.AcceptedInput(sku="dear", rank=1),
                    service.AcceptedInput(sku="cheap", rank=2),
                ],
            ),
        )

    monkeypatch.setattr(service.ordering, "WEIGHT_UNIT_PRICE", 1.0)
    monkeypatch.setattr(service.ordering, "WEIGHT_RATING", 0.0)
    monkeypatch.setattr(service.ordering, "WEIGHT_LLM_RANK", 0.0)
    with factory() as s:
        assert service.reorder_proposals(s) == 0
    assert _order(factory) == ["dear", "cheap"]


def test_a_re_proposal_does_not_overwrite_an_established_each_to_grams(factory):
    _seed(factory)
    _propose(factory, [{"sku": "cheap", "rank": 1, "match_type": "exact", "reason": "ok"}])

    def fake_complete_with_each(system, user, schema):
        return {
            "accepted": [{"sku": "cheap", "rank": 1, "match_type": "exact", "reason": "ok"}],
            "each_to_grams": 60.0,
            "needs_substitution": False,
            "note": "ok",
        }

    # First value sticks: derive_count_metadata defers to whatever is already
    # there, so a second guess would never be corrected downstream.
    P.run_propose(factory, complete=fake_complete_with_each, model="test-model", force=True)
    with factory() as s:
        item = next(i for i in service.list_items(s) if i.ingredient_key == "name:chorizo")
        assert item.each_to_grams == 60.0

    def fake_complete_rerolled(system, user, schema):
        return {
            "accepted": [{"sku": "cheap", "rank": 1, "match_type": "exact", "reason": "ok"}],
            "each_to_grams": 15.0,
            "needs_substitution": False,
            "note": "ok",
        }

    P.run_propose(factory, complete=fake_complete_rerolled, model="test-model", force=True)
    with factory() as s:
        item = next(i for i in service.list_items(s) if i.ingredient_key == "name:chorizo")
        assert item.each_to_grams == 60.0
