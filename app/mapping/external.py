"""Offer a specialist retailer's catalogue as candidates in the review queue.

Ocado cannot supply HelloFresh's own spice blends — "Chermoula Spice Mix",
"Central American Style Spice Mix", "North Indian Style Spice Mix" and a couple
of dozen more. Until now the only answer was to hand-enter each as a manual
product (:mod:`app.mapping.manual`), which is accurate but slow and goes stale
the moment a price changes. Seasoned Pioneers sells nearly all of them — they
market a "Hello Fresh Spices" bundle naming six — so with their catalogue cached
locally the same job becomes a matching problem instead of a typing one.

Matching runs against the cached rows rather than a search endpoint, because the
whole catalogue is only ~320 buyable products. That means the scoring below is
the *only* relevance judgement in play, so it is deliberately conservative: it
proposes, and a human still accepts. Nothing here approves anything.

Where the products end up is the trick borrowed from :mod:`app.mapping.manual`:
the candidate *hit* is filed under the mapping's host retailer while the
*product* keeps ``retailer='seasoned_pioneers'``. ``ProductSearchHit.retailer``
names the review context an ingredient is being shopped for, so these appear in
the normal candidate list and the existing accept/rank UI works untouched, while
:attr:`app.planner.index.Pack.external` still routes them out of the Ocado order
and onto their own line of the shopping list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import IngredientMapping, Product, ProductSearchHit
from app.mapping import service
from app.scraper.products.seasoned_pioneers import RETAILER as CATALOGUE_RETAILER

#: The review queue these candidates join; see the module docstring.
HOST_RETAILER = service.RETAILER

#: Dice coefficient over content tokens. Tuned against the real catalogue: 0.6
#: keeps "Cajun Spice Mix" -> "Cajun Seasoning Spice Blend" and drops
#: "Chilli Flakes" -> "Chilli con Carne Spices". Raise it for a stricter pass.
MIN_SCORE = 0.6
#: Two tokens count as the same word above this ratio, which is what lets
#: "Za'atar" find "Zahtar" without a hand-maintained spelling table.
TOKEN_MATCH_RATIO = 0.8
#: ...but only between words long enough, and close enough in length, for a
#: near-miss to mean a spelling variant rather than a different word. Without
#: this, "peas" matches "petals" at 0.8 and the catalogue offers rose petals for
#: garden peas. "zahtar"/"zaatar" is 6 against 6; "peas"/"petals" is 4 against 6.
TOKEN_MIN_LENGTH = 5
TOKEN_LENGTH_RATIO = 0.75
#: Candidates offered per ingredient. More than a handful just makes the review
#: list longer without making the decision better.
DEFAULT_LIMIT = 4

# The containers HelloFresh ships a dry seasoning in. This catalogue is entirely
# dried goods, so without this guard the name match cheerfully offers "Red
# Chillies Crushed" for fresh "Red Chilli" (585 lines) and "Thyme Leaves" for the
# bunch of fresh thyme — names that are close and products that are not
# interchangeable. How an ingredient *arrives* settles it where the name cannot:
# fresh produce comes as unit(s) or bunch(es), never as a sachet.
_SEASONING_UNITS = ("sachet(s)", "pot(s)", "pinch")

# Words that describe the *form of packaging* rather than the ingredient.
# Dropping them is what makes "Cajun Spice Mix" and "Cajun Seasoning Spice Blend"
# the same thing, and "Dried Oregano" the same as "Oregano Leaves, Wild-Grown" —
# every herb in this catalogue is dried, so saying so distinguishes nothing.
#
# "Ground", "whole" and "seeds" are deliberately NOT here: ground cumin and whole
# cumin seeds are different products and the reviewer has to see which one is
# being offered. Nor is "organic", which is a real difference in price.
_FILLER = frozenset(
    {
        "a", "and", "of", "the", "with",
        "style", "spice", "spices", "seasoning", "seasonings",
        "mix", "mixes", "blend", "blends", "rub", "rubs",
        "dried", "leaves", "leaf", "wild", "grown",
    }
)

# Words for the same preparation, folded together so the fuzzy token match is not
# asked to see through a genuine synonym. HelloFresh writes "Ground Turmeric"
# where the shop writes "Turmeric Powder", and "Chilli Flakes" for "Red Chillies
# Crushed" — neither pair is close enough as *text* to match on spelling alone.
_BLEND_KIND = frozenset(
    {
        "spice", "spices", "mix", "mixes", "blend", "blends",
        "seasoning", "seasonings", "rub", "rubs", "masala", "powder",
    }
)

_SYNONYMS = {
    "powder": "ground",
    "powdered": "ground",
    "crushed": "flakes",
    "flaked": "flakes",
    "chillies": "chilli",
    "chile": "chilli",
    "chiles": "chilli",
}

# Parenthesised trailers are alternative names, and which of the two names
# matches varies: "North Indian Style Spice Mix (Curry Powder)" matches on the
# part outside, "Pimenton Dulce, Smoked (Smoked Paprika)" only on the part
# inside. So both are scored and the better one wins, rather than picking one
# convention and losing half the catalogue's aliases.
_PARENS_RE = re.compile(r"\(([^)]*)\)")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
# Apostrophes elide rather than separate: "Za'atar" is one word, and splitting it
# into "za" + "atar" loses the very token that matches "Zahtar".
_APOSTROPHE_RE = re.compile(r"['‘’]")


@dataclass(frozen=True)
class CatalogueMatch:
    """One catalogue product proposed for an ingredient, with its score."""

    sku: str
    product_name: str
    score: float
    price: float | None = None
    pack_size_raw: str | None = None
    url: str | None = None
    in_stock: bool | None = None


@dataclass
class AttachResult:
    ingredients_matched: int = 0
    hits_added: int = 0
    considered: int = 0
    skipped_not_seasoning: int = 0
    notes: list[str] = field(default_factory=list)


def normalize_name(name: str) -> list[str]:
    """Reduce a product or ingredient name to its content tokens."""
    lowered = _APOSTROPHE_RE.sub("", _PARENS_RE.sub(" ", name.lower()))
    tokens = [_SYNONYMS.get(t, t) for t in _NON_WORD_RE.sub(" ", lowered).split() if t]
    content = [t for t in tokens if t not in _FILLER]
    # An all-filler name ("Spice Mix") still has to compare as something, so fall
    # back to the raw tokens rather than scoring every such pair as a match.
    # Duplicates would inflate the denominator without adding information.
    return list(dict.fromkeys(content or tokens))


def name_variants(name: str) -> list[str]:
    """The name itself plus whatever its parentheses offer as an alias."""
    variants = [_PARENS_RE.sub(" ", name)]
    variants.extend(group for group in _PARENS_RE.findall(name) if group.strip())
    return variants


def is_blend_kind(name: str) -> bool:
    """True when the name says the product is a prepared mix, not a raw spice.

    :func:`normalize_name` throws these words away, and rightly — they are noise
    for *identity*, which is why "Cajun Spice Mix" and "Cajun Seasoning Spice
    Blend" match. But they are real signal for *kind*, and losing them entirely
    lets "Oregano Mexican, Leaves" and "Mexican Adobo Spice Rub" tie for "Mexican
    Style Spice Mix" on token overlap alone. So the words are dropped from the
    score and reinstated as a tie-break.
    """
    tokens = set(_NON_WORD_RE.sub(" ", name.lower()).split())
    return bool(tokens & _BLEND_KIND)


def similarity(ingredient_name: str, product_name: str) -> float:
    """Best Dice coefficient over content tokens across the names' aliases.

    Token-set rather than whole-string comparison because word *order* carries no
    meaning here ("Caribbean Style Jerk" vs "Caribbean Jerk Seasoning Spice Rub")
    while the presence of an extra content word usually does ("Chilli Flakes" vs
    "Chilli con Carne Spices").
    """
    return max(
        (
            _token_dice(normalize_name(left), normalize_name(right))
            for left in name_variants(ingredient_name)
            for right in name_variants(product_name)
        ),
        default=0.0,
    )


def _token_dice(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0

    unmatched = list(right)
    shared = 0
    for token in left:
        best_index, best_ratio = None, 0.0
        for index, candidate in enumerate(unmatched):
            if token == candidate:
                best_index, best_ratio = index, 1.0
                break
            if not _comparable_length(token, candidate):
                continue
            ratio = SequenceMatcher(None, token, candidate).ratio()
            if ratio > best_ratio:
                best_index, best_ratio = index, ratio
        if best_index is not None and best_ratio >= TOKEN_MATCH_RATIO:
            shared += 1
            unmatched.pop(best_index)
    return 2 * shared / (len(left) + len(right))


def _comparable_length(left: str, right: str) -> bool:
    """Whether two words are worth comparing as possible spelling variants."""
    shortest, longest = min(len(left), len(right)), max(len(left), len(right))
    return shortest >= TOKEN_MIN_LENGTH and shortest / longest >= TOKEN_LENGTH_RATIO


def match_products(
    session: Session,
    name: str,
    *,
    limit: int = DEFAULT_LIMIT,
    min_score: float = MIN_SCORE,
) -> list[CatalogueMatch]:
    """Score the cached catalogue against one ingredient name, best first."""
    products = session.scalars(
        select(Product).where(Product.retailer == CATALOGUE_RETAILER)
    ).all()
    wanted_blend = is_blend_kind(name)
    scored = [
        (
            CatalogueMatch(
                sku=product.sku,
                product_name=product.name,
                score=score,
                price=product.price,
                pack_size_raw=product.pack_size_raw,
                url=product.url,
                in_stock=bool(product.in_stock) if product.in_stock is not None else None,
            ),
            is_blend_kind(product.name) == wanted_blend,
        )
        for product in products
        if (score := similarity(name, product.name)) >= min_score
    ]
    # Score, then kind agreement, then price. Among name matches that are equally
    # good and equally the right kind of thing, the cheaper pack is the better
    # default; ties would otherwise fall out in insertion order.
    scored.sort(
        key=lambda row: (
            -row[0].score,
            not row[1],
            row[0].price if row[0].price is not None else 1e9,
            row[0].sku,
        )
    )
    return [match for match, _agrees in scored[:limit]]


def attach_matches(
    session: Session,
    ingredient_key: str,
    *,
    name: str | None = None,
    limit: int = DEFAULT_LIMIT,
    min_score: float = MIN_SCORE,
) -> list[CatalogueMatch]:
    """Offer the best catalogue matches for one ingredient as candidates.

    Idempotent, and additive: an existing hit is left where it is rather than
    re-ranked, so re-running this never disturbs a pool the reviewer has already
    worked through. Returns the matches now attached, including any that were
    already there.
    """
    display_name = resolve_name(session, ingredient_key, name=name)
    matches = match_products(
        session, display_name, limit=limit, min_score=min_score
    )
    if not matches:
        return []

    existing = {
        row[0]
        for row in session.execute(
            select(ProductSearchHit.sku).where(
                ProductSearchHit.retailer == HOST_RETAILER,
                ProductSearchHit.ingredient_key == ingredient_key,
            )
        )
    }
    line_count = session.scalar(
        select(func.max(ProductSearchHit.line_count)).where(
            ProductSearchHit.retailer == HOST_RETAILER,
            ProductSearchHit.ingredient_key == ingredient_key,
        )
    ) or _mapping_line_count(session, ingredient_key)
    next_rank = (
        session.scalar(
            select(func.max(ProductSearchHit.result_rank)).where(
                ProductSearchHit.retailer == HOST_RETAILER,
                ProductSearchHit.ingredient_key == ingredient_key,
            )
        )
        or 0
    )

    for match in matches:
        if match.sku in existing:
            continue
        product = session.scalar(
            select(Product).where(
                Product.retailer == CATALOGUE_RETAILER, Product.sku == match.sku
            )
        )
        if product is None:
            continue
        next_rank += 1
        session.add(
            ProductSearchHit(
                product_id=product.id,
                retailer=HOST_RETAILER,
                ingredient_key=ingredient_key,
                # Recording the matched-against name, not a search term anyone
                # typed, keeps the review UI honest about where this came from.
                search_term=display_name,
                term_rank=0,
                line_count=line_count or 0,
                sku=match.sku,
                result_rank=next_rank,
            )
        )
    session.flush()
    return matches


def arrives_as_seasoning(common_native_amounts: str | None) -> bool:
    """True when the recipe library ships this ingredient as a dry seasoning.

    Reads the frequency data's ``common_native_amounts`` ("1 sachet(s) (244) |
    1 pot(s) (48)"), which records the containers the ingredient actually arrives
    in. That is a far better test of "is this the dried thing?" than the name:
    "Dried Thyme" is a sachet and "Thyme" is a bunch, and no amount of string
    similarity will tell you which of those a jar of thyme leaves replaces.
    """
    if not common_native_amounts:
        return False
    return any(unit in common_native_amounts for unit in _SEASONING_UNITS)


def attach_all(
    session: Session,
    *,
    keys: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    min_score: float = MIN_SCORE,
    include_approved: bool = False,
    seasonings_only: bool = True,
    usage: dict | None = None,
) -> AttachResult:
    """Run the match over the review queue in one pass.

    Skips aliases (they inherit their root's products) and, unless asked,
    ingredients already approved — an approved mapping is a decision, and
    quietly widening its candidate pool would invite re-opening settled work.

    ``seasonings_only`` (the default) restricts the pass to ingredients that
    arrive as a dry seasoning; see :func:`arrives_as_seasoning`. Turn it off to
    score the whole queue, which is useful when hunting for a substitute by hand
    but noisy as a bulk pass.
    """
    from app.mapping.candidates import load_usage_stats

    result = AttachResult()
    stats = usage if usage is not None else (load_usage_stats() if seasonings_only else {})
    stmt = select(
        IngredientMapping.ingredient_key, IngredientMapping.name, IngredientMapping.status
    ).where(IngredientMapping.retailer == HOST_RETAILER)
    if keys is not None:
        stmt = stmt.where(IngredientMapping.ingredient_key.in_(keys))
    rows = session.execute(stmt).all()

    for key, name, status in rows:
        if status == "alias":
            continue
        if status == "approved" and not include_approved:
            continue
        if seasonings_only:
            entry = stats.get(key)
            if entry is None or not arrives_as_seasoning(entry.common_native_amounts):
                result.skipped_not_seasoning += 1
                continue
        result.considered += 1
        before = _hit_count(session, key)
        matches = attach_matches(session, key, name=name, limit=limit, min_score=min_score)
        if matches:
            result.ingredients_matched += 1
            result.hits_added += _hit_count(session, key) - before
    session.commit()
    result.notes.append(
        f"{result.considered} ingredients considered, "
        f"{result.ingredients_matched} matched, {result.hits_added} candidates added"
        + (
            f", {result.skipped_not_seasoning} skipped (not shipped as a seasoning)"
            if result.skipped_not_seasoning
            else ""
        )
    )
    return result


def resolve_name(
    session: Session, ingredient_key: str, *, name: str | None = None, fallback: str | None = None
) -> str:
    """The wording to score against for an ingredient.

    Worth being careful about: an ingredient with no candidates yet has no
    display name to borrow from a search hit, and the raw key leaks its
    ``name:`` prefix — scoring "name:chermoula spice mix" against "Chermoula
    Spice Mix" drops it from 1.0 to 0.67 on a token nobody meant to include.
    """
    if name:
        return name
    mapped = _mapping_name(session, ingredient_key)
    if mapped:
        return mapped
    if fallback and fallback != ingredient_key:
        return fallback
    _prefix, _sep, rest = ingredient_key.partition(":")
    return rest or ingredient_key


def _hit_count(session: Session, ingredient_key: str) -> int:
    return session.scalar(
        select(func.count())
        .select_from(ProductSearchHit)
        .where(
            ProductSearchHit.retailer == HOST_RETAILER,
            ProductSearchHit.ingredient_key == ingredient_key,
        )
    ) or 0


def _mapping_name(session: Session, ingredient_key: str) -> str | None:
    return session.scalar(
        select(IngredientMapping.name).where(
            IngredientMapping.retailer == HOST_RETAILER,
            IngredientMapping.ingredient_key == ingredient_key,
        )
    )


def _mapping_line_count(session: Session, ingredient_key: str) -> int:
    return session.scalar(
        select(IngredientMapping.line_count).where(
            IngredientMapping.retailer == HOST_RETAILER,
            IngredientMapping.ingredient_key == ingredient_key,
        )
    ) or 0
