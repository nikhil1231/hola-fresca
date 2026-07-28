"""CLI for the offline analyses.

    python -m app.analysis mass-balance [--write] [--all]

``mass-balance`` grades every per-unit gram constant against HelloFresh's own
stated serving weights and reports where ours disagree. It only reads; ``--write``
refreshes the review queue at ``exports/gram_suggestions.csv``, preserving any
``status`` you have already filled in.
"""
from __future__ import annotations

import argparse
import sys

from app.analysis import mass_balance


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.analysis")
    sub = parser.add_subparsers(dest="command", required=True)

    p_mb = sub.add_parser(
        "mass-balance", help="grade gram constants against stated serving weights"
    )
    p_mb.add_argument(
        "--write", action="store_true", help=f"refresh {mass_balance.EXPORT_PATH.name}"
    )
    p_mb.add_argument(
        "--all", action="store_true",
        help="ignore the disagreement floor and list everything above the evidence floors",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "mass-balance":
        if args.all:
            mass_balance.MIN_DISAGREEMENT = 0.0
        items, baseline = mass_balance.suggestions()
        print(
            f"corpus baseline {baseline:.3f} "
            f"(divided out of every figure below — see the module docstring)"
        )
        print(
            f"whole-recipe weights average {items[0].corpus_recipe_ratio:.2f} of stated "
            f"serving weight; an ingredient whose own recipes match that has no missing "
            f"mass to explain and is marked artefact?\n" if items else ""
        )
        print(
            f"{len(items)} ingredient(s) disagree by more than "
            f"{mass_balance.MIN_DISAGREEMENT:.0%}, at >={mass_balance.MIN_MASS_SHARE:.0%} "
            f"mass share and >={mass_balance.MIN_RECIPES} recipes\n"
        )
        if items:
            print(
                f"  {'ingredient':<32}{'unit':<12}{'now':>7}{'->':>8}{'x':>7}"
                f"{'n':>6}{'share':>7}{'recipes':>9}"
            )
        for s in items:
            flag = "  artefact?" if s.artefact else ""
            print(
                f"  {s.name[:30]:<32}{s.unit:<12}{s.current_g:>7.0f}"
                f"{s.suggested_g:>8.0f}{s.multiplier:>7.2f}{s.recipes:>6}{s.mass_share:>7.0%}"
                f"{s.recipe_ratio:>9.2f}{flag}"
            )
        if args.write:
            path = mass_balance.write_csv(items)
            print(f"\nwrote {path} — set status=approved on the rows you accept")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
