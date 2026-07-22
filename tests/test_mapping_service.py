"""Service layer: saving decisions, detail overlay, bulk approve, coverage."""
from __future__ import annotations

from app.mapping import service
from app.mapping.candidates import gather_candidates

from tests.conftest import seed_candidates

PRODUCTS = [
    {"sku": "p1", "name": "Ocado Chicken Breast Fillets", "price": 3.5, "pack_value": 600, "pack_unit": "g"},
    {"sku": "p2", "name": "Chicken Breast Mini Fillets", "price": 2.5, "pack_value": 300, "pack_unit": "g"},
    {"sku": "p3", "name": "Organic Free Range Chicken", "price": 6.0, "pack_value": 400, "pack_unit": "g"},
]


def _seed(factory):
    with factory() as s:
        seed_candidates(s, "name:chicken breast", "Chicken Breast", PRODUCTS, line_count=500)


def test_save_decision_persists_accepted_and_status(factory):
    _seed(factory)
    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        decision = service.DecisionInput(
            status="approved",
            accepted=[
                service.AcceptedInput(sku="p2", rank=1, match_type="exact", reason="value pick"),
                service.AcceptedInput(sku="p1", rank=2, match_type="exact"),
            ],
            each_to_grams=None,
            reviewer_notes="good",
        )
        service.save_decision(s, ic, decision)

    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        detail = service.get_detail(s, ic)
        assert detail.status == "approved"
        assert detail.decided_by == "human"
        accepted = [c for c in detail.candidates if c.accepted]
        assert [c.candidate.sku for c in accepted] == ["p2", "p1"]  # accepted first, by rank
        assert accepted[0].rank == 1
        # spend_score uses the rank-1 accepted product's price (p2 = 2.5).
        assert detail.spend_score == 500 * 2.5


def test_save_decision_ignores_unknown_sku(factory):
    _seed(factory)
    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        decision = service.DecisionInput(
            status="approved",
            accepted=[service.AcceptedInput(sku="ghost", rank=1)],
        )
        service.save_decision(s, ic, decision)
        detail = service.get_detail(s, ic)
        assert all(not c.accepted for c in detail.candidates)


def test_get_detail_overlays_all_candidates(factory):
    _seed(factory)
    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        detail = service.get_detail(s, ic)
        assert len(detail.candidates) == 3  # all candidates present, not just accepted
        # rating suppressed when ratings_count is 0/None
        assert all(c.candidate.avg_rating is None for c in detail.candidates)


def test_bulk_approve(factory):
    _seed(factory)
    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        service.save_decision(s, ic, service.DecisionInput(status="proposed",
                              accepted=[service.AcceptedInput(sku="p1")]))
    with factory() as s:
        n = service.bulk_approve(s, ["name:chicken breast"])
        assert n == 1
        assert service.list_items(s, status="approved")[0].ingredient_key == "name:chicken breast"


def test_list_items_sorted_by_spend_desc(factory):
    with factory() as s:
        seed_candidates(s, "name:salt", "Salt", [{"sku": "s1", "name": "Salt", "price": 0.5}], line_count=50)
        seed_candidates(s, "name:beef", "Beef", [{"sku": "b1", "name": "Beef", "price": 5.0}], line_count=300)
    with factory() as s:
        for key in ("name:salt", "name:beef"):
            ic = gather_candidates(s, key)
            service.save_decision(s, ic, service.DecisionInput(
                status="approved", accepted=[service.AcceptedInput(sku=ic.candidates[0].sku)]))
    with factory() as s:
        items = service.list_items(s)
        # Beef (300 x 5.0) outranks salt (50 x 0.5).
        assert [i.ingredient_key for i in items] == ["name:beef", "name:salt"]


def test_pantry_staple_persists_and_shows_in_detail(factory):
    _seed(factory)
    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        service.save_decision(
            s,
            ic,
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="p1", rank=1)],
                pantry_staple=True,
            ),
        )

    with factory() as s:
        detail = service.get_detail(s, gather_candidates(s, "name:chicken breast"))
        assert detail.pantry_staple is True
        item = next(i for i in service.list_items(s) if i.ingredient_key == "name:chicken breast")
        assert item.pantry_staple is True


def test_pantry_staple_defaults_false(factory):
    _seed(factory)
    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        service.save_decision(
            s,
            ic,
            service.DecisionInput(
                status="approved", accepted=[service.AcceptedInput(sku="p1", rank=1)]
            ),
        )
    with factory() as s:
        detail = service.get_detail(s, gather_candidates(s, "name:chicken breast"))
        assert detail.pantry_staple is False
