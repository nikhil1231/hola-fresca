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

    if args.command == "status":
        counts = pipeline.status_counts(source, session_factory)
        print(f"source: {source.name}")
        print("  scrape_state:")
        for status, count in sorted(counts["states"].items()):
            print(f"    {status:<12} {count}")
        print(f"  recipes stored : {counts['recipes']}")
        print(f"  complete       : {counts['complete']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
