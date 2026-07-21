"""LLM proposal pass: prompt building, response parsing, and orchestration."""
from __future__ import annotations

from app.mapping.candidates import Candidate, IngredientCandidates, UsageStats, gather_candidates
from app.mapping import propose as P
from app.mapping import service

from tests.conftest import seed_candidates


def _candidate(sku, name, price=1.0, rank=1):
    return Candidate(
        product_id=rank,
        sku=sku,
        name=name,
        brand=None,
        pack_size_raw="500g",
        pack_size_value=500,
        pack_size_unit="g",
        price=price,
        unit_price=price * 2,
        unit_price_basis="kg",
        avg_rating=4.5,
        ratings_count=10,
        url="http://x",
        result_rank=rank,
    )


def _ic():
    return IngredientCandidates(
        ingredient_key="name:chicken breast",
        name="Chicken Breast",
        line_count=500,
        usage=UsageStats(line_count=500, metric_unit="g", median=450, p25=400, p75=500),
        candidates=[_candidate("sku-a", "Chicken Breast Fillets", rank=1),
                    _candidate("sku-b", "Diced Chicken", rank=2)],
    )


def test_build_prompt_includes_usage_and_candidates():
    system, user = P.build_prompt(_ic())
    assert "own-brand" in system
    assert "Chicken Breast" in user
    assert "sku-a" in user and "sku-b" in user
    assert "450" in user  # median usage grams


def test_parse_proposal_drops_hallucinated_and_normalises_ranks():
    ic = _ic()
    raw = {
        "accepted": [
            {"sku": "sku-b", "rank": 5, "match_type": "exact", "reason": "ok"},
            {"sku": "sku-a", "rank": 2, "match_type": "form_differs", "reason": "raw"},
            {"sku": "sku-ghost", "rank": 1, "match_type": "exact", "reason": "hallucinated"},
        ],
        "each_to_grams": None,
        "needs_substitution": False,
        "note": "fine",
    }
    parsed = P.parse_proposal(raw, ic)
    skus = [a.sku for a in parsed.accepted]
    assert skus == ["sku-a", "sku-b"]  # ghost dropped, sorted by rank then renumbered
    assert [a.rank for a in parsed.accepted] == [1, 2]
    assert parsed.accepted[0].match_type == "form_differs"


def test_parse_proposal_invalid_match_type_defaults_to_exact():
    ic = _ic()
    raw = {"accepted": [{"sku": "sku-a", "rank": 1, "match_type": "nonsense", "reason": ""}],
           "each_to_grams": 67.0, "needs_substitution": False, "note": ""}
    parsed = P.parse_proposal(raw, ic)
    assert parsed.accepted[0].match_type == "exact"
    assert parsed.each_to_grams == 67.0


def test_run_propose_writes_proposed_rows_and_is_idempotent(factory):
    with factory() as s:
        seed_candidates(
            s, "name:garlic", "Garlic",
            [{"sku": "g1", "name": "Ocado Garlic", "price": 0.3, "pack_value": 3, "pack_unit": "each"},
             {"sku": "g2", "name": "Garlic Bulb", "price": 0.5}],
            line_count=200,
        )

    def fake_complete(system, user, schema):
        return {
            "accepted": [{"sku": "g1", "rank": 1, "match_type": "exact", "reason": "own brand"}],
            "each_to_grams": 5.0,
            "needs_substitution": False,
            "note": "ok",
        }

    res = P.run_propose(factory, complete=fake_complete, model="test-model")
    assert res.proposed == 1

    with factory() as s:
        items = service.list_items(s)
        assert len(items) == 1
        assert items[0].status == "proposed"
        assert items[0].num_accepted == 1
        assert items[0].each_to_grams == 5.0
        assert items[0].spend_score == 200 * 0.3  # line_count x rank-1 price

    # Re-running without --force skips the already-mapped ingredient.
    res2 = P.run_propose(factory, complete=fake_complete, model="test-model")
    assert res2.proposed == 0 and res2.skipped == 1


def test_worklist_excludes_ingredients_without_candidates(factory):
    # An ingredient with no ProductSearchHit rows never enters the worklist.
    from app.mapping.candidates import iter_worklist

    with factory() as s:
        seed_candidates(s, "name:beef", "Beef", [{"sku": "b1", "name": "Beef Mince", "price": 3.0}])
        work = iter_worklist(s)
    assert [ic.ingredient_key for ic in work] == ["name:beef"]
