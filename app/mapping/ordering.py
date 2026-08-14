"""The default order of a mapping's accepted products.

The LLM decides *which* candidates are genuinely the ingredient and what kind of
match each one is; the order they are offered in is arithmetic, and arithmetic is
done here rather than in the prompt. Two reasons: comparing twenty products on
£/kg and a confidence-adjusted star rating is exactly the sort of consistency an
LLM does not have, and keeping it in code means the balance can be re-tuned by
re-sorting stored proposals (``python -m app.mapping reorder``) instead of paying
for another pass.

The scoring is a port of ``frontend/src/routes/mappingComparison.js``, which
colours the metric pills on the review page. That is the point of the port: the
reviewer sees green next to the products that sorted to the top, so the order
explains itself. Keep the two in step — the constants and the shape of
:func:`relative_quality_scores` and :func:`rating_quality_score` are meant to
match, and ``tests/test_mapping_ordering.py`` mirrors the JS test cases.

The price it compares is the *shelf* price, with any promotion stripped back off
(:func:`sort_unit_price`). An order is not a quote — it is stored, and approving
it freezes it — so it has to be built on the number that will still be true next
month. The basket is priced separately, and does spend the promotion.

Two things are deliberately *not* a weighted term:

* **Match type** is a hard primary key (exact → form_differs → substitute). No
  amount of cheapness promotes a substitute above the real ingredient; the
  planner already believes this order (``MATCH_TYPE_PREFERENCE``) and the
  mapping should not hand it a contradiction.
* **A poor rating** demotes outright rather than being outweighed. Unit price
  varies far more than rating does (median 2x within an accepted set against a
  median 0.13 of confidence-adjusted rating), so a weighted sum alone lets a
  cheap, genuinely bad product take the top slot — the 2.2-star garlic that
  ``drop_poorly_rated`` exists to keep out of the basket.
"""
from __future__ import annotations

import math
from typing import Callable, Iterable, Protocol, Sequence, TypeVar

from app.mapping.candidates import Candidate

# What counts as a bad product is the planner's judgement, imported rather than
# restated so the order a reviewer approves cannot disagree with the order the
# basket then buys in.
from app.planner.basket import RATING_FLOOR, RATING_MAX_DROP, RATING_MIN_COUNT

#: Primary sort key. Mirrors ``app.planner.basket.MATCH_TYPE_PREFERENCE``.
MATCH_TYPE_ORDER = ("exact", "form_differs", "substitute")

#: Five neutral (3-star) reviews, as the review page uses.
RATING_PRIOR_COUNT = 5

#: No opinion. An unscored metric contributes neither for nor against.
NEUTRAL = 0.5

# Price and rating carry the same nominal weight, which is not the same as
# mattering equally. The price score is *positional* — the cheapest of a group
# scores 1 and the dearest 0 however close they are — while the rating score is
# *absolute*, so a set of respectable products all land near 0.8 and separate by
# a tenth. The damping in relative_quality_scores is what converts that into the
# behaviour we want: at the median accepted set (a 2x price spread) price swings
# the full 0.45 and settles it, and as the prices converge its swing collapses
# toward nothing and the rating gap — untouched, because it never depended on the
# spread — is left deciding. Weighting price higher on top of that would make a
# penny's difference outrank a star and a half.
WEIGHT_UNIT_PRICE = 0.45
WEIGHT_RATING = 0.45
# Small enough to settle products the catalogue cannot separate without
# overturning a rating gap worth having.
WEIGHT_LLM_RANK = 0.10

T = TypeVar("T")


class Accepted(Protocol):
    """The accepted-product shape both the proposal and the store use."""

    sku: str
    match_type: str
    llm_rank: int | None


def _finite(value: float | None) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def sort_unit_price(candidate: Candidate | None) -> float | None:
    """The unit price to rank on: the shelf price, not the promotional one.

    An order is not a price quote. It is written into
    ``ingredient_mapping_products.rank`` and, once a reviewer approves it, stays
    there — ``reorder_proposals`` deliberately never moves a mapping a human has
    ordered. A half-price Nectar promotion runs for three weeks; the rank it
    would win outlives it by however long the mapping does, and nothing would
    ever re-sort it back.

    So the promotion is stripped here and only here. The basket still spends
    ``price``, which is what the shop will actually charge — see
    :attr:`app.db.models.Product.base_price`.
    """
    if candidate is None:
        return None
    if candidate.base_unit_price is not None:
        return candidate.base_unit_price
    return candidate.unit_price


def canonical_unit_price_basis(basis: str | None) -> str | None:
    """Collapse spellings that describe the same unit-price basis.

    Unknown retailer-specific bases stay separate rather than being compared
    blindly — 15% of Sainsbury's accepted sets mix kg with each or litres, and
    sorting those on the raw number means nothing.
    """
    if not isinstance(basis, str) or not basis.strip():
        return None
    normalized = basis.strip().lower()
    if normalized.startswith("per "):
        normalized = normalized[4:].lstrip()
    if normalized in ("kg", "kilogram", "kilograms"):
        return "kg"
    if normalized in ("l", "litre", "litres", "liter", "liters"):
        return "l"
    if normalized in ("each", "item", "items"):
        return "each"
    return normalized


def relative_quality_scores(
    items: Iterable[T],
    *,
    key_of: Callable[[T], str],
    value_of: Callable[[T], float | None],
    group_of: Callable[[T], str | None] = lambda _: "all",
    higher_is_better: bool = True,
) -> dict[str, float]:
    """Quality scores (0 = worse, 1 = better) for comparable values.

    Position within the group's range decides the score; the size of that range
    *relative to its minimum* decides how far the endpoints are allowed to leave
    neutral. Ten pence between the cheapest and dearest 1 kg of flour is not the
    same evidence as ten pounds, and this is what stops the sort treating them
    alike. A group of one, or one with no spread at all, goes unscored.
    """
    groups: dict[str, list[tuple[str, float]]] = {}
    for item in items:
        value = value_of(item)
        group = group_of(item)
        if not _finite(value) or group is None:
            continue
        groups.setdefault(group, []).append((key_of(item), float(value)))

    scores: dict[str, float] = {}
    for entries in groups.values():
        if len(entries) < 2:
            continue
        values = [value for _, value in entries]
        minimum, maximum = min(values), max(values)
        spread = maximum - minimum
        if spread <= 0:
            continue
        # Unit prices and ratings are non-negative. If a zero ever arrives, any
        # non-zero range is maximally meaningful and avoids division by zero.
        relative_spread = spread / minimum if minimum > 0 else 1.0
        intensity = min(1.0, math.sqrt(relative_spread))
        for key, value in entries:
            position = (value - minimum) / spread
            quality = position if higher_is_better else 1 - position
            scores[key] = NEUTRAL + (quality - NEUTRAL) * intensity
    return scores


def rating_quality_score(rating: float | None, ratings_count: int | None) -> float | None:
    """The 1–5 star scale mapped onto 0–1, pulled toward neutral when sparse.

    Absolute rather than relative: a set of uniformly excellent products should
    not have one of them ranked last for being 4.7 among 4.8s. Equivalent to
    adding five neutral reviews before scoring, which is what keeps a lone
    five-star review from beating a proven 4.7.
    """
    if not _finite(rating):
        return None
    linear = (max(1.0, min(5.0, float(rating))) - 1) / 4
    count = max(0, ratings_count) if _finite(ratings_count) else 0
    confidence = count / (count + RATING_PRIOR_COUNT)
    return NEUTRAL + (linear - NEUTRAL) * confidence


def _llm_rank_score(llm_rank: int | None, total: int) -> float:
    """The model's own ordering, normalised to 0–1 (first = 1.0).

    This is the one judgement worth keeping from the prompt: whether the pack
    suits how the ingredient is actually used. Without it the best £/kg wins
    outright and a 5 kg sack of potatoes leads an ingredient cooked 400 g at a
    time.
    """
    if llm_rank is None or total < 2:
        return NEUTRAL
    position = min(max(int(llm_rank), 1), total)
    return 1 - (position - 1) / (total - 1)


def _poorly_rated(candidate: Candidate | None, best: float | None) -> bool:
    """Bad outright *and* clearly beaten — ``basket.drop_poorly_rated``'s test."""
    if candidate is None or best is None or candidate.avg_rating is None:
        return False
    if (candidate.ratings_count or 0) < RATING_MIN_COUNT:
        return False
    return candidate.avg_rating < RATING_FLOOR and best - candidate.avg_rating > RATING_MAX_DROP


def _match_type_index(match_type: str) -> int:
    try:
        return MATCH_TYPE_ORDER.index(match_type)
    except ValueError:
        return len(MATCH_TYPE_ORDER)


def score_accepted(
    accepted: Sequence[Accepted], candidates: Sequence[Candidate]
) -> dict[str, float]:
    """Blended 0–1 desirability per accepted sku.

    Scored across the whole accepted set, not per match-type tier, so the number
    behind the order is the same one the review page colours its pills with.
    """
    by_sku = {c.sku: c for c in candidates}
    rows = [(a, by_sku.get(a.sku)) for a in accepted]

    unit_price = relative_quality_scores(
        [row for row in rows if row[1] is not None],
        key_of=lambda row: row[0].sku,
        value_of=lambda row: sort_unit_price(row[1]),
        group_of=lambda row: canonical_unit_price_basis(row[1].unit_price_basis),
        higher_is_better=False,
    )

    total = len(accepted)
    scores: dict[str, float] = {}
    for a, candidate in rows:
        rating = (
            rating_quality_score(candidate.avg_rating, candidate.ratings_count)
            if candidate is not None
            else None
        )
        scores[a.sku] = (
            WEIGHT_UNIT_PRICE * unit_price.get(a.sku, NEUTRAL)
            + WEIGHT_RATING * (rating if rating is not None else NEUTRAL)
            + WEIGHT_LLM_RANK * _llm_rank_score(a.llm_rank, total)
        )
    return scores


def order_accepted(
    accepted: Sequence[Accepted], candidates: Sequence[Candidate]
) -> list[Accepted]:
    """The accepted products in the order they should be offered.

    Match type first, then the poorly-rated demotion, then the blended score.
    The caller renumbers ``rank`` over the result; nothing here mutates.
    """
    by_sku = {c.sku: c for c in candidates}
    scores = score_accepted(accepted, candidates)

    credible = [
        by_sku[a.sku].avg_rating
        for a in accepted
        if a.sku in by_sku
        and by_sku[a.sku].avg_rating is not None
        and (by_sku[a.sku].ratings_count or 0) >= RATING_MIN_COUNT
    ]
    best_rating = max(credible) if credible else None

    return sorted(
        accepted,
        key=lambda a: (
            _match_type_index(a.match_type),
            _poorly_rated(by_sku.get(a.sku), best_rating),
            -scores[a.sku],
            a.llm_rank if a.llm_rank is not None else 0,
            a.sku,
        ),
    )
