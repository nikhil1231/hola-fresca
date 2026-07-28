"""CLI for retailer product-cache scraping.

    python -m app.scraper.products --retailer ocado discover --limit 10
    python -m app.scraper.products --retailer ocado fetch --limit 10
    python -m app.scraper.products --retailer ocado normalize
    python -m app.scraper.products --retailer ocado status

Seasoned Pioneers is catalogue-first and reads a committed snapshot rather than
the network, so it has one sync command instead of discover/fetch/normalize:

    python -m app.scraper.products --retailer seasoned_pioneers sync
    python -m app.scraper.products --retailer seasoned_pioneers status
    python -m app.scraper.products --retailer seasoned_pioneers refresh --from FILE
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app import config
from app.db.session import init_db, make_engine, make_session_factory
from app.scraper.products import catalogue, pipeline, seasoned_pioneers
from app.scraper.products.seasoned_pioneers import RETAILER as SP_RETAILER
from app.scraper.ratelimit import AdaptiveThrottle

RETAILERS = ("ocado", SP_RETAILER)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.scraper.products")
    parser.add_argument(
        "--retailer", default="ocado", choices=RETAILERS, help="retailer adapter name"
    )
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

    sub.add_parser(
        "backfill-shelf-life",
        help="re-derive shelf life from cached raw payloads (no re-fetch)",
    )
    sub.add_parser("status", help="print product-cache status")

    p_sync = sub.add_parser(
        "sync", help="load a catalogue snapshot into products (seasoned_pioneers)"
    )
    p_sync.add_argument(
        "--path", type=Path, default=None, help="snapshot to sync instead of the committed one"
    )

    p_refresh = sub.add_parser(
        "refresh", help="replace the committed catalogue snapshot (seasoned_pioneers)"
    )
    p_refresh.add_argument(
        "--from",
        dest="source",
        type=Path,
        required=True,
        help="captured catalogue JSON: {'products': [...]} with a size_raw on each",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    config.ensure_dirs()
    engine = make_engine()
    init_db(engine)
    session_factory = make_session_factory(engine)

    if args.retailer == SP_RETAILER:
        return _run_catalogue(args, session_factory)

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
    elif args.command == "backfill-shelf-life":
        res = pipeline.backfill_shelf_life(session_factory)
        print(
            f"backfill-shelf-life: {res.normalized} of {res.products} products "
            f"have a stated shelf life, {res.errors} errors"
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
        life_pct = 100 * counts["shelf_life"] / max(products, 1)
        print(f"  shelf life      : {counts['shelf_life']} ({life_pct:.1f}%)")

    return 0


def _run_catalogue(args, session_factory) -> int:
    """Snapshot-backed stages, for retailers whose whole shop fits in one file."""
    if args.command in ("discover", "fetch", "normalize", "backfill-shelf-life"):
        # Deliberately not aliased onto `sync`: these name a network pipeline this
        # retailer does not have, and silently doing something else would hide that.
        print(
            f"{args.command}: not applicable to {SP_RETAILER}, which reads a committed "
            "catalogue snapshot — use `sync` (or `refresh --from FILE` to replace it)",
            file=sys.stderr,
        )
        return 2

    if args.command == "sync":
        try:
            res = catalogue.sync(session_factory, path=args.path)
        except ValueError as exc:
            print(f"sync: {exc}", file=sys.stderr)
            return 1
        print(f"sync: {'; '.join(res.notes)}, {res.errors} errors")
    elif args.command == "refresh":
        try:
            with args.source.open(encoding="utf-8") as fh:
                captured = json.load(fh)
            written = seasoned_pioneers.write_snapshot(captured)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"refresh: could not read {args.source}: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"refresh: {exc}", file=sys.stderr)
            return 1
        print(
            f"refresh: {written} products written to "
            f"{seasoned_pioneers.CATALOGUE_PATH}; review the diff, then run sync"
        )
    elif args.command == "status":
        counts = catalogue.status_counts(session_factory)
        products = counts["products"]
        pack_pct = 100 * counts["pack_parsed"] / max(products, 1)
        snapshot = counts["snapshot"]
        print(f"retailer: {SP_RETAILER}")
        if snapshot:
            print(
                f"  snapshot        : {snapshot.get('product_count')} products, "
                f"captured {snapshot.get('captured_at')}"
            )
        print("  product_scrape_state:")
        for status, count in sorted(counts["states"].items()):
            print(f"    {status:<12} {count}")
        print(f"  products cached : {products}")
        print(f"  pack parsed     : {counts['pack_parsed']} ({pack_pct:.1f}%)")
        print(f"  priced          : {counts['priced']}")
        print(f"  in stock        : {counts['in_stock']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
