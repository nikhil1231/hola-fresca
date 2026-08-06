"""Revision-aware, incrementally hydrated planner snapshots."""
from __future__ import annotations

import logging
import threading
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.planner.basket import Selection, score_basket
from app.planner.index import (
    DEFAULT_STATUSES,
    RETAILER,
    PlanIndex,
    PlannerCatalogue,
    hydrate_recipes,
    load_catalogue,
)
from app.planner.ranking import RankedCandidate, rank_candidates

log = logging.getLogger(__name__)

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
        return self.gap_count > 0


@dataclass
class _Entry:
    signature: tuple[int, int, tuple]
    catalogue: PlannerCatalogue
    index: PlanIndex
    loaded_recipe_ids: set[int] = field(default_factory=set)
    full_recipe_ids: set[int] | None = None
    standalone: dict[tuple, dict[int, StandalonePrice]] = field(default_factory=dict)
    rankings: dict[tuple, list[RankedCandidate]] = field(default_factory=dict)


def _db_path(factory: sessionmaker[Session]) -> Path | None:
    bind = getattr(factory, "kw", {}).get("bind")
    database = getattr(getattr(bind, "url", None), "database", None)
    return Path(database) if database else None


def _stat(path: Path | None) -> tuple:
    if path is None:
        return ()
    try:
        value = path.stat()
    except OSError:
        return ()
    return value.st_mtime_ns, value.st_size


def _signature(
    factory: sessionmaker[Session], csv_path: Path | None
) -> tuple[int, int, tuple] | None:
    """Catalogue generations plus the one planner input outside SQLite."""
    bind = getattr(factory, "kw", {}).get("bind")
    if bind is None or _db_path(factory) is None:
        return None
    try:
        with bind.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT recipe_revision, ingredient_revision "
                    "FROM planner_cache_state WHERE id = 1"
                )
            ).one()
    except Exception:
        log.exception("planner cache revision state is unavailable; bypassing cache")
        return None
    csv = csv_path or (config.DATA_DIR / "ingredient_frequency.csv")
    return int(row[0]), int(row[1]), _stat(csv)


def _cache_key(
    factory: sessionmaker[Session],
    statuses: tuple[str, ...],
    retailer: str,
    csv_path: Path | None,
) -> tuple:
    return (
        str(_db_path(factory)),
        statuses,
        retailer,
        str(csv_path) if csv_path else None,
    )


def _fresh_entry(
    factory: sessionmaker[Session],
    statuses: tuple[str, ...],
    retailer: str,
    csv_path: Path | None,
    signature: tuple[int, int, tuple],
) -> _Entry:
    catalogue = load_catalogue(
        factory, statuses=statuses, retailer=retailer, csv_path=csv_path
    )
    return _Entry(
        signature=signature,
        catalogue=catalogue,
        index=PlanIndex(ingredients=catalogue.ingredients, statuses=statuses),
    )


def _entry(
    factory: sessionmaker[Session],
    statuses: tuple[str, ...],
    retailer: str,
    csv_path: Path | None,
) -> tuple[tuple, _Entry, bool]:
    """Return a current base snapshot; expensive loading happens unlocked."""
    key = _cache_key(factory, statuses, retailer, csv_path)
    for attempt in range(2):
        before = _signature(factory, csv_path)
        if before is None:
            catalogue = load_catalogue(
                factory, statuses=statuses, retailer=retailer, csv_path=csv_path
            )
            transient = _Entry(
                signature=(0, 0, ()),
                catalogue=catalogue,
                index=PlanIndex(ingredients=catalogue.ingredients, statuses=statuses),
            )
            return key, transient, False

        with _LOCK:
            cached = _CACHE.get(key)
            if cached is not None and cached.signature == before:
                return key, cached, True

            # A recipe-only edit does not make the 697-item ingredient catalogue
            # or its pure covering results stale. Start a new recipe generation
            # around those shared objects and discard recipe-derived data.
            if (
                cached is not None
                and cached.signature[1:] == before[1:]
                and cached.signature[0] != before[0]
            ):
                replacement = _Entry(
                    signature=before,
                    catalogue=cached.catalogue,
                    index=PlanIndex(
                        ingredients=cached.index.ingredients,
                        statuses=statuses,
                        cover_cache=cached.index.cover_cache,
                    ),
                )
                _CACHE[key] = replacement
                return key, replacement, True

        built = _fresh_entry(factory, statuses, retailer, csv_path, before)
        after = _signature(factory, csv_path)
        if after != before:
            if attempt == 0:
                continue
            log.warning("planner catalogue changed twice while its snapshot was loading")
            return key, built, False

        with _LOCK:
            current = _CACHE.get(key)
            if current is not None and current.signature == before:
                return key, current, True
            _CACHE[key] = built
            return key, built, True

    raise AssertionError("unreachable")


def _index_entry(
    factory: sessionmaker[Session],
    *,
    recipe_ids: Collection[int] | None,
    statuses: tuple[str, ...],
    retailer: str,
    csv_path: Path | None,
) -> tuple[_Entry, bool]:
    """Hydrate only the missing recipes, retrying one catalogue race."""
    requested = None if recipe_ids is None else set(recipe_ids)
    for attempt in range(2):
        key, entry, cacheable = _entry(factory, statuses, retailer, csv_path)
        with _LOCK:
            if requested is None:
                if entry.full_recipe_ids is not None:
                    return entry, cacheable
                missing: set[int] | None = None
            else:
                missing = requested - entry.loaded_recipe_ids
                if not missing:
                    return entry, cacheable

        loaded = hydrate_recipes(
            factory, entry.catalogue, recipe_ids=missing, curated_only=True
        )
        after = _signature(factory, csv_path)
        stable = (
            after is None
            if entry.signature == (0, 0, ())
            else after == entry.signature
        )
        if not stable:
            if attempt == 0:
                continue
            log.warning("planner catalogue changed twice while recipes were hydrating")
            latest = after or entry.signature
            fallback = _fresh_entry(factory, statuses, retailer, csv_path, latest)
            current = hydrate_recipes(
                factory, fallback.catalogue, recipe_ids=requested, curated_only=True
            )
            fallback.index.recipes.update(current)
            fallback.loaded_recipe_ids.update(requested or current)
            if requested is None:
                fallback.full_recipe_ids = set(current)
            return fallback, False

        with _LOCK:
            target = entry
            if cacheable:
                current = _CACHE.get(key)
                if current is not None and current.signature == entry.signature:
                    target = current
            if requested is None:
                # Record the curated set separately. Explicit detail requests may
                # also place non-curated recipes in the same targeted view, but a
                # catalogue ranking must never treat those as candidates.
                target.full_recipe_ids = set(loaded)
                target.loaded_recipe_ids.update(loaded)
            else:
                target.loaded_recipe_ids.update(missing or ())
            target.index.recipes.update(loaded)
            return target, cacheable

    raise AssertionError("unreachable")


def get_index(
    factory: sessionmaker[Session],
    *,
    recipe_ids: Collection[int] | None = None,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    retailer: str = RETAILER,
    csv_path: Path | None = None,
) -> PlanIndex:
    """Return a shared index, hydrating one view or the full curated library."""
    entry, _ = _index_entry(
        factory,
        recipe_ids=recipe_ids,
        statuses=statuses,
        retailer=retailer,
        csv_path=csv_path,
    )
    return entry.index


def note_pack_preference(factory: sessionmaker[Session]) -> None:
    """Prune only results derived from the changed user's pack preferences."""
    db = str(_db_path(factory))
    with _LOCK:
        for key, entry in _CACHE.items():
            if key[0] == db:
                entry.standalone.clear()
                entry.rankings.clear()


def _preferences_key(pack_preferences: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((pack_preferences or {}).items()))


def get_standalone_prices(
    factory: sessionmaker[Session],
    *,
    servings: int,
    recipe_ids: Collection[int] | None = None,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    retailer: str = RETAILER,
    csv_path: Path | None = None,
    pack_preferences: dict[str, str] | None = None,
) -> dict[int, StandalonePrice]:
    """Price requested recipes, reusing every individual price already known."""
    requested = None if recipe_ids is None else set(recipe_ids)
    entry, cacheable = _index_entry(
        factory,
        recipe_ids=requested,
        statuses=statuses,
        retailer=retailer,
        csv_path=csv_path,
    )
    ids = set(entry.full_recipe_ids or ()) if requested is None else requested
    ids.intersection_update(entry.index.recipes)
    prefs = pack_preferences or {}
    key = servings, _preferences_key(prefs)
    with _LOCK:
        table = entry.standalone.setdefault(key, {})
        missing = ids - table.keys()
    computed: dict[int, StandalonePrice] = {}
    for recipe_id in missing:
        scored = score_basket(
            entry.index,
            [Selection(recipe_id=recipe_id, servings=servings)],
            pack_preferences=prefs,
        )
        computed[recipe_id] = StandalonePrice(
            score=scored.score,
            consumed_cost=scored.consumed_cost,
            gap_count=scored.gap_count,
        )
    if computed:
        stable = cacheable and _signature(factory, csv_path) == entry.signature
        if stable:
            with _LOCK:
                table.update(computed)
        else:
            table = {**table, **computed}
    return {recipe_id: table[recipe_id] for recipe_id in ids if recipe_id in table}


def get_ranking(
    factory: sessionmaker[Session],
    pinned: list[Selection],
    *,
    candidate_portions: int,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    retailer: str = RETAILER,
    csv_path: Path | None = None,
    pack_preferences: dict[str, str] | None = None,
) -> list[RankedCandidate]:
    """Rank the deliberately full curated library against a pinned week."""
    entry, cacheable = _index_entry(
        factory,
        recipe_ids=None,
        statuses=statuses,
        retailer=retailer,
        csv_path=csv_path,
    )
    prefs = pack_preferences or {}
    pinned_key = tuple(sorted((s.recipe_id, s.servings) for s in pinned))
    key = pinned_key, candidate_portions, _preferences_key(prefs)
    with _LOCK:
        cached = entry.rankings.get(key)
    if cached is not None:
        return cached
    pinned_ids = {selection.recipe_id for selection in pinned}
    candidate_ids = [
        recipe_id
        for recipe_id in (entry.full_recipe_ids or ())
        if recipe_id not in pinned_ids
    ]
    ranked = rank_candidates(
        entry.index,
        pinned,
        candidate_ids,
        candidate_portions=candidate_portions,
        pack_preferences=prefs,
    )
    if cacheable and _signature(factory, csv_path) == entry.signature:
        with _LOCK:
            entry.rankings[key] = ranked
            while len(entry.rankings) > MAX_RANKINGS:
                entry.rankings.pop(next(iter(entry.rankings)))
    return ranked
