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


def test_save_decision_rejects_unknown_sku(factory):
    _seed(factory)
    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        decision = service.DecisionInput(
            status="approved",
            accepted=[service.AcceptedInput(sku="ghost", rank=1)],
        )
        try:
            service.save_decision(s, ic, decision)
        except ValueError as exc:
            assert "unknown accepted sku" in str(exc)
        else:
            raise AssertionError("unknown accepted sku should be rejected")


def test_save_decision_dedupes_repeated_sku(factory):
    """A SKU sent twice is stored once, keeping its best rank (uq_mapping_product_sku)."""
    _seed(factory)
    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        service.save_decision(s, ic, service.DecisionInput(
            status="approved",
            accepted=[
                service.AcceptedInput(sku="p1", rank=2, reason="second"),
                service.AcceptedInput(sku="p1", rank=1, reason="first"),
                service.AcceptedInput(sku="p2", rank=3),
            ],
        ))

    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        accepted = [c for c in service.get_detail(s, ic).candidates if c.accepted]
        assert [c.candidate.sku for c in accepted] == ["p1", "p2"]
        assert [c.rank for c in accepted] == [1, 2]  # re-ranked contiguously


def test_approved_requires_product_unless_pantry(factory):
    _seed(factory)
    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        try:
            service.save_decision(s, ic, service.DecisionInput(status="approved", accepted=[]))
        except ValueError as exc:
            assert "at least one accepted product" in str(exc)
        else:
            raise AssertionError("empty non-pantry approval should be rejected")

        service.save_decision(
            s, ic, service.DecisionInput(status="approved", accepted=[], pantry_staple=True)
        )
        detail = service.get_detail(s, ic)
        assert detail.status == "approved" and detail.pantry_staple is True


def test_get_detail_overlays_all_candidates(factory):
    _seed(factory)
    with factory() as s:
        ic = gather_candidates(s, "name:chicken breast")
        detail = service.get_detail(s, ic)
        assert len(detail.candidates) == 3  # all candidates present, not just accepted
        # rating suppressed when ratings_count is 0/None
        assert all(c.candidate.avg_rating is None for c in detail.candidates)


def test_candidate_detail_carries_the_frozen_storage_form(factory):
    with factory() as s:
        seed_candidates(
            s,
            "name:peas",
            "Peas",
            [{"sku": "frozen-peas", "name": "Garden Peas", "is_frozen": True}],
        )
    with factory() as s:
        detail = service.get_detail(s, gather_candidates(s, "name:peas"))

    assert detail.candidates[0].candidate.is_frozen is True


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


def test_list_items_paginates_searches_and_hides_pantry_spend(factory):
    with factory() as s:
        seed_candidates(s, "name:salt", "Salt", [{"sku": "s1", "name": "Salt", "price": 0.5}], line_count=50)
        seed_candidates(s, "name:beef", "Beef", [{"sku": "b1", "name": "Beef", "price": 5.0}], line_count=300)
        seed_candidates(s, "name:beans", "Beans", [{"sku": "z1", "name": "Beans", "price": 1.0}], line_count=100)
        service.save_decision(
            s,
            gather_candidates(s, "name:salt"),
            service.DecisionInput(status="approved", accepted=[], pantry_staple=True),
        )
        for key in ("name:beef", "name:beans"):
            ic = gather_candidates(s, key)
            service.save_decision(
                s,
                ic,
                service.DecisionInput(status="approved", accepted=[service.AcceptedInput(sku=ic.candidates[0].sku)]),
            )

    with factory() as s:
        assert service.count_items(s, q="be") == 2
        page = service.list_items(s, q="be", limit=1)
        assert len(page) == 1 and page[0].ingredient_key == "name:beef"
        pantry = next(i for i in service.list_items(s) if i.ingredient_key == "name:salt")
        assert pantry.spend_score is None
        assert pantry.top_product_name is None


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
