"""CLI for retailer product-cache scraping.

    python -m app.scraper.products --retailer ocado discover --limit 10
    python -m app.scraper.products --retailer ocado fetch --limit 10
    python -m app.scraper.products --retailer ocado normalize
    python -m app.scraper.products --retailer sainsburys status

Each retailer keeps its own ``product_scrape_state`` rows, raw cache and browser
profile, so the stages can be run for one shop without disturbing another's
progress.
"""
from __future__ import annotations

import argparse
import sys

from app import config
from app.db.session import init_db, make_engine, make_session_factory
from app.retailers import DEFAULT_RETAILER
from app.scraper.products import pipeline
from app.scraper.products.registry import ADAPTER_IDS, has_adapter
from app.scraper.ratelimit import AdaptiveThrottle


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.scraper.products")
    parser.add_argument(
        "--retailer",
        default=DEFAULT_RETAILER,
        help=f"retailer adapter name ({', '.join(sorted(ADAPTER_IDS))})",
    )
    parser.add_argument("--workers", type=int, default=1, help="reserved; defaults to 1")
    parser.add_argument("--headless", action="store_true", help="run browser session headlessly")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="load ingredient terms into product state")
    p_discover.add_argument("--limit", type=int, default=250)

    p_fetch = sub.add_parser("fetch", help="fetch search and product payloads")
    p_fetch.add_argument("--limit", type=int, default=None)
    p_fetch.add_argument("--retry-errors", action="store_true")

    p_norm = sub.add_parser("normalize", help="normalize cached payloads into products")
    p_norm.add_argument("--limit", type=int, default=250)
    p_norm.add_argument("--force", action="store_true")

    sub.add_parser(
        "backfill-shelf-life",
        help="re-derive shelf life from cached raw payloads (no re-fetch)",
    )
    sub.add_parser("status", help="print product-cache status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not has_adapter(args.retailer):
        print(
            f"unknown retailer {args.retailer!r}; known: {', '.join(sorted(ADAPTER_IDS))}",
            file=sys.stderr,
        )
        return 2
    retailer = args.retailer

    config.ensure_dirs()
    engine = make_engine()
    init_db(engine)
    session_factory = make_session_factory(engine)

    if args.command == "discover":
        res = pipeline.discover(session_factory, limit=args.limit, retailer=retailer)
        print(f"discover: {'; '.join(res.notes)}")
    elif args.command == "fetch":
        throttle = AdaptiveThrottle(workers=args.workers, delay=1.5, max_delay=20.0)
        res = pipeline.fetch(
            session_factory,
            limit=args.limit,
            retry_errors=args.retry_errors,
            headless=args.headless,
            throttle=throttle,
            retailer=retailer,
        )
        print(f"fetch: {res.fetched} fetched, {res.errors} errors")
    elif args.command == "normalize":
        res = pipeline.normalize(
            session_factory, limit=args.limit, force=args.force, retailer=retailer
        )
        print(
            f"normalize: {res.normalized} products normalized, "
            f"{res.hits} search hits linked, {res.errors} errors"
        )
    elif args.command == "backfill-shelf-life":
        res = pipeline.backfill_shelf_life(session_factory, retailer=retailer)
        print(
            f"backfill-shelf-life: {res.normalized} of {res.products} products "
            f"have a stated shelf life, {res.errors} errors"
        )
    elif args.command == "status":
        counts = pipeline.status_counts(session_factory, retailer=retailer)
        print(f"retailer: {retailer}")
        print("  product_scrape_state:")
        for (kind, status), count in sorted(counts["states"].items()):
            print(f"    {kind:<8} {status:<12} {count}")
        products = counts["products"]
        pack_pct = 100 * counts["pack_parsed"] / max(products, 1)
        unit_pct = 100 * counts["unit_parsed"] / max(products, 1)
        print(f"  products cached : {products}")
        print(f"  search hits     : {counts['hits']}")
        print(f"  terms with hits : {counts['terms_with_hits']}")
        print(f"  pack parsed     : {counts['pack_parsed']} ({pack_pct:.1f}%)")
        print(f"  unit parsed     : {counts['unit_parsed']} ({unit_pct:.1f}%)")
        life_pct = 100 * counts["shelf_life"] / max(products, 1)
        print(f"  shelf life      : {counts['shelf_life']} ({life_pct:.1f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
