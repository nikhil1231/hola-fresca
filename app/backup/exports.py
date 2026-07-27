"""Export the ingredient→product mapping decisions to version-controlled CSV.

Everything else in the database is a derivative of ``data/raw/`` and can be
rebuilt by re-running the scrape/normalize/enrich stages. The mapping tables are
not: they hold hours of human review — which SKUs are acceptable for an
ingredient, which are rejected, what counts as a substitute — and that judgement
exists nowhere else. Losing the database means re-doing it by hand.

So these two tables get their own backup, in a format git can store and diff:

* Surrogate ``id`` columns are dropped. They are assigned by insertion order and
  change on every rebuild, which would churn the diff without carrying meaning.
  Rows are identified by their natural keys instead — ``(retailer,
  ingredient_key)`` for a mapping, plus ``sku`` for one of its products — both of
  which already carry a ``UniqueConstraint``.
* ``product_id`` is dropped for the same reason: it points into ``products``,
  which is rebuilt from the raw Ocado cache. ``sku`` is the retailer's own
  identifier and survives that rebuild, so a restore re-joins on it.
* Rows are sorted by natural key so an unchanged decision never moves, and a real
  edit shows up as a one-line diff.

Restoring is not implemented here: it needs a rebuilt ``products`` table to join
against, and writing to a live database is a much riskier operation than reading
from one. The CSVs carry everything required to do it.
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app import config

# Repo-root directory the CSVs are written to. Deliberately outside ``data/``,
# which is gitignored wholesale as rebuildable scrape output.
EXPORT_DIR = config.ROOT_DIR / "exports"

# Columns that identify a row rather than describe it, and the order they lead
# with in the CSV. Anything else the table happens to have follows, so a column
# added to the schema later is picked up without touching this file.
_MAPPING_KEY = ("retailer", "ingredient_key")
_PRODUCT_KEY = ("retailer", "ingredient_key", "sku")

# Never exported: surrogate keys and foreign keys into rebuildable tables.
_MAPPING_DROP = frozenset({"id"})
_PRODUCT_DROP = frozenset({"id", "mapping_id", "product_id"})


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    rows: int
    columns: tuple[str, ...]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _ordered(columns: list[str], key: tuple[str, ...], drop: frozenset[str]) -> list[str]:
    """Key columns first, then everything else in schema order."""
    rest = [c for c in columns if c not in drop and c not in key]
    return [c for c in key if c in columns] + rest


def _write(path: Path, columns: list[str], rows) -> ExportResult:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    # newline="" per the csv docs; an explicit \n terminator keeps the file
    # byte-identical across platforms so git sees no spurious changes.
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])
            count += 1
    return ExportResult(path=path, rows=count, columns=tuple(columns))


def export_mappings(conn: sqlite3.Connection, out_dir: Path) -> ExportResult:
    columns = _ordered(_columns(conn, "ingredient_mappings"), _MAPPING_KEY, _MAPPING_DROP)
    select = ", ".join(f'm."{c}"' for c in columns)
    rows = conn.execute(
        f"SELECT {select} FROM ingredient_mappings m ORDER BY m.retailer, m.ingredient_key"
    )
    return _write(out_dir / "ingredient_mappings.csv", columns, rows)


def export_mapping_products(conn: sqlite3.Connection, out_dir: Path) -> ExportResult:
    """Accepted/rejected products per mapping, re-keyed onto the parent's natural key."""
    own = [
        c
        for c in _columns(conn, "ingredient_mapping_products")
        if c not in _PRODUCT_DROP and c != "sku"
    ]
    columns = [*_PRODUCT_KEY, *own]
    select = ", ".join(
        ["m.retailer", "m.ingredient_key", "p.sku", *(f'p."{c}"' for c in own)]
    )
    rows = conn.execute(
        f"SELECT {select} FROM ingredient_mapping_products p "
        "JOIN ingredient_mappings m ON m.id = p.mapping_id "
        "ORDER BY m.retailer, m.ingredient_key, p.rank, p.sku"
    )
    return _write(out_dir / "ingredient_mapping_products.csv", columns, rows)


def export_all(db_path: Path | None = None, out_dir: Path | None = None) -> list[ExportResult]:
    """Write both mapping tables. Read-only: the database is opened ``mode=ro``."""
    db = db_path or config.DB_PATH
    if not db.exists():
        raise FileNotFoundError(f"no database at {db}")
    target = out_dir or EXPORT_DIR
    uri = f"file:{db}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        return [export_mappings(conn, target), export_mapping_products(conn, target)]
