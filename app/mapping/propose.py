"""The offline LLM proposal pass: ingredient candidates → proposed mapping.

Pure prompt-building and parsing live here (testable with a fake completer); the
network call is injected. Every write is ``status='proposed'`` / ``source='llm'``
— nothing downstream reads a mapping until a human approves it, so this pass is
safe to re-run and tune.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.mapping import ordering, service
from app.mapping.candidates import Candidate, IngredientCandidates, iter_worklist
from app.mapping.openai_client import Completer
from app.retailers import DEFAULT_RETAILER

log = logging.getLogger("holafresca.mapping")

MATCH_TYPES = ("exact", "substitute", "form_differs")

SYSTEM_PROMPT = (
    "You map a cooking-recipe ingredient to real UK supermarket grocery products. "
    "You are given the ingredient, how it is typically used across recipes (grams), "
    "and a list of candidate products already returned by the retailer's search. "
    "Choose only the candidates that genuinely ARE this ingredient, and rank them by "
    "how well the pack suits the typical usage.\n"
    "Policies:\n"
    "- Prefer own-brand / everyday options over premium ranges; prefer the product family "
    "whose pack size suits the typical usage. Include a few pack sizes of the same product "
    "when useful.\n"
    "- Reject wrong forms (e.g. breaded when raw is wanted), and unrelated products the "
    "search dragged in. Reject clearly bad products.\n"
    "- Do NOT try to balance price against rating when ranking: the final order is computed "
    "afterwards from unit price and ratings. Your 'rank' should express only which product "
    "is the most sensible thing to cook from — pack size against typical usage, and how "
    "squarely the product is the ingredient.\n"
    "- NEVER force a match. If nothing fits directly, return an empty 'accepted' list and set "
    "'needs_substitution' true, explaining the substitution or composite in 'note'.\n"
    "- If the ingredient is sold by count (e.g. limes, garlic bulbs) estimate 'each_to_grams' "
    "(grams per single unit); otherwise leave it null.\n"
    "- 'match_type': 'exact' (is the ingredient), 'substitute' (a stand-in), or 'form_differs' "
    "(right ingredient, different prep/form). This decides the top-level grouping of the "
    "offered products, so be precise with it. A frozen candidate is 'form_differs' unless the "
    "ingredient explicitly asks for frozen. Every accepted sku must be one of the given skus."
)

PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "accepted": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sku": {"type": "string"},
                    "rank": {"type": "integer"},
                    "match_type": {"type": "string", "enum": list(MATCH_TYPES)},
                    "reason": {"type": "string"},
                },
                "required": ["sku", "rank", "match_type", "reason"],
            },
        },
        "each_to_grams": {"type": ["number", "null"]},
        "needs_substitution": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["accepted", "each_to_grams", "needs_substitution", "note"],
}


@dataclass
class AcceptedProduct:
    sku: str
    rank: int
    match_type: str
    reason: str
    #: The model's own ordering, before :mod:`app.mapping.ordering` re-sorted it.
    llm_rank: int | None = None


@dataclass
class ProposedMapping:
    each_to_grams: float | None = None
    needs_substitution: bool = False
    note: str = ""
    accepted: list[AcceptedProduct] = field(default_factory=list)


def _candidate_line(c: Candidate) -> dict:
    pack = c.pack_size_raw or (
        f"{c.pack_size_value:g}{c.pack_size_unit}" if c.pack_size_value and c.pack_size_unit else None
    )
    line = {"sku": c.sku, "name": c.name}
    if c.brand:
        line["brand"] = c.brand
    if pack:
        line["pack"] = pack
    if c.price is not None:
        line["price_gbp"] = round(c.price, 2)
    # The shelf price, matching what the order is computed from: the model is
    # asked which pack suits the ingredient, and a temporary half-price should
    # not make a 5 kg sack look like the sensible size.
    unit_price = ordering.sort_unit_price(c)
    if unit_price is not None and c.unit_price_basis:
        line["unit_price"] = f"£{unit_price:g}/{c.unit_price_basis}"
    if c.avg_rating is not None:
        line["rating"] = f"{c.avg_rating:g} ({c.ratings_count})"
    if c.is_frozen:
        line["storage_form"] = "frozen"
    return line


def build_prompt(ic: IngredientCandidates) -> tuple[str, str]:
    usage: dict = {"typical_recipe_line_count": ic.line_count}
    if ic.usage:
        u = ic.usage
        usage.update(
            metric_unit=u.metric_unit,
            median_amount=u.median,
            p25_amount=u.p25,
            p75_amount=u.p75,
            common_native_amounts=u.common_native_amounts,
        )
    payload = {
        "ingredient": ic.name,
        "usage": usage,
        "candidates": [_candidate_line(c) for c in ic.candidates],
    }
    user = (
        "Ingredient and candidate products (choose and rank the good matches):\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )
    return SYSTEM_PROMPT, user


def parse_proposal(raw: dict, ic: IngredientCandidates) -> ProposedMapping:
    valid_skus = {c.sku for c in ic.candidates}
    by_sku = {c.sku: c for c in ic.candidates}
    ingredient_requests_frozen = bool(re.search(r"\bfrozen\b", ic.name, re.I))
    accepted: list[AcceptedProduct] = []
    seen: set[str] = set()
    for entry in raw.get("accepted") or []:
        sku = str(entry.get("sku", ""))
        if sku not in valid_skus or sku in seen:
            continue  # drop hallucinated or duplicate skus
        seen.add(sku)
        match_type = entry.get("match_type")
        if match_type not in MATCH_TYPES:
            match_type = "exact"
        reason = str(entry.get("reason") or "")
        if by_sku[sku].is_frozen and not ingredient_requests_frozen:
            # This is catalogue fact, not model judgement. Without the guard a
            # model can still call frozen peas exact even though the Frozen chip
            # never made it into its natural-language reasoning.
            match_type = "form_differs"
            if "frozen" not in reason.lower():
                reason = f"{reason.rstrip('.')} — frozen form" if reason else "Frozen form"
        accepted.append(
            AcceptedProduct(
                sku=sku,
                rank=int(entry.get("rank") or (len(accepted) + 1)),
                match_type=match_type,
                reason=reason,
            )
        )
    # Normalise the model's ordering to 1..n and keep it, then let the
    # deterministic pass decide the order the products are actually offered in.
    accepted.sort(key=lambda a: a.rank)
    for i, a in enumerate(accepted, start=1):
        a.llm_rank = i
    accepted = ordering.order_accepted(accepted, ic.candidates)
    for i, a in enumerate(accepted, start=1):
        a.rank = i
    each = raw.get("each_to_grams")
    return ProposedMapping(
        each_to_grams=float(each) if isinstance(each, (int, float)) else None,
        needs_substitution=bool(raw.get("needs_substitution")),
        note=str(raw.get("note") or ""),
        accepted=accepted,
    )


def propose_one(ic: IngredientCandidates, complete: Completer) -> ProposedMapping:
    system, user = build_prompt(ic)
    raw = complete(system, user, PROPOSAL_SCHEMA)
    return parse_proposal(raw, ic)


@dataclass
class ProposeResult:
    proposed: int = 0
    skipped: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


def run_propose(
    session_factory: sessionmaker[Session],
    *,
    limit: int | None = None,
    only_missing: bool = False,
    force: bool = False,
    model: str | None = None,
    csv_path: Path | None = None,
    complete: Completer | None = None,
    retailer: str = DEFAULT_RETAILER,
) -> ProposeResult:
    """Propose mappings for one retailer's candidate pool.

    Mappings are per-shop rows, so this is run once per retailer: the same
    ingredient needs its own proposal against each catalogue, and being mapped at
    one shop is not a reason to skip it at another.
    """
    if complete is None:
        from app.mapping.openai_client import OpenAIJSONClient

        complete = OpenAIJSONClient(model=model)
        model_name = complete.model
    else:
        model_name = model or config.OPENAI_MODEL

    result = ProposeResult()
    with session_factory() as session:
        worklist = iter_worklist(session, csv_path=csv_path, limit=limit, retailer=retailer)
        existing = service.existing_mapping_keys(session, retailer)

    # By default (and with --only-missing) skip ingredients already mapped;
    # --force re-proposes and overwrites them.
    to_process = [ic for ic in worklist if force or ic.ingredient_key not in existing]
    result.skipped = len(worklist) - len(to_process)
    total = len(to_process)
    log.info(
        "propose[%s]: %d in worklist, %d to process, %d already mapped (skipped); model=%s",
        retailer, len(worklist), total, result.skipped, model_name,
    )

    for i, ic in enumerate(to_process, start=1):
        try:
            proposed = propose_one(ic, complete)
        except Exception as exc:  # noqa: BLE001 - one bad ingredient must not abort the run
            result.errors += 1
            result.notes.append(f"{ic.name}: {exc}")
            log.warning("[%d/%d] %s -> ERROR: %s", i, total, ic.name, exc)
            continue
        with session_factory() as session:
            service.write_proposal(
                session, ic, proposed, model=model_name, retailer=retailer
            )
        result.proposed += 1
        flags = []
        if proposed.each_to_grams is not None:
            flags.append(f"{proposed.each_to_grams:g}g/ea")
        if proposed.needs_substitution:
            flags.append("needs-sub")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        log.info(
            "[%d/%d] %-28s %d/%d accepted%s",
            i, total, ic.name[:28], len(proposed.accepted), len(ic.candidates), suffix,
        )
    log.info(
        "propose done: %d proposed, %d skipped, %d errors",
        result.proposed, result.skipped, result.errors,
    )
    return result
