"""The computed default order of a mapping's accepted products.

The scoring half of these mirrors ``frontend/src/routes/mappingComparison.test.js``
case for case: the review page colours its metric pills with the same maths, and
the whole point of the port is that the colours explain the order. If one side
changes, this file should fail.
"""
from __future__ import annotations

from app.mapping import ordering as O
from app.mapping.candidates import Candidate


def _candidate(
    sku, *, unit_price=None, basis="kg", rating=None, count=None, price=1.0,
    base_unit_price=None,
):
    return Candidate(
        base_unit_price=base_unit_price,
        product_id=abs(hash(sku)) % 10_000,
        sku=sku,
        name=sku,
        brand=None,
        pack_size_raw="500g",
        pack_size_value=500,
        pack_size_unit="g",
        price=price,
        unit_price=unit_price,
        unit_price_basis=basis if unit_price is not None else None,
        avg_rating=rating,
        ratings_count=count,
        url=None,
        result_rank=1,
    )


class _Accepted:
    """The minimal shape ``order_accepted`` consumes."""

    def __init__(self, sku, match_type="exact", llm_rank=None):
        self.sku = sku
        self.match_type = match_type
        self.llm_rank = llm_rank

    def __repr__(self):  # pragma: no cover - test failure output only
        return f"<{self.sku} {self.match_type} llm={self.llm_rank}>"


def _scores(items, **kwargs):
    return O.relative_quality_scores(
        items, key_of=lambda i: i["id"], value_of=lambda i: i["value"], **kwargs
    )


# --- the ported scoring (mirrors the JS tests) -------------------------------


def test_a_small_relative_spread_remains_close_to_neutral():
    scores = _scores([{"id": "lower", "value": 4.8}, {"id": "higher", "value": 4.9}])
    assert scores["lower"] > 0.4
    assert scores["higher"] < 0.6


def test_a_large_spread_reaches_the_endpoints_with_lower_prices_better():
    scores = _scores(
        [{"id": "cheap", "value": 2}, {"id": "middle", "value": 3}, {"id": "expensive", "value": 4}],
        higher_is_better=False,
    )
    assert scores == {"cheap": 1.0, "middle": 0.5, "expensive": 0.0}


def test_single_equal_and_missing_values_remain_unscored():
    scores = _scores(
        [{"id": "same-a", "value": 3}, {"id": "same-b", "value": 3}, {"id": "missing", "value": None}]
    )
    assert scores == {}


def test_unit_prices_compare_only_within_canonical_like_for_like_groups():
    items = [
        {"id": "retailer-kg", "value": 8, "basis": "kg"},
        {"id": "manual-kg", "value": 10, "basis": "per kg"},
        {"id": "litre", "value": 2, "basis": "l"},
    ]
    scores = O.relative_quality_scores(
        items,
        key_of=lambda i: i["id"],
        value_of=lambda i: i["value"],
        group_of=lambda i: O.canonical_unit_price_basis(i["basis"]),
        higher_is_better=False,
    )
    assert scores["retailer-kg"] > scores["manual-kg"]
    assert "litre" not in scores  # alone in its group, so nothing to compare against
    assert O.canonical_unit_price_basis("per litre") == "l"
    assert O.canonical_unit_price_basis("  ") is None


def test_rating_score_uses_the_fixed_linear_scale_and_clamps_outliers():
    assert O.rating_quality_score(1, 100) < O.rating_quality_score(3, 100)
    assert O.rating_quality_score(3, 100) < O.rating_quality_score(5, 100)
    assert O.rating_quality_score(0.5, 100) == O.rating_quality_score(1, 100)
    assert O.rating_quality_score(5.5, 100) == O.rating_quality_score(5, 100)
    assert O.rating_quality_score(None, 100) is None


def test_low_review_counts_pull_the_rating_score_toward_neutral():
    sparse_perfect = O.rating_quality_score(5, 2)
    proven_high = O.rating_quality_score(4.7, 500)

    assert 0.5 < sparse_perfect < 0.7
    assert sparse_perfect < proven_high
    assert proven_high > 0.9
    assert O.rating_quality_score(5, 0) == 0.5


# --- the ordering itself ----------------------------------------------------


def test_match_type_is_the_primary_key_whatever_the_value_says():
    accepted = [
        _Accepted("bargain-sub", match_type="substitute", llm_rank=1),
        _Accepted("dearer-exact", match_type="exact", llm_rank=2),
        _Accepted("mid-form", match_type="form_differs", llm_rank=3),
    ]
    candidates = [
        _candidate("bargain-sub", unit_price=1.0, rating=5.0, count=900),
        _candidate("dearer-exact", unit_price=20.0, rating=3.6, count=40),
        _candidate("mid-form", unit_price=4.0, rating=4.5, count=200),
    ]
    ordered = [a.sku for a in O.order_accepted(accepted, candidates)]
    assert ordered == ["dearer-exact", "mid-form", "bargain-sub"]


def test_within_a_tier_a_much_cheaper_unit_price_wins():
    accepted = [_Accepted("dear", llm_rank=1), _Accepted("cheap", llm_rank=2)]
    candidates = [
        _candidate("dear", unit_price=12.0, rating=4.4, count=100),
        _candidate("cheap", unit_price=3.0, rating=4.3, count=100),
    ]
    assert [a.sku for a in O.order_accepted(accepted, candidates)][0] == "cheap"


def test_a_promotion_does_not_win_the_top_slot():
    # The half-price one is cheaper today and dearer than its rival on the shelf.
    # Ranking on today's price would freeze a three-week Nectar promotion into a
    # stored rank that nothing re-sorts once the mapping is approved.
    accepted = [_Accepted("promoted", llm_rank=1), _Accepted("plain", llm_rank=2)]
    candidates = [
        _candidate("promoted", unit_price=7.0, base_unit_price=14.0, rating=4.4, count=100),
        _candidate("plain", unit_price=10.0, rating=4.4, count=100),
    ]
    assert [a.sku for a in O.order_accepted(accepted, candidates)][0] == "plain"
    assert O.sort_unit_price(candidates[0]) == 14.0


def test_a_product_with_no_promotion_ranks_on_the_price_it_has():
    # Only a real offer sets a base price, so everything else must keep sorting
    # exactly as it did rather than falling out of the comparison.
    assert O.sort_unit_price(_candidate("plain", unit_price=10.0)) == 10.0
    assert O.sort_unit_price(_candidate("unpriced")) is None
    assert O.sort_unit_price(None) is None


def test_a_tight_price_spread_lets_the_rating_decide():
    # Pennies apart: the price term damps to nearly neutral, so the product
    # people actually rate well should lead even though it is not the cheapest.
    accepted = [_Accepted("meh", llm_rank=1), _Accepted("loved", llm_rank=2)]
    candidates = [
        _candidate("meh", unit_price=5.00, rating=3.4, count=400),
        _candidate("loved", unit_price=5.05, rating=4.8, count=400),
    ]
    assert [a.sku for a in O.order_accepted(accepted, candidates)][0] == "loved"


def test_a_credibly_poor_rating_is_demoted_however_cheap_it_is():
    accepted = [_Accepted("cheap-bad", llm_rank=1), _Accepted("dear-good", llm_rank=2)]
    candidates = [
        _candidate("cheap-bad", unit_price=1.0, rating=2.2, count=60),
        _candidate("dear-good", unit_price=11.0, rating=4.3, count=60),
    ]
    assert [a.sku for a in O.order_accepted(accepted, candidates)] == ["dear-good", "cheap-bad"]


def test_a_poor_rating_from_two_reviews_is_not_evidence_enough_to_demote():
    accepted = [_Accepted("cheap-unproven", llm_rank=2), _Accepted("dear-good", llm_rank=1)]
    candidates = [
        _candidate("cheap-unproven", unit_price=1.0, rating=2.2, count=2),
        _candidate("dear-good", unit_price=11.0, rating=4.3, count=60),
    ]
    assert [a.sku for a in O.order_accepted(accepted, candidates)][0] == "cheap-unproven"


def test_the_model_ordering_breaks_ties_the_catalogue_cannot():
    # Same price, same rating: all that is left is which pack the model thought
    # suited the way the ingredient is actually cooked.
    accepted = [_Accepted("second", llm_rank=2), _Accepted("first", llm_rank=1)]
    candidates = [
        _candidate("second", unit_price=4.0, rating=4.5, count=50),
        _candidate("first", unit_price=4.0, rating=4.5, count=50),
    ]
    assert [a.sku for a in O.order_accepted(accepted, candidates)] == ["first", "second"]


def test_products_missing_price_or_rating_score_neutral_rather_than_last():
    accepted = [
        _Accepted("unknown", llm_rank=2),
        _Accepted("dearest", llm_rank=3),
        _Accepted("cheapest", llm_rank=1),
    ]
    candidates = [
        _candidate("unknown", unit_price=None, rating=None, count=None),
        _candidate("dearest", unit_price=20.0, rating=4.5, count=90),
        _candidate("cheapest", unit_price=2.0, rating=4.5, count=90),
    ]
    ordered = [a.sku for a in O.order_accepted(accepted, candidates)]
    assert ordered == ["cheapest", "unknown", "dearest"]


def test_mixed_unit_price_bases_are_not_compared_against_each_other():
    # A per-each price beside per-kg ones says nothing, so the each product is
    # placed on its rating and the model's ordering alone.
    accepted = [_Accepted("by-each", llm_rank=3), _Accepted("kg-a", llm_rank=1), _Accepted("kg-b", llm_rank=2)]
    candidates = [
        _candidate("by-each", unit_price=0.3, basis="each", rating=4.5, count=50),
        _candidate("kg-a", unit_price=3.0, rating=4.5, count=50),
        _candidate("kg-b", unit_price=9.0, rating=4.5, count=50),
    ]
    ordered = [a.sku for a in O.order_accepted(accepted, candidates)]
    assert ordered == ["kg-a", "by-each", "kg-b"]


def test_an_unknown_match_type_sorts_last_rather_than_raising():
    accepted = [_Accepted("odd", match_type="nonsense", llm_rank=1), _Accepted("fine", llm_rank=2)]
    candidates = [_candidate("odd", unit_price=1.0), _candidate("fine", unit_price=9.0)]
    assert [a.sku for a in O.order_accepted(accepted, candidates)] == ["fine", "odd"]
