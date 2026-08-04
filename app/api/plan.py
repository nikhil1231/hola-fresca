"""The plan: which recipes a user has chosen for which week, and how.

This lived in ``localStorage`` until accounts arrived, which is why it looks the
way it does — a week holds recipes with portions and a protein modifier, plus
per-ingredient decisions about the basket. Moving it here is the whole point of
the exercise: a plan made on the laptop should be there on the phone.

Two things follow from it being shared between devices rather than owned by one.

**Every write is one row.** There is no "save the plan" endpoint, because two
phones with the same week open would then take turns overwriting each other with
whatever they last read. Adding a recipe adds a recipe; changing portions changes
portions. Concurrent edits to different entries all survive, and the worst a
genuine collision can do is decide between two values for one field.

**Nothing is trusted from the client that the server can know.** A selection is a
recipe id and how you are cooking it; the recipe's name, macros and price are
looked up on the way out. The browser used to cache a copy of those beside each
entry, which meant a corrected macro or a renamed dish never reached a plan that
already contained it.

**A week that has been and gone is read-only.** Reads go back as far as the plan
does — a shop from March is exactly how you find out that those recipes were
cooked at 1.5x protein — but writes stop at the week being planned. See
:func:`_require_editable_week`.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_session
from app.api.recipes import _library_condition, _personal_rating_map, _to_card, _wishlist_map
from app.api.schemas import (
    PlanEntryIn,
    PlanEntryOut,
    PlanEntryPatchIn,
    PlanImportIn,
    PlanImportOut,
    PlanOut,
    PlanWeekItemIn,
    PlanWeekOut,
    ProteinModifierIn,
)
from app import schedule as sched
from app.db.models import PlanSelection, PlanWeekItem, Recipe, User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plan", tags=["plan"])

#: The most recipes one week may hold. Not the same as the ``recipes_per_week``
#: setting, which is a target the UI enforces and can be lowered freely: this is
#: a ceiling on what may be stored, well above any sane setting, so that turning
#: the target down cannot delete recipes already chosen.
MAX_WEEK_RECIPES = 14

DEFAULT_PORTIONS = 4


def _require_week_start(value: str) -> str:
    """Validate a week key. Weeks are Mondays; anything else is a client bug."""
    try:
        parsed = sched.parse_date(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Not a date: {value}") from None
    if not sched.is_week_start(parsed):
        raise HTTPException(status_code=400, detail=f"{value} is not a Monday")
    return sched.format_date(parsed)


def _require_editable_week(week_start: str) -> str:
    """Reject a write to a week that has been and gone.

    A finished week is a record rather than a draft: its recipes are what was
    cooked and its pack choices are what was bought. Editing one would not change
    anything that has already happened, it would only lose the account of it —
    including the protein swap you need to read back when you cook the thing
    again. The pages hide the controls; this is what makes it true of the API.
    """
    if sched.parse_date(week_start) < sched.upcoming_week_start():
        raise HTTPException(
            status_code=409,
            detail=f"The week of {week_start} has been and gone, and is read-only",
        )
    return week_start


def _protein_out(raw: str | None) -> ProteinModifierIn | None:
    """Read a stored modifier back, tolerating one that no longer validates.

    The modifier's schema can change under rows already written — a swap target
    that no longer exists, a field that was dropped. A plan entry is worth more
    than its modifier, so a blob that will not parse is logged and dropped rather
    than being allowed to fail the whole week's read.
    """
    if not raw:
        return None
    try:
        return ProteinModifierIn.model_validate(json.loads(raw))
    except Exception:
        log.warning("discarding unreadable protein modifier: %r", raw)
        return None


def _protein_json(protein: ProteinModifierIn | None) -> str | None:
    if protein is None:
        return None
    data = protein.model_dump(exclude_none=True)
    return json.dumps(data) if data else None


def _utc(value: datetime | None) -> datetime | None:
    """SQLite drops the offset on the way in; put it back on the way out."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _week_items(session: Session, user_id: int, week_starts: list[str]) -> dict[str, list[PlanWeekItem]]:
    if not week_starts:
        return {}
    rows = session.scalars(
        select(PlanWeekItem).where(
            PlanWeekItem.user_id == user_id,
            PlanWeekItem.week_start.in_(week_starts),
        )
    ).all()
    by_week: dict[str, list[PlanWeekItem]] = {}
    for row in rows:
        by_week.setdefault(row.week_start, []).append(row)
    return by_week


def _plan_out(session: Session, user_id: int, week_starts: list[str] | None = None) -> PlanOut:
    """Build the whole plan (or given weeks) with recipes hydrated.

    One query for the selections and one for the recipes, rather than a lookup
    per entry: a plan is a handful of weeks of five recipes, but the browse page
    asks for it on every navigation.
    """
    stmt = select(PlanSelection).where(PlanSelection.user_id == user_id)
    if week_starts is not None:
        stmt = stmt.where(PlanSelection.week_start.in_(week_starts))
    selections = session.scalars(
        stmt.order_by(PlanSelection.week_start, PlanSelection.position, PlanSelection.id)
    ).all()

    recipe_ids = list({s.recipe_id for s in selections})
    by_id: dict[int, Recipe] = {}
    if recipe_ids:
        rows = session.scalars(
            select(Recipe)
            .where(Recipe.id.in_(recipe_ids))
            .options(selectinload(Recipe.cuisines), selectinload(Recipe.tags))
        ).all()
        by_id = {recipe.id: recipe for recipe in rows}
    ratings = _personal_rating_map(session, user_id, recipe_ids)
    wishlist = _wishlist_map(session, user_id, recipe_ids)

    weeks: dict[str, PlanWeekOut] = {}
    if week_starts is not None:
        for week_start in week_starts:
            weeks[week_start] = PlanWeekOut(week_start=week_start)

    for selection in selections:
        recipe = by_id.get(selection.recipe_id)
        if recipe is None:
            # The recipe was deleted from the library under a plan that still
            # names it. Skipped rather than erroring: the rest of the week is
            # perfectly good, and a stale row is not worth a broken page.
            continue
        week = weeks.setdefault(
            selection.week_start, PlanWeekOut(week_start=selection.week_start)
        )
        week.recipes.append(
            PlanEntryOut(
                recipe=_to_card(
                    recipe,
                    personal_rating=ratings.get(recipe.id),
                    wishlisted=wishlist.get(recipe.id, False),
                ),
                portions=selection.portions,
                protein=_protein_out(selection.protein_json),
                added_at=_utc(selection.created_at),
            )
        )

    for week_start, items in _week_items(
        session, user_id, week_starts if week_starts is not None else list(weeks)
    ).items():
        week = weeks.setdefault(week_start, PlanWeekOut(week_start=week_start))
        for item in items:
            if item.pack_sku:
                week.pack_overrides[item.ingredient_key] = item.pack_sku
            if item.snapped:
                week.snap_overrides[item.ingredient_key] = True
            if item.owned:
                week.owned_item_keys.append(item.ingredient_key)
        week.owned_item_keys.sort()

    return PlanOut(weeks=[weeks[key] for key in sorted(weeks)])


def _require_planable_recipe(session: Session, recipe_id: int) -> Recipe:
    """A recipe may be planned if it is in the shared library.

    Personal hides are not consulted — hiding a recipe is about not being shown
    it, and adding one you had hidden is a perfectly coherent thing to do.
    """
    recipe = session.scalar(
        select(Recipe).where(*_library_condition(), Recipe.id == recipe_id)
    )
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Unknown or unavailable recipe: {recipe_id}")
    return recipe


def _next_position(session: Session, user_id: int, week_start: str) -> int:
    used = session.scalars(
        select(PlanSelection.position).where(
            PlanSelection.user_id == user_id, PlanSelection.week_start == week_start
        )
    ).all()
    return (max(used) + 1) if used else 0


def _week_item(
    session: Session, user_id: int, week_start: str, ingredient_key: str
) -> PlanWeekItem | None:
    return session.scalar(
        select(PlanWeekItem).where(
            PlanWeekItem.user_id == user_id,
            PlanWeekItem.week_start == week_start,
            PlanWeekItem.ingredient_key == ingredient_key,
        )
    )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

@router.get("", response_model=PlanOut)
def get_plan(
    session: Session = Depends(get_session), user: User = Depends(get_current_user)
) -> PlanOut:
    """Every week this user has planned, oldest first.

    Past weeks are returned too. They are small, and the client decides what
    counts as "upcoming" — which depends on the clock, and so is not a decision
    a cached response should have baked into it.
    """
    return _plan_out(session, user.id)


@router.get("/weeks/{week_start}", response_model=PlanWeekOut)
def get_week(
    week_start: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlanWeekOut:
    week_start = _require_week_start(week_start)
    plan = _plan_out(session, user.id, [week_start])
    return plan.weeks[0] if plan.weeks else PlanWeekOut(week_start=week_start)


# --------------------------------------------------------------------------
# Writes — one row each, so two devices cannot overwrite each other wholesale
# --------------------------------------------------------------------------

@router.post("/weeks/{week_start}/recipes", response_model=PlanWeekOut, status_code=201)
def add_recipe(
    week_start: str,
    body: PlanEntryIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlanWeekOut:
    """Put a recipe in a week.

    Adding one that is already there is not an error — on two devices it is
    simply what happens — so it returns the week unchanged rather than 409ing at
    someone who is already looking at the state they asked for.
    """
    week_start = _require_editable_week(_require_week_start(week_start))
    _require_planable_recipe(session, body.recipe_id)

    existing = session.scalar(
        select(PlanSelection).where(
            PlanSelection.user_id == user.id,
            PlanSelection.week_start == week_start,
            PlanSelection.recipe_id == body.recipe_id,
        )
    )
    if existing is None:
        count = len(
            session.scalars(
                select(PlanSelection.id).where(
                    PlanSelection.user_id == user.id, PlanSelection.week_start == week_start
                )
            ).all()
        )
        if count >= MAX_WEEK_RECIPES:
            raise HTTPException(
                status_code=400,
                detail=f"A week holds at most {MAX_WEEK_RECIPES} recipes",
            )
        session.add(
            PlanSelection(
                user_id=user.id,
                week_start=week_start,
                recipe_id=body.recipe_id,
                position=_next_position(session, user.id, week_start),
                portions=body.portions or DEFAULT_PORTIONS,
                protein_json=_protein_json(body.protein),
            )
        )
        session.commit()

    return get_week(week_start, session, user)


@router.patch("/weeks/{week_start}/recipes/{recipe_id}", response_model=PlanWeekOut)
def update_recipe(
    week_start: str,
    recipe_id: int,
    body: PlanEntryPatchIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlanWeekOut:
    """Change how a planned recipe is being cooked: portions, protein, or both."""
    week_start = _require_editable_week(_require_week_start(week_start))
    row = session.scalar(
        select(PlanSelection).where(
            PlanSelection.user_id == user.id,
            PlanSelection.week_start == week_start,
            PlanSelection.recipe_id == recipe_id,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Recipe {recipe_id} is not in the week of {week_start}"
        )

    if body.portions is not None:
        row.portions = body.portions
    # Told apart from "not mentioned" so that clearing a modifier is expressible;
    # `protein: null` is how the UI takes a swap back off.
    if "protein" in body.model_fields_set:
        row.protein_json = _protein_json(body.protein)
    session.commit()
    return get_week(week_start, session, user)


@router.delete("/weeks/{week_start}/recipes/{recipe_id}", response_model=PlanWeekOut)
def remove_recipe(
    week_start: str,
    recipe_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlanWeekOut:
    """Take a recipe out of a week. Removing one that is not there is a no-op."""
    week_start = _require_editable_week(_require_week_start(week_start))
    session.execute(
        delete(PlanSelection).where(
            PlanSelection.user_id == user.id,
            PlanSelection.week_start == week_start,
            PlanSelection.recipe_id == recipe_id,
        )
    )
    session.commit()
    return get_week(week_start, session, user)


@router.put("/weeks/{week_start}/items/{ingredient_key:path}", response_model=PlanWeekOut)
def set_week_item(
    week_start: str,
    ingredient_key: str,
    body: PlanWeekItemIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlanWeekOut:
    """Record a per-week decision about one basket line.

    Only the fields present in the body are touched, so the basket page can set
    "already own this" without knowing or caring what pack was chosen. When a row
    ends up saying nothing at all it is deleted rather than kept as a row of
    defaults, which keeps the table proportional to decisions actually made.
    """
    week_start = _require_editable_week(_require_week_start(week_start))
    if not ingredient_key:
        raise HTTPException(status_code=400, detail="An ingredient key is required")

    row = _week_item(session, user.id, week_start, ingredient_key)
    fields = body.model_fields_set
    if row is None:
        row = PlanWeekItem(
            user_id=user.id, week_start=week_start, ingredient_key=ingredient_key
        )
        session.add(row)

    if "pack_sku" in fields:
        row.pack_sku = body.pack_sku
    if "snapped" in fields:
        row.snapped = bool(body.snapped)
    if "owned" in fields:
        row.owned = bool(body.owned)

    if not row.pack_sku and not row.snapped and not row.owned:
        session.delete(row)
    session.commit()
    return get_week(week_start, session, user)


@router.delete("/weeks/{week_start}", response_model=PlanOut)
def clear_week(
    week_start: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlanOut:
    """Empty a week: its recipes and its per-ingredient decisions together.

    The decisions are meaningless without the recipes that created the demand,
    so leaving them behind would only mean a resurrected week came back with
    someone's old pack choices attached.
    """
    week_start = _require_editable_week(_require_week_start(week_start))
    session.execute(
        delete(PlanSelection).where(
            PlanSelection.user_id == user.id, PlanSelection.week_start == week_start
        )
    )
    session.execute(
        delete(PlanWeekItem).where(
            PlanWeekItem.user_id == user.id, PlanWeekItem.week_start == week_start
        )
    )
    session.commit()
    return _plan_out(session, user.id)


@router.post("/import", response_model=PlanImportOut)
def import_plan(
    body: PlanImportIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> PlanImportOut:
    """Take on a plan that was living in one browser's localStorage.

    Strictly additive: a week the account already has recipes in is left alone,
    and within a new week a recipe that has since left the library is skipped and
    reported rather than failing the import. Both rules exist because this runs
    unattended on first load after the upgrade — it must not be able to destroy a
    plan, and it must not be able to fail in a way that leaves the user stuck.
    """
    imported_weeks = 0
    imported_recipes = 0
    skipped: list[int] = []

    for week in body.weeks:
        week_start = _require_week_start(week.week_start)
        occupied = session.scalar(
            select(PlanSelection.id)
            .where(
                PlanSelection.user_id == user.id, PlanSelection.week_start == week_start
            )
            .limit(1)
        )
        if occupied is not None:
            continue

        added = 0
        for position, entry in enumerate(week.recipes[:MAX_WEEK_RECIPES]):
            in_library = session.scalar(
                select(Recipe.id).where(*_library_condition(), Recipe.id == entry.recipe_id)
            )
            if in_library is None:
                skipped.append(entry.recipe_id)
                continue
            session.add(
                PlanSelection(
                    user_id=user.id,
                    week_start=week_start,
                    recipe_id=entry.recipe_id,
                    position=position,
                    portions=entry.portions or DEFAULT_PORTIONS,
                    protein_json=_protein_json(entry.protein),
                )
            )
            added += 1

        keys = (
            set(week.pack_overrides)
            | {key for key, on in week.snap_overrides.items() if on}
            | set(week.owned_item_keys)
        )
        owned = set(week.owned_item_keys)
        for key in keys:
            if _week_item(session, user.id, week_start, key) is not None:
                continue
            session.add(
                PlanWeekItem(
                    user_id=user.id,
                    week_start=week_start,
                    ingredient_key=key,
                    pack_sku=week.pack_overrides.get(key),
                    snapped=bool(week.snap_overrides.get(key)),
                    owned=key in owned,
                )
            )

        if added or keys:
            imported_weeks += 1
            imported_recipes += added

    session.commit()
    return PlanImportOut(
        imported_weeks=imported_weeks,
        imported_recipes=imported_recipes,
        skipped_recipes=sorted(set(skipped)),
        plan=_plan_out(session, user.id),
    )
