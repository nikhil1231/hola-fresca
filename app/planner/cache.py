"""Process-wide cache of the planner index and everything derived from it.

Building a :class:`~app.planner.index.PlanIndex` reads the whole mapping and the
whole recipe library — several seconds against the real database — but nothing it
reads changes between edits. Every browse request was paying for a snapshot
identical to the one the previous request had just thrown away, and the browse
page issues three such requests at once.

The same argument applies twice over to what is computed *from* the index. A
recipe's standalone price and its standing against a pinned week are fixed by the
library, not by the request: filtering and paging change which of those figures
are shown, never what any of them is. So they are computed once per edit and held
here beside the index that produced them, and are dropped together with it.

Staleness is decided by stat-ing the files the index is derived from rather than
by calling ``invalidate()`` at each write site. The SQLite file is written by the
scraper and the mapping CLI as well as by the API, and those writes have to count
too; a stat costs microseconds, so it is affordable on the way in to every
request. (There is no long-lived rollback journal in SQLite's default mode, so a
commit always moves the main file's mtime.)
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.planner.basket import Selection, score_basket
from app.planner.index import DEFAULT_STATUSES, RETAILER, PlanIndex, load_index
from app.planner.ranking import RankedCandidate, rank_candidates

log = logging.getLogger(__name__)

# How many pinned-week rankings to keep. Each is a few hundred kilobytes and the
# week grows one recipe at a time, so a handful covers adding a recipe, changing
# your mind, and going back — without holding every week ever planned.
MAX_RANKINGS = 6

_LOCK = threading.Lock()
_CACHE: dict[tuple, "_Entry"] = {}


@dataclass(frozen=True, slots=True)
class StandalonePrice:
    """What one recipe costs on its own, ignoring the rest of the week."""

    score: float
    consumed_cost: float
    gap_count: int

    @property
    def has_gap(self) -> bool:
        """Something in this recipe cannot be priced, so its total understates."""
        return self.gap_count > 0


@dataclass
class _Entry:
    fingerprint: tuple
    index: PlanIndex
    standalone: dict[int, dict[int, StandalonePrice]] = field(default_factory=dict)
    rankings: dict[tuple, list[RankedCandidate]] = field(default_factory=dict)


def _db_path(factory: sessionmaker[Session]) -> Path | None:
    """The SQLite file a session factory is bound to, if it is one."""
    bind = getattr(factory, "kw", {}).get("bind")
    database = getattr(getattr(bind, "url", None), "database", None)
    return Path(database) if database else None


def _stat(path: Path | None) -> tuple:
    if path is None:
        return ()
    try:
        st = path.stat()
    except OSError:
        return ()
    return (st.st_mtime_ns, st.st_size)


def _fingerprint(db: Path | None, csv_path: Path | None) -> tuple:
    csv = csv_path or (config.DATA_DIR / "ingredient_frequency.csv")
    return (_stat(db), _stat(csv))


def _entry(
    factory: sessionmaker[Session],
    statuses: tuple[str, ...],
    retailer: str,
    csv_path: Path | None,
) -> _Entry:
    """The cache entry for this database, rebuilt if the files moved under it.

    Callers hold ``_LOCK``. Returning the entry rather than the index lets the
    derived tables live and die with the snapshot they were computed from.
    """
    db = _db_path(factory)
    # Keyed on the database's path, not the factory's identity: tests build a new
    # factory per temp database, and a freed object's id can be handed straight
    # back to its successor.
    key = (str(db), statuses, retailer, str(csv_path) if csv_path else None)
    before = _fingerprint(db, csv_path)

    cached = _CACHE.get(key)
    if cached is not None and cached.fingerprint == before and before[0]:
        return cached

    index = load_index(
        factory, statuses=statuses, retailer=retailer, csv_path=csv_path, curated_only=True
    )
    entry = _Entry(fingerprint=before, index=index)

    if not before[0]:
        # Nothing to watch for staleness — an in-memory database, or a bind that
        # is not a file. Serving a stale index would be unfixable, so this one is
        # never cached.
        return entry

    # If the database moved while we were reading it, the snapshot may be torn
    # across the write. Hand it back but do not cache it, so the next caller
    # rebuilds against a settled file.
    after = _fingerprint(db, csv_path)
    if after == before:
        _CACHE[key] = entry
    else:
        _CACHE.pop(key, None)
        log.info("planner index: rebuilt during a write, not caching this snapshot")
    return entry


def get_index(
    factory: sessionmaker[Session],
    *,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    retailer: str = RETAILER,
    csv_path: Path | None = None,
) -> PlanIndex:
    """The whole curated library's planner index, built at most once per edit.

    Always the full library rather than a subset: loading the ingredient table
    costs the same either way, so asking for twenty-four recipes was never
    cheaper than asking for all of them — it just meant the answer could not be
    reused.

    The returned index is shared, and callers mutate its ``cover_cache``. That is
    deliberate — the covering results are pure functions of the pack list, so a
    racing duplicate computation is harmless and the shared cache is what makes
    scoring the library cheap.
    """
    with _LOCK:
        return _entry(factory, statuses, retailer, csv_path).index


def get_standalone_prices(
    factory: sessionmaker[Session],
    *,
    servings: int,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    retailer: str = RETAILER,
    csv_path: Path | None = None,
) -> dict[int, StandalonePrice]:
    """Every curated recipe's own price, priced once per edit.

    Sorting the library by price needs all of these before it can show the first
    twenty-four, which is why that sort used to cost seconds a page. Computed in
    one pass over a warm cover cache, the whole table is cheaper than the index
    load that precedes it.
    """
    with _LOCK:
        entry = _entry(factory, statuses, retailer, csv_path)
        table = entry.standalone.get(servings)
        if table is None:
            index = entry.index
            table = {}
            for recipe_id in index.recipes:
                scored = score_basket(
                    index, [Selection(recipe_id=recipe_id, servings=servings)]
                )
                table[recipe_id] = StandalonePrice(
                    score=scored.score,
                    consumed_cost=scored.consumed_cost,
                    gap_count=scored.gap_count,
                )
            entry.standalone[servings] = table
        return table


def get_ranking(
    factory: sessionmaker[Session],
    pinned: list[Selection],
    *,
    candidate_portions: int,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    retailer: str = RETAILER,
    csv_path: Path | None = None,
) -> list[RankedCandidate]:
    """The whole library ranked against a pinned week, best fit first.

    Ranked over every curated recipe rather than over the filtered subset,
    because the ranking does not depend on the filters — so narrowing by cuisine,
    or turning to page two, reuses this instead of recomputing it.
    """
    pinned_key = tuple(sorted((s.recipe_id, s.servings) for s in pinned))
    with _LOCK:
        entry = _entry(factory, statuses, retailer, csv_path)
        key = (pinned_key, candidate_portions)
        ranked = entry.rankings.get(key)
        if ranked is None:
            pinned_ids = {s.recipe_id for s in pinned}
            candidate_ids = [r for r in entry.index.recipes if r not in pinned_ids]
            ranked = rank_candidates(
                entry.index, pinned, candidate_ids, candidate_portions=candidate_portions
            )
            entry.rankings[key] = ranked
            while len(entry.rankings) > MAX_RANKINGS:
                entry.rankings.pop(next(iter(entry.rankings)))
        return ranked
