"""CLI for the ingredient→product mapping.

    python -m app.mapping propose [--limit N] [--only-missing] [--force] [--model M]
    python -m app.mapping --retailer sainsburys propose
    python -m app.mapping reorder
    python -m app.mapping status
    python -m app.mapping coverage [--include-proposed]
    python -m app.mapping basket <recipe_id> [<recipe_id> ...] [--include-proposed]

``propose`` calls OpenAI (key from the repo-root .env) and writes proposed
mappings; nothing downstream trusts them until approved in the review UI. The
order those products are offered in is computed, not asked for (see
:mod:`app.mapping.ordering`), so ``reorder`` re-applies a change to that balance
across every untouched proposal without spending anything.

Every subcommand is scoped to one retailer, defaulting to the app's own default.
Mappings are per-shop rows — an ingredient approved at Ocado says nothing about
Sainsbury's — so ``propose`` is run once per shop, and without ``--retailer`` it
will report every ingredient as already mapped once the default shop is done.
"""
from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import func, select

from app.db.session import init_db, make_engine, make_session_factory
from app.mapping import coverage as coverage_mod
from app.mapping import propose as propose_mod
from app.db.models import IngredientMapping
from app.retailers import DEFAULT_RETAILER, RETAILER_IDS, is_known


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.mapping")
    parser.add_argument(
        "--retailer",
        default=DEFAULT_RETAILER,
        help=f"which shop to map for ({', '.join(RETAILER_IDS)})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_propose = sub.add_parser("propose", help="LLM proposal pass over the ingredient worklist")
    p_propose.add_argument("--limit", type=int, default=None)
    p_propose.add_argument("--only-missing", action="store_true")
    p_propose.add_argument("--force", action="store_true", help="re-propose already-mapped items")
    p_propose.add_argument("--model", default=None)

    p_gen = sub.add_parser("generate", help="add the next N ingredients to the review queue")
    p_gen.add_argument("--count", type=int, default=10)
    p_gen.add_argument("--model", default=None)

    sub.add_parser(
        "reorder",
        help="re-sort proposed products under the current ordering rules (no LLM calls)",
    )

    sub.add_parser("status", help="counts by mapping status")

    p_cov = sub.add_parser("coverage", help="share of curated-recipe lines that resolve")
    p_cov.add_argument("--include-proposed", action="store_true")

    p_basket = sub.add_parser("basket", help="itemised priced basket for some recipes")
    p_basket.add_argument("recipe_ids", nargs="+", type=int)
    p_basket.add_argument("--include-proposed", action="store_true")
    p_basket.add_argument(
        "--include-staples", action="store_true",
        help="also shop for pantry staples (salt, oil, ...) instead of assuming them owned",
    )

    return parser


def _statuses(include_proposed: bool) -> tuple[str, ...]:
    return ("approved", "proposed") if include_proposed else ("approved",)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not is_known(args.retailer):
        print(
            f"unknown retailer {args.retailer!r}; known: {', '.join(RETAILER_IDS)}",
            file=sys.stderr,
        )
        return 2
    retailer = args.retailer
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # The OpenAI SDK logs one HTTP line per request at INFO; keep progress readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    engine = make_engine()
    init_db(engine)
    session_factory = make_session_factory(engine)

    if args.command == "propose":
        try:
            res = propose_mod.run_propose(
                session_factory,
                limit=args.limit,
                only_missing=args.only_missing,
                force=args.force,
                model=args.model,
                retailer=retailer,
            )
        except Exception as exc:  # noqa: BLE001 - config/auth errors should print cleanly
            print(f"propose failed: {exc}", file=sys.stderr)
            return 1
        print(f"propose: {res.proposed} proposed, {res.skipped} skipped, {res.errors} errors")
        for note in res.notes[:10]:
            print(f"  ! {note}")

    elif args.command == "generate":
        from app.mapping import generate as generate_mod

        try:
            job = generate_mod.generate(
                session_factory, count=args.count, model=args.model, retailer=retailer
            )
        except Exception as exc:  # noqa: BLE001 - config/auth errors should print cleanly
            print(f"generate failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"generate: {job.added} proposed, {job.staples} pantry staples, "
            f"{job.no_match} no-match, {job.errors} errors"
        )

    elif args.command == "reorder":
        from app.mapping import service as service_mod

        with session_factory() as session:
            changed = service_mod.reorder_proposals(session, retailer=retailer)
        print(f"reorder: {changed} mapping(s) re-ordered ({retailer})")

    elif args.command == "status":
        with session_factory() as session:
            rows = session.execute(
                select(IngredientMapping.status, func.count())
                .where(IngredientMapping.retailer == retailer)
                .group_by(IngredientMapping.status)
            ).all()
        print(f"ingredient mappings ({retailer}):")
        for status, count in sorted(rows):
            print(f"  {status:<14} {count}")

    elif args.command == "coverage":
        rep = coverage_mod.coverage_report(
            session_factory, statuses=_statuses(args.include_proposed), retailer=retailer
        )
        print(
            f"coverage: {rep.lines_resolved}/{rep.lines_total} curated ingredient lines "
            f"resolved ({rep.pct:.1f}%)"
        )
        print(f"  ingredient groups resolved: {rep.resolved_keys}/{rep.distinct_keys}")
        print("  top unresolved (by line frequency):")
        for key, count in rep.top_unresolved:
            print(f"    {count:5}  {key}")

    elif args.command == "basket":
        basket = coverage_mod.build_basket(
            session_factory,
            args.recipe_ids,
            statuses=_statuses(args.include_proposed),
            include_staples=args.include_staples,
            retailer=retailer,
        )
        print(f"basket for recipes {args.recipe_ids}:")
        for line in basket.lines:
            detail = (
                f"{line.packs}x {line.product_name} "
                f"({line.pack_size_value:g}{line.pack_size_unit}) "
                # leftover_g is always grams, even when the pack is sold by count.
                f"= £{line.line_cost:.2f}, {line.leftover_g:g}g left"
                if line.line_cost is not None
                else f"{line.product_name or '—'} [{line.note}]"
            )
            print(f"  {line.name:<28} need {line.need_g:g}g -> {detail}")
        if basket.staples:
            print(f"  assumed in cupboard: {', '.join(basket.staples)}")
        if basket.unmapped:
            print(f"  unmapped: {', '.join(basket.unmapped)}")
        print(f"  estimated total: £{basket.total:.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
