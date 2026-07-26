"""CLI for the planner's basket engine.

    python -m app.planner basket <recipe_id>[:servings] ... [--include-staples]
    python -m app.planner waste-model

``basket`` prices a chosen week and shows where the money goes and where it is
thrown away. ``waste-model`` prints the salvage assumptions applied to the
approved mappings, which is how the perishability model gets sanity-checked
against real products rather than in the abstract.
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from app.db.session import init_db, make_engine, make_session_factory
from app.planner import waste as waste_mod
from app.planner.basket import Selection, build_basket
from app.planner.index import load_index


def _selection(token: str) -> Selection:
    recipe_id, _, servings = token.partition(":")
    return Selection(recipe_id=int(recipe_id), servings=int(servings) if servings else None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.planner")
    sub = parser.add_subparsers(dest="command", required=True)

    p_basket = sub.add_parser("basket", help="priced, waste-scored basket for some recipes")
    p_basket.add_argument(
        "selections",
        nargs="+",
        type=_selection,
        metavar="RECIPE_ID[:SERVINGS]",
        help="recipe ids, optionally with servings (default: the recipe's base yield)",
    )
    p_basket.add_argument("--include-proposed", action="store_true")
    p_basket.add_argument(
        "--include-staples",
        action="store_true",
        help="shop for pantry staples instead of assuming them owned",
    )

    sub.add_parser("waste-model", help="salvage fractions applied across approved mappings")
    return parser


def _statuses(include_proposed: bool) -> tuple[str, ...]:
    return ("approved", "proposed") if include_proposed else ("approved",)


def _print_basket(index, args) -> None:
    basket = build_basket(index, args.selections, include_staples=args.include_staples)

    print("plan:")
    for selection in args.selections:
        recipe = index.recipes.get(selection.recipe_id)
        if recipe is None:
            print(f"  ! recipe {selection.recipe_id} not found")
            continue
        servings = selection.servings or recipe.base_yield
        print(f"  [{recipe.id}] {recipe.name} — {servings} servings")

    def _print_lines(lines) -> None:
        for line in lines:
            if line.cover is None:
                print(f"  {line.name:<34} {line.need_g:>8.0f}g   {line.note}")
                continue
            cover = line.cover
            print(
                f"  {line.name:<34} {line.need_g:>8.0f}g  "
                f"£{cover.cost:>6.2f}  {cover.describe():<22} "
                f"leftover {cover.leftover_g:>7.0f}g  waste £{cover.waste_gbp:>5.2f}"
            )
            print(f"  {'':<34} {cover.choices[0].pack.product_name}")

    print("\nbasket:")
    _print_lines(basket.retailer_lines)
    external = basket.external_lines
    if external:
        spend = sum(line.cost for line in external)
        print(f"\nsource elsewhere (£{spend:.2f}, not part of the online order):")
        _print_lines(external)

    print(f"\n  spend            £{basket.cost:.2f}")
    print(f"  waste (valued)   £{basket.waste_gbp:.2f}", end="")
    if basket.cost:
        print(f"  ({100 * basket.waste_gbp / basket.cost:.0f}% of spend)")
    else:
        print()
    print(f"  planner score    £{basket.score:.2f}")
    trace = basket.trace_lines
    if trace:
        spend = sum(line.cost for line in trace)
        print(
            f"\n  {len(trace)} trace-demand lines cost £{spend:.2f} "
            f"({100 * spend / basket.cost:.0f}% of spend) — a whole pack for a few grams."
        )
        print("  These are pantry-staple candidates, not real weekly shopping:")
        for line in sorted(trace, key=lambda line: line.cost, reverse=True):
            print(f"    {line.name:<32} needs {line.need_g:>5.1f}g  costs £{line.cost:.2f}")
    if basket.staples:
        print(f"\n  assumed in cupboard: {', '.join(basket.staples)}")
    if basket.unmapped:
        print(f"  unmapped: {', '.join(basket.unmapped)}")
    if basket.unpriceable:
        print(f"  mapped but not priceable: {', '.join(basket.unpriceable)}")
    if basket.untracked_lines:
        print(f"  ingredient lines contributing no demand: {basket.untracked_lines}")


def _print_waste_model(index) -> None:
    buckets: Counter[str] = Counter()
    for ingredient in index.ingredients.values():
        for pack in ingredient.packs:
            buckets[f"{pack.salvage:.2f}"] += 1
    print("salvage fraction distribution over shoppable packs:")
    for value, count in sorted(buckets.items()):
        print(f"  {value}  {count:>5} packs")
    print(f"\nreplan horizon: {waste_mod.REPLAN_HORIZON_DAYS} days")
    print("most perishable shoppable ingredients (salvage 0.00, by pack count):")
    perishable = [
        i for i in index.ingredients.values() if i.packs and all(p.salvage == 0.0 for p in i.packs)
    ]
    for ingredient in sorted(perishable, key=lambda i: i.name)[:20]:
        print(f"  {ingredient.name}")
    print(f"  ({len(perishable)} ingredients where nothing is salvageable)")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = make_engine()
    init_db(engine)
    session_factory = make_session_factory(engine)

    if args.command == "basket":
        index = load_index(
            session_factory,
            statuses=_statuses(args.include_proposed),
            recipe_ids=[s.recipe_id for s in args.selections],
            curated_only=False,
        )
        _print_basket(index, args)
    elif args.command == "waste-model":
        index = load_index(session_factory, recipe_ids=[])
        _print_waste_model(index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
