"""One-shot HelloFresh commands: the weekly auto-skip, and a look at what it would do.

    python -m app.hellofresh autoskip --dry-run
    python -m app.hellofresh autoskip

Run by ``~/infra/stacks/holafresca/autoskip`` on a Sunday-night timer. Exits
non-zero when anything failed, so a run that cannot reach HelloFresh or sign in
is reported rather than passing quietly as "nothing to do".
"""
from __future__ import annotations

import argparse
import logging
import sys

from app.db.session import init_db, make_engine, make_session_factory
from app.hellofresh import autoskip


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.hellofresh")
    sub = parser.add_subparsers(dest="command", required=True)
    p_skip = sub.add_parser("autoskip", help="skip every delivery week not yet settled")
    p_skip.add_argument("--dry-run", action="store_true",
                        help="say what would be skipped, change nothing, record nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    engine = make_engine()
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        report = autoskip.run(session, dry_run=args.dry_run)

    for line in report.lines():
        print(line)
    verb = "would skip" if report.dry_run else "skipped"
    print(f"autoskip: {verb} {len(report.skipped)}, "
          f"{len(report.decided) - len(report.skipped)} already settled, "
          f"{len(report.resurfaced)} running again, {len(report.errors)} errors")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
