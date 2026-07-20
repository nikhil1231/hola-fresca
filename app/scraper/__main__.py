"""Command-line entry point for the scrape pipeline.

    python -m app.scraper discover
    python -m app.scraper fetch --limit 50
    python -m app.scraper normalize
    python -m app.scraper run --limit 50        # all three stages
    python -m app.scraper status

The scraper is an offline job that writes into the same SQLite database the
service reads; it never runs inside the API process.
"""
from __future__ import annotations

import argparse
import logging
import sys

from app import config
from app.db.session import init_db, make_engine, make_session_factory
from app.scraper import pipeline
from app.scraper.ratelimit import AdaptiveThrottle
from app.scraper.sources import SOURCES


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.scraper", description="HolaFresca recipe scraper")
    parser.add_argument("--source", default="hellofresh", help="source adapter name")
    parser.add_argument("--workers", type=int, default=24, help="concurrent fetch workers")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="enumerate recipe URLs from the source sitemap")

    p_fetch = sub.add_parser("fetch", help="download and cache raw recipe payloads")
    p_fetch.add_argument("--limit", type=int, default=None)
    p_fetch.add_argument("--retry-errors", action="store_true")

    p_norm = sub.add_parser("normalize", help="parse cached payloads into the database")
    p_norm.add_argument("--force", action="store_true", help="re-normalize already-done recipes")

    p_run = sub.add_parser("run", help="discover, fetch and normalize in sequence")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--retry-errors", action="store_true")
    p_run.add_argument("--force", action="store_true")

    sub.add_parser(
        "enrich",
        help="backfill units, convert amounts to grams, compute diet/macro/ratio fields",
    )

    p_cur = sub.add_parser("curate", help="flag the active library (Profile A by default)")
    p_cur.add_argument("--min-ratings", type=int, default=25)
    p_cur.add_argument("--min-stars", type=float, default=0.0)
    p_cur.add_argument("--since-year", type=int, default=None)
    p_cur.add_argument("--keep-addons", action="store_true", help="do not drop add-on items")
    p_cur.add_argument("--no-dedup", action="store_true", help="keep all versions of a dish")
    p_cur.add_argument(
        "--recent-days", type=int, default=120,
        help="recipes newer than this need only --recent-min-ratings (0 disables)",
    )
    p_cur.add_argument("--recent-min-ratings", type=int, default=3)

    sub.add_parser("status", help="print pipeline state counts")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)

    source = SOURCES.get(args.source)
    if source is None:
        print(f"unknown source '{args.source}'; known: {', '.join(SOURCES)}", file=sys.stderr)
        return 2

    config.ensure_dirs()
    engine = make_engine()
    init_db(engine)
    session_factory = make_session_factory(engine)
    throttle = AdaptiveThrottle(workers=args.workers)

    if args.command in ("discover", "run"):
        res = pipeline.discover(source, session_factory)
        print(f"discover: {'; '.join(res.notes)}")

    if args.command in ("fetch", "run"):
        res = pipeline.fetch(
            source,
            session_factory,
            limit=args.limit,
            retry_errors=args.retry_errors,
            throttle=throttle,
        )
        note = f"fetch: {res.fetched} fetched, {res.errors} errors"
        if res.stopped_early:
            note += " [STOPPED EARLY]"
        print(note)
        for n in res.notes:
            print(f"  {n}")

    if args.command in ("normalize", "run"):
        res = pipeline.normalize(source, session_factory, force=getattr(args, "force", False))
        print(
            f"normalize: {res.normalized} recipes "
            f"({res.incomplete} incomplete stubs, {res.errors} errors)"
        )

    if args.command == "enrich":
        from app.scraper.enrich import enrich

        rep = enrich(session_factory)
        print(
            f"enrich: {rep.recipes} recipes; {rep.units_backfilled} units backfilled; "
            f"{rep.ingredients_gram_resolved}/{rep.ingredients_total} ingredient lines "
            f"resolved to grams "
            f"({100 * rep.ingredients_gram_resolved / max(rep.ingredients_total, 1):.0f}%)"
        )

    if args.command == "curate":
        from app.scraper.curate import CurationRules, curate

        rules = CurationRules(
            min_ratings=args.min_ratings,
            min_avg_rating=args.min_stars,
            since_year=args.since_year,
            drop_addons=not args.keep_addons,
            dedup_by_name=not args.no_dedup,
            recent_days=args.recent_days,
            recent_min_ratings=args.recent_min_ratings,
        )
        rep = curate(session_factory, source.name, rules)
        print(
            f"curate: {rep.curated} of {rep.total} recipes flagged as active library "
            f"({rep.kept_recent} kept via recency exception)"
        )
        print(
            "  cut: "
            f"{rep.cut_incomplete} incomplete, {rep.cut_bundle} bundles, "
            f"{rep.cut_low_kcal} low-kcal, {rep.cut_suspect} bad-macros, "
            f"{rep.cut_addon} add-ons, {rep.cut_unrated} under-rated, "
            f"{rep.cut_low_stars} low-stars, {rep.cut_old} too-old, "
            f"{rep.cut_dup} duplicate versions"
        )

    if args.command == "status":
        counts = pipeline.status_counts(source, session_factory)
        print(f"source: {source.name}")
        print("  scrape_state:")
        for status, count in sorted(counts["states"].items()):
            print(f"    {status:<12} {count}")
        print(f"  recipes stored : {counts['recipes']}")
        print(f"  complete       : {counts['complete']}")
        print(f"  curated (active): {counts['curated']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
