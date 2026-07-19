"""Three-stage scrape pipeline: discover -> fetch -> normalize.

Each stage is idempotent and restartable. State lives in the ``scrape_state``
table so a run only does outstanding work, and the raw payload store decouples
fetching (network, slow, fragile) from normalisation (local, fast, re-runnable).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.scraper import storage
from app.scraper.models import NormalizedRecipe
from app.scraper.ratelimit import AdaptiveThrottle
from app.scraper.sources.base import RecipeSource
from app.scraper.sources.hellofresh import RecipeExtractionError
from app.db.models import (
    Recipe,
    RecipeAllergen,
    RecipeCuisine,
    RecipeIngredient,
    RecipeNutrition,
    RecipeStep,
    RecipeTag,
    ScrapeState,
)

log = logging.getLogger("holafresca.scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Stop the whole fetch run if this many requests fail back-to-back — a strong
# sign the page structure changed or the source is hard-blocking us.
CONSECUTIVE_FAILURE_LIMIT = 20
# Per-request retry attempts before a URL is marked as errored.
MAX_ATTEMPTS = 4


@dataclass
class StageResult:
    discovered_new: int = 0
    fetched: int = 0
    normalized: int = 0
    incomplete: int = 0
    errors: int = 0
    stopped_early: bool = False
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Discover
# --------------------------------------------------------------------------

def discover(source: RecipeSource, session_factory: sessionmaker[Session]) -> StageResult:
    result = StageResult()

    def http_get(url: str) -> bytes:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content

    pairs = list(source.discover(http_get))
    with session_factory() as session:
        existing = {
            row[0]
            for row in session.execute(
                select(ScrapeState.source_id).where(ScrapeState.source == source.name)
            )
        }
        new_rows = [
            ScrapeState(source=source.name, source_id=sid, url=url, status="discovered")
            for sid, url in pairs
            if sid not in existing
        ]
        session.add_all(new_rows)
        session.commit()
        result.discovered_new = len(new_rows)
    result.notes.append(f"{len(pairs)} URLs in sitemap, {result.discovered_new} new")
    return result


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def fetch(
    source: RecipeSource,
    session_factory: sessionmaker[Session],
    *,
    limit: int | None = None,
    retry_errors: bool = False,
    throttle: AdaptiveThrottle | None = None,
) -> StageResult:
    return asyncio.run(
        _fetch_async(
            source,
            session_factory,
            limit=limit,
            retry_errors=retry_errors,
            throttle=throttle or AdaptiveThrottle(),
        )
    )


async def _fetch_async(
    source: RecipeSource,
    session_factory: sessionmaker[Session],
    *,
    limit: int | None,
    retry_errors: bool,
    throttle: AdaptiveThrottle,
) -> StageResult:
    result = StageResult()
    statuses = ["discovered", "error"] if retry_errors else ["discovered"]

    with session_factory() as session:
        stmt = (
            select(ScrapeState.id, ScrapeState.source_id, ScrapeState.url)
            .where(ScrapeState.source == source.name, ScrapeState.status.in_(statuses))
            .order_by(ScrapeState.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        work = [(sid, src_id, url) for sid, src_id, url in session.execute(stmt)]

    if not work:
        result.notes.append("nothing to fetch")
        return result

    queue: asyncio.Queue = asyncio.Queue()
    for item in work:
        queue.put_nowait(item)

    db_lock = asyncio.Lock()
    stop = asyncio.Event()
    counters = {"consecutive_failures": 0}

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True
    ) as client:
        workers = [
            asyncio.create_task(
                _fetch_worker(
                    name=i,
                    source=source,
                    client=client,
                    queue=queue,
                    throttle=throttle,
                    session_factory=session_factory,
                    db_lock=db_lock,
                    stop=stop,
                    counters=counters,
                    result=result,
                )
            )
            for i in range(throttle.workers)
        ]
        await queue.join()
        stop.set()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    if stop.is_set() and not queue.empty():
        result.stopped_early = True
        result.notes.append(
            f"hard-stopped after {CONSECUTIVE_FAILURE_LIMIT} consecutive failures"
        )
    return result


async def _fetch_worker(
    *,
    name: int,
    source: RecipeSource,
    client: httpx.AsyncClient,
    queue: asyncio.Queue,
    throttle: AdaptiveThrottle,
    session_factory: sessionmaker[Session],
    db_lock: asyncio.Lock,
    stop: asyncio.Event,
    counters: dict,
    result: StageResult,
) -> None:
    while True:
        try:
            state_id, source_id, url = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        if stop.is_set():
            queue.task_done()
            continue

        try:
            page = await _get_with_retries(client, url, throttle)
            if page is None:
                await _finish(db_lock, session_factory, state_id, "error",
                              "fetch failed after retries", result, counters, stop)
            else:
                try:
                    payload = source.extract(page, url)
                    await asyncio.to_thread(
                        storage.write_raw, source.name, source_id, payload
                    )
                    await _finish(db_lock, session_factory, state_id, "fetched",
                                  None, result, counters, stop)
                except RecipeExtractionError as exc:
                    await _finish(db_lock, session_factory, state_id, "error",
                                  str(exc), result, counters, stop)
        finally:
            queue.task_done()


async def _get_with_retries(
    client: httpx.AsyncClient, url: str, throttle: AdaptiveThrottle
) -> bytes | None:
    for attempt in range(MAX_ATTEMPTS):
        await throttle.before_request()
        try:
            resp = await client.get(url)
        except (httpx.TimeoutException, httpx.TransportError):
            await throttle.on_throttle()
            await asyncio.sleep(2**attempt)
            continue
        if resp.status_code == 200:
            await throttle.on_success()
            return resp.content
        if resp.status_code == 429 or resp.status_code >= 500:
            await throttle.on_throttle()
            await asyncio.sleep(2**attempt)
            continue
        # 4xx other than 429 (e.g. 404/410): permanent, do not retry.
        return None
    return None


async def _finish(
    db_lock: asyncio.Lock,
    session_factory: sessionmaker[Session],
    state_id: int,
    status: str,
    error: str | None,
    result: StageResult,
    counters: dict,
    stop: asyncio.Event,
) -> None:
    async with db_lock:
        await asyncio.to_thread(_mark_state, session_factory, state_id, status, error)
        if status == "fetched":
            result.fetched += 1
            counters["consecutive_failures"] = 0
        else:
            result.errors += 1
            counters["consecutive_failures"] += 1
            if counters["consecutive_failures"] >= CONSECUTIVE_FAILURE_LIMIT:
                stop.set()


def _mark_state(
    session_factory: sessionmaker[Session], state_id: int, status: str, error: str | None
) -> None:
    from datetime import datetime, timezone

    with session_factory() as session:
        state = session.get(ScrapeState, state_id)
        if state is None:
            return
        state.status = status
        state.attempts += 1
        state.error_message = error
        if status == "fetched":
            state.fetched_at = datetime.now(timezone.utc)
        session.commit()


# --------------------------------------------------------------------------
# Normalize
# --------------------------------------------------------------------------

def normalize(
    source: RecipeSource,
    session_factory: sessionmaker[Session],
    *,
    force: bool = False,
) -> StageResult:
    from datetime import datetime, timezone

    result = StageResult()
    statuses = ["fetched", "normalized"] if force else ["fetched"]

    with session_factory() as session:
        rows = [
            (r[0], r[1])
            for r in session.execute(
                select(ScrapeState.id, ScrapeState.source_id)
                .where(ScrapeState.source == source.name, ScrapeState.status.in_(statuses))
                .order_by(ScrapeState.id)
            )
        ]

    for state_id, source_id in rows:
        try:
            payload = storage.read_raw(source.name, source_id)
        except FileNotFoundError:
            with session_factory() as session:
                _set_status(session, state_id, "error", "raw payload missing")
            result.errors += 1
            continue

        try:
            recipe = source.normalize(payload, url="")
        except Exception as exc:  # noqa: BLE001 - one bad payload must not abort the run
            log.warning("normalize failed for %s: %s", source_id, exc)
            with session_factory() as session:
                _set_status(session, state_id, "error", f"normalize failed: {exc}")
            result.errors += 1
            continue

        with session_factory() as session:
            _upsert_recipe(session, recipe)
            _set_status(session, state_id, "normalized", None)
            session.get(ScrapeState, state_id).normalized_at = datetime.now(timezone.utc)
            session.commit()

        result.normalized += 1
        if not recipe.is_complete:
            result.incomplete += 1

    return result


def _parse_dt(value: str | None):
    """Parse an ISO-8601 timestamp (e.g. '2025-12-15T20:10:55.108Z') to naive UTC."""
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _set_status(session: Session, state_id: int, status: str, error: str | None) -> None:
    state = session.get(ScrapeState, state_id)
    if state is not None:
        state.status = status
        state.error_message = error
        session.commit()


def _upsert_recipe(session: Session, recipe: NormalizedRecipe) -> Recipe:
    existing = session.scalar(
        select(Recipe).where(
            Recipe.source == recipe.source, Recipe.source_id == recipe.source_id
        )
    )
    if existing is not None:
        session.delete(existing)
        session.flush()

    from datetime import datetime, timezone

    row = Recipe(
        source=recipe.source,
        source_id=recipe.source_id,
        url=recipe.url,
        name=recipe.name,
        headline=recipe.headline,
        slug=recipe.slug,
        description=recipe.description,
        difficulty=recipe.difficulty,
        prep_time_min=recipe.prep_time_min,
        total_time_min=recipe.total_time_min,
        serving_size_g=recipe.serving_size_g,
        base_yield=recipe.base_yield,
        image_path=recipe.image_path,
        energy_kcal=recipe.energy_kcal,
        protein_g=recipe.protein_g,
        fat_g=recipe.fat_g,
        carbs_g=recipe.carbs_g,
        is_complete=1 if recipe.is_complete else 0,
        avg_rating=recipe.avg_rating,
        ratings_count=recipe.ratings_count,
        favorites_count=recipe.favorites_count,
        is_addon=1 if recipe.is_addon else 0,
        source_created_at=_parse_dt(recipe.source_created_at),
        source_updated_at=_parse_dt(recipe.source_updated_at),
        scraped_at=datetime.now(timezone.utc),
    )
    row.ingredients = [
        RecipeIngredient(
            source_ingredient_id=i.source_ingredient_id,
            name=i.name,
            raw_text=i.raw_text,
            type=i.type,
            slug=i.slug,
            amount=i.amount,
            unit=i.unit,
            image_path=i.image_path,
        )
        for i in recipe.ingredients
    ]
    row.steps = [
        RecipeStep(
            index=s.index,
            instructions_text=s.instructions_text,
            instructions_html=s.instructions_html,
        )
        for s in recipe.steps
    ]
    row.nutrition = [
        RecipeNutrition(name=n.name, amount=n.amount, unit=n.unit) for n in recipe.nutrition
    ]
    row.tags = [RecipeTag(name=t.name, type=t.type, slug=t.slug) for t in recipe.tags]
    row.cuisines = [RecipeCuisine(name=c) for c in recipe.cuisines]
    row.allergens = [
        RecipeAllergen(name=a.name, slug=a.slug) for a in recipe.allergens
    ]
    session.add(row)
    return row


# --------------------------------------------------------------------------
# Status reporting
# --------------------------------------------------------------------------

def status_counts(source: RecipeSource, session_factory: sessionmaker[Session]) -> dict:
    with session_factory() as session:
        state_rows = session.execute(
            select(ScrapeState.status, func.count())
            .where(ScrapeState.source == source.name)
            .group_by(ScrapeState.status)
        ).all()
        recipes = session.scalar(
            select(func.count()).select_from(Recipe).where(Recipe.source == source.name)
        )
        complete = session.scalar(
            select(func.count())
            .select_from(Recipe)
            .where(Recipe.source == source.name, Recipe.is_complete == 1)
        )
        curated = session.scalar(
            select(func.count())
            .select_from(Recipe)
            .where(Recipe.source == source.name, Recipe.curated == 1)
        )
    return {
        "states": {status: count for status, count in state_rows},
        "recipes": recipes or 0,
        "complete": complete or 0,
        "curated": curated or 0,
    }
