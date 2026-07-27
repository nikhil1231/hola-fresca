"""Consistent whole-database snapshots for offsite sync.

Copying ``holafresca.db`` with ``cp`` — or letting a folder-sync client watch it —
can capture a torn file: SQLite writes in pages, and a copy taken mid-transaction
holds some of them from before the write and some from after. In WAL mode the
committed state is split across the ``-wal`` sidecar too, so the main file alone
is not the database.

``VACUUM INTO`` avoids all of that. It runs inside a read transaction and writes
a fresh, fully-consistent, already-compacted database to a new path, safely while
the API is serving. The snapshot is then gzipped — the file is mostly text
indexes and compresses several-fold, which matters when a week of them syncs to a
cloud drive.

The snapshot is the coarse half of the backup story. Most of what it holds is
rebuildable from ``data/raw/``; the irreplaceable part is the mapping decisions,
which also go to git via :mod:`app.backup.exports` because a binary blob in a
cloud folder is a poor place to keep something worth reviewing by hand.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app import config

DEFAULT_DEST = Path.home() / "backups" / "holafresca"
# Daily snapshots, kept for a week. Long enough to notice damage and roll back,
# short enough that a few hundred MB apiece stays reasonable in a cloud drive.
DEFAULT_KEEP = 7
_STEM = "holafresca"
_SUFFIX = ".db.gz"


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    bytes_written: int
    source_bytes: int

    @property
    def ratio(self) -> float:
        return self.bytes_written / self.source_bytes if self.source_bytes else 0.0


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def take_snapshot(
    db_path: Path | None = None,
    dest_dir: Path | None = None,
    *,
    compress: bool = True,
) -> Snapshot:
    """Write a consistent copy of the database to ``dest_dir``."""
    source = db_path or config.DB_PATH
    if not source.exists():
        raise FileNotFoundError(f"no database at {source}")
    dest = dest_dir or DEFAULT_DEST
    dest.mkdir(parents=True, exist_ok=True)

    stamp = _timestamp()
    raw = dest / f"{_STEM}-{stamp}.db"
    # VACUUM INTO refuses to overwrite, so a stale file from a failed run would
    # otherwise wedge every future snapshot at the same second.
    raw.unlink(missing_ok=True)

    # Read-only on the source: a snapshot must never be able to damage the thing
    # it is protecting. VACUUM INTO writes to `raw`, not through this handle.
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as conn:
        conn.execute("VACUUM INTO ?", (str(raw),))

    if not compress:
        return Snapshot(path=raw, bytes_written=raw.stat().st_size, source_bytes=source.stat().st_size)

    final = dest / f"{_STEM}-{stamp}{_SUFFIX}"
    with raw.open("rb") as src, gzip.open(final, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
    raw.unlink()
    return Snapshot(
        path=final,
        bytes_written=final.stat().st_size,
        source_bytes=source.stat().st_size,
    )


def prune(dest_dir: Path | None = None, keep: int = DEFAULT_KEEP) -> list[Path]:
    """Delete all but the ``keep`` newest snapshots. Returns what was removed."""
    dest = dest_dir or DEFAULT_DEST
    if not dest.is_dir():
        return []
    # Names are timestamped in a sortable format, so lexical order is age order —
    # no reliance on mtimes, which a cloud-sync client may rewrite.
    existing = sorted(
        [p for p in dest.iterdir() if p.name.startswith(f"{_STEM}-") and p.suffix in (".gz", ".db")]
    )
    stale = existing[:-keep] if keep > 0 else []
    for path in stale:
        path.unlink()
    return stale
