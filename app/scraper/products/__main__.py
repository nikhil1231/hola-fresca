"""CLI for retailer product-cache scraping.

    python -m app.scraper.products --retailer ocado discover --limit 10
    python -m app.scraper.products --retailer ocado fetch --limit 10
    python -m app.scraper.products --retailer ocado normalize
    python -m app.scraper.products --retailer ocado status
"""
from __future__ import annotations

import argparse
import sys

from app import config
from app.db.session import init_db, make_engine, make_session_factory
from app.scraper.products import pipeline
from app.scraper.ratelimit import AdaptiveThrottle


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.scraper.products")
    parser.add_argument("--retailer", default="ocado", help="retailer adapter name")
    parser.add_argument("--workers", type=int, default=1, help="reserved; Ocado defaults to 1")
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

    sub.add_parser("status", help="print product-cache status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.retailer != "ocado":
        print("unknown retailer; known: ocado", file=sys.stderr)
        return 2

    config.ensure_dirs()
    engine = make_engine()
    init_db(engine)
    session_factory = make_session_factory(engine)

    if args.command == "discover":
        res = pipeline.discover(session_factory, limit=args.limit)
        print(f"discover: {'; '.join(res.notes)}")
    elif args.command == "fetch":
        throttle = AdaptiveThrottle(workers=args.workers, delay=1.5, max_delay=20.0)
        res = pipeline.fetch(
            session_factory,
            limit=args.limit,
            retry_errors=args.retry_errors,
            headless=args.headless,
            throttle=throttle,
        )
        print(f"fetch: {res.fetched} fetched, {res.errors} errors")
    elif args.command == "normalize":
        res = pipeline.normalize(session_factory, limit=args.limit, force=args.force)
        print(
            f"normalize: {res.normalized} products normalized, "
            f"{res.hits} search hits linked, {res.errors} errors"
        )
    elif args.command == "status":
        counts = pipeline.status_counts(session_factory)
        print("retailer: ocado")
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
