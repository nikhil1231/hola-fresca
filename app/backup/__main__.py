"""CLI for backups.

    python -m app.backup export-mappings [--out DIR] [--db PATH]
    python -m app.backup snapshot [--dest DIR] [--keep N] [--no-compress]
    python -m app.backup status [--dest DIR]

``export-mappings`` writes the human review decisions to ``exports/`` for git;
``snapshot`` writes a consistent gzipped copy of the whole database for offsite
sync and prunes old ones. Both read the database read-only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import config
from app.backup import exports as exports_mod
from app.backup import snapshot as snapshot_mod


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.backup")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export-mappings", help="write mapping decisions to CSV for git")
    p_export.add_argument("--out", type=Path, default=None)
    p_export.add_argument("--db", type=Path, default=None)

    p_snap = sub.add_parser("snapshot", help="consistent gzipped copy of the whole database")
    p_snap.add_argument("--dest", type=Path, default=None)
    p_snap.add_argument("--db", type=Path, default=None)
    p_snap.add_argument("--keep", type=int, default=snapshot_mod.DEFAULT_KEEP)
    p_snap.add_argument("--no-compress", action="store_true")

    p_status = sub.add_parser("status", help="what backups exist and how old they are")
    p_status.add_argument("--dest", type=Path, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "export-mappings":
        results = exports_mod.export_all(db_path=args.db, out_dir=args.out)
        for result in results:
            rel = result.path.relative_to(config.ROOT_DIR) if result.path.is_relative_to(config.ROOT_DIR) else result.path
            print(f"{result.rows:>6} rows  {len(result.columns):>2} cols  {rel}")
        return 0

    if args.command == "snapshot":
        snap = snapshot_mod.take_snapshot(
            db_path=args.db, dest_dir=args.dest, compress=not args.no_compress
        )
        print(
            f"wrote {snap.path} ({_human(snap.bytes_written)} "
            f"from {_human(snap.source_bytes)}, {snap.ratio:.0%})"
        )
        for stale in snapshot_mod.prune(dest_dir=args.dest, keep=args.keep):
            print(f"pruned {stale.name}")
        return 0

    if args.command == "status":
        dest = args.dest or snapshot_mod.DEFAULT_DEST
        if not dest.is_dir():
            print(f"no snapshot directory at {dest}")
            return 1
        files = sorted(p for p in dest.iterdir() if p.name.startswith("holafresca-"))
        if not files:
            print(f"no snapshots in {dest}")
            return 1
        for path in files:
            print(f"  {_human(path.stat().st_size):>8}  {path.name}")
        print(f"{len(files)} snapshot(s) in {dest}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
