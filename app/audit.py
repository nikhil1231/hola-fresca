"""Take a second look at one recipe's numbers, and correct them.

Raised from the recipe page when the macros look wrong. The pass is deliberately
ordered cheapest-first, because most bad data does not need a model to spot:

1. **Arithmetic.** Atwater energy (4·protein + 4·carbs + 9·fat) against the stated
   kcal catches the majority of broken macros for nothing. If three of the four
   numbers agree, the fourth is the wrong one and can be solved for directly —
   no model, no guessing.
2. **Composition.** When the four numbers are mutually consistent but still look
   implausible (60 g of protein in a vegetarian dish), only knowing what the
   ingredients actually contain settles it. That is the one question worth paying
   a model for, and it is asked in the narrowest possible form: per-100 g
   composition for each ingredient. The multiplication and the summing stay here,
   in Python, so the result is auditable line by line and the model never gets to
   assert a total.

Every correction is written as a :class:`~app.db.models.RecipeEdit` carrying the
value it replaced, so any of this is reversible.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app import config
from app.canonicalize import _MASS_UNITS, normalize_name, _typical_grams_by_name
from app.classify import macros_suspect, protein_energy_ratio
from app.db.models import Recipe, RecipeEdit, RecipeIngredient
from app.mapping.openai_client import Completer

log = logging.getLogger("holafresca.audit")

MACRO_FIELDS = ("energy_kcal", "protein_g", "fat_g", "carbs_g")

# Atwater coefficients: kcal per gram of each macronutrient.
KCAL_PER_G = {"protein_g": 4.0, "carbs_g": 4.0, "fat_g": 9.0}

# How far Atwater energy may sit from the stated kcal before the macros are
# considered broken. Matches classify.macros_suspect so the flag and the fix agree.
TOLERANCE = 0.25
# Within this, the numbers are close enough that "correcting" them would be noise.
CLOSE_ENOUGH = 0.05
# A single serving beyond this is not a meal, it is a data error.
MAX_PLAUSIBLE_KCAL = 2000
# ...and below this it is not a main course. Used as a guard on the arithmetic fix
# rather than on the source value: see check_macro_arithmetic.
MIN_PLAUSIBLE_KCAL = 250
# Vegetarian servings realistically top out around here; see
# classify.macros_implausible_for_veg.
MAX_VEG_PROTEIN_G = 50
# A weight this far below what the same ingredient normally weighs is a
# placeholder rather than a quantity: "Green Beans, 1 g" against a 150 g norm.
#
# The test has to be relative, not a flat threshold. Small gram amounts are
# perfectly real — 5 g of sesame seeds, 2.5 g of sugar in a sauce — and flagging
# those is crying wolf. What marks "Green Beans, 1 g" out is not that it is small
# but that it is 150 times smaller than every other line for the same ingredient.
IMPLAUSIBLY_SMALL_RATIO = 0.1
# Below this many stated weights there is no norm to compare against, so nothing
# is claimed either way.
MIN_WEIGHT_EVIDENCE = 20


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def atwater_kcal(protein_g: float, carbs_g: float, fat_g: float) -> float:
    return (
        KCAL_PER_G["protein_g"] * protein_g
        + KCAL_PER_G["carbs_g"] * carbs_g
        + KCAL_PER_G["fat_g"] * fat_g
    )


@dataclass
class Finding:
    """One proposed correction, before it is written."""

    field: str
    old_value: float | None
    new_value: float | None
    reason: str
    source: str = "check"


@dataclass
class AuditResult:
    recipe_id: int
    checked: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    used_llm: bool = False
    verdict: str = "ok"  # ok | corrected | inconclusive
    # Ingredient quantities that are missing or implausible. Reported whatever the
    # verdict, because macros passing their own cross-check says nothing about
    # whether the recipe's quantities are right — and those are what the basket
    # and the planner actually consume.
    ingredient_gaps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "recipe_id": self.recipe_id,
            "verdict": self.verdict,
            "used_llm": self.used_llm,
            "checked": self.checked,
            "ingredient_gaps": self.ingredient_gaps,
            "findings": [
                {
                    "field": f.field,
                    "old_value": f.old_value,
                    "new_value": f.new_value,
                    "reason": f.reason,
                    "source": f.source,
                }
                for f in self.findings
            ],
        }


# --------------------------------------------------------------------------
# Step 1: arithmetic
# --------------------------------------------------------------------------

def check_macro_arithmetic(recipe: Recipe) -> list[Finding]:
    """Reconcile the four macro numbers with each other.

    With all four present and disagreeing, the stated energy is corrected to what
    the macros imply — the macros are three independent numbers against energy's
    one, so they are usually the better evidence. With exactly one missing, it is
    solved for instead, which turns a gap into a fact rather than a guess.

    The exception is when believing the macros would produce a serving too small
    to be a meal. "King Prawn Linguine, 497 kcal, 26 g carbs" reconciles to 217
    kcal, but a pasta main is not 217 kcal — the carbohydrate figure is the wrong
    one, and no amount of arithmetic can say what it should be. Those are left for
    the composition check rather than confidently made worse.
    """
    values = {f: getattr(recipe, f) for f in MACRO_FIELDS}
    missing = [f for f, v in values.items() if v is None]

    if len(missing) == 1:
        return [_solve_missing(values, missing[0])]
    if missing:
        return []

    implied = atwater_kcal(values["protein_g"], values["carbs_g"], values["fat_g"])
    stated = values["energy_kcal"]
    if not stated:
        return []
    drift = abs(implied - stated) / stated
    if drift <= TOLERANCE:
        return []
    if implied < MIN_PLAUSIBLE_KCAL <= stated:
        return []  # the macros are the unreliable party; see the docstring
    return [
        Finding(
            field="energy_kcal",
            old_value=stated,
            new_value=round(implied, 1),
            reason=(
                f"stated {stated:.0f} kcal, but {values['protein_g']:.0f}g protein + "
                f"{values['carbs_g']:.0f}g carbs + {values['fat_g']:.0f}g fat implies "
                f"{implied:.0f} kcal ({drift:.0%} off)"
            ),
        )
    ]


def _solve_missing(values: dict[str, float | None], missing: str) -> Finding:
    """Fill the one absent macro from the other three."""
    if missing == "energy_kcal":
        implied = atwater_kcal(values["protein_g"], values["carbs_g"], values["fat_g"])
        return Finding(
            field="energy_kcal",
            old_value=None,
            new_value=round(implied, 1),
            reason="energy was missing; derived from the stated macros",
        )
    others = atwater_kcal(
        values["protein_g"] or 0 if missing != "protein_g" else 0,
        values["carbs_g"] or 0 if missing != "carbs_g" else 0,
        values["fat_g"] or 0 if missing != "fat_g" else 0,
    )
    remaining = (values["energy_kcal"] or 0) - others
    grams = max(0.0, remaining / KCAL_PER_G[missing])
    return Finding(
        field=missing,
        old_value=None,
        new_value=round(grams, 1),
        reason=f"{missing} was missing; the energy unaccounted for by the other macros implies it",
    )


def check_plausibility(recipe: Recipe) -> list[str]:
    """Concerns that arithmetic cannot settle, as human-readable strings.

    These are what justify spending a model call: the numbers are self-consistent,
    so only knowing the actual ingredients can say whether they are right.
    """
    concerns: list[str] = []
    if recipe.energy_kcal and recipe.energy_kcal > MAX_PLAUSIBLE_KCAL:
        concerns.append(f"{recipe.energy_kcal:.0f} kcal is too much for one serving")
    # The mismatch the arithmetic declined to resolve, restated as a question for
    # the composition check. Without this it would fall through unexamined.
    if all(getattr(recipe, f) is not None for f in MACRO_FIELDS) and recipe.energy_kcal:
        implied = atwater_kcal(recipe.protein_g, recipe.carbs_g, recipe.fat_g)
        drift = abs(implied - recipe.energy_kcal) / recipe.energy_kcal
        if drift > TOLERANCE and implied < MIN_PLAUSIBLE_KCAL:
            concerns.append(
                f"macros imply only {implied:.0f} kcal against a stated "
                f"{recipe.energy_kcal:.0f}, so one of the macros is wrong"
            )
    if recipe.is_vegetarian and recipe.protein_g and recipe.protein_g > MAX_VEG_PROTEIN_G:
        concerns.append(
            f"{recipe.protein_g:.0f}g protein is implausible for a vegetarian serving"
        )
    if recipe.protein_g and recipe.energy_kcal:
        ratio = protein_energy_ratio(recipe.protein_g, recipe.energy_kcal)
        # Above ~13 g/100 kcal a dish is essentially pure lean protein.
        if ratio and ratio > 13:
            concerns.append(f"{ratio}g protein per 100 kcal is higher than food allows")
    return concerns


# --------------------------------------------------------------------------
# Step 2: composition, via one narrow model call
# --------------------------------------------------------------------------

COMPOSITION_SYSTEM = (
    "You are a food composition reference. Given a list of recipe ingredients, "
    "return per-100g nutrition for each one: kcal, protein, fat and carbohydrate.\n"
    "Rules:\n"
    "- Report the ingredient AS SOLD/RAW unless the name says otherwise.\n"
    "- Use standard reference values (McCance & Widdowson / USDA style). Do not "
    "guess wildly; if an ingredient is a negligible seasoning, give its real values "
    "anyway (they are small).\n"
    "- Return one entry per ingredient, in the order given. Echo the name exactly "
    "as written, without the weight in brackets.\n"
    "- Do NOT compute totals or per-serving values. Per-100g only."
)

COMPOSITION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "kcal_per_100g": {"type": "number"},
                    "protein_per_100g": {"type": "number"},
                    "fat_per_100g": {"type": "number"},
                    "carbs_per_100g": {"type": "number"},
                },
                "required": [
                    "name",
                    "kcal_per_100g",
                    "protein_per_100g",
                    "fat_per_100g",
                    "carbs_per_100g",
                ],
            },
        }
    },
    "required": ["ingredients"],
}


def build_composition_prompt(ingredients: list[RecipeIngredient]) -> str:
    lines = [
        f"- {i.name} ({i.amount_g:.0f} g)" if i.amount_g else f"- {i.name}"
        for i in ingredients
    ]
    return "Ingredients:\n" + "\n".join(lines)


# Kit the source lists among the ingredients. It has no weight because it is not
# food, so it is neither a data gap nor part of any nutrition sum. Matched on word
# boundaries — a substring test reads "cabbage" as containing "bag".
_NON_FOOD = re.compile(
    r"\b(?:skewers?|cocktail sticks?|toothpicks?|foil|baking paper|parchment|"
    r"piping bags?|thermometer)\b",
    re.I,
)


def is_non_food(name: str) -> bool:
    """True for equipment the source ships alongside the ingredients."""
    return bool(_NON_FOOD.search(name))


def edible_ingredients(recipe: Recipe) -> list[RecipeIngredient]:
    """The ingredients that actually contribute to what the dish contains."""
    return [i for i in recipe.ingredients if not is_non_food(i.name)]


def typical_weights(session: Session, recipe: Recipe) -> dict[str, float]:
    """Median stated weight for each of this recipe's ingredients, corpus-wide.

    Scoped to the names in hand rather than the whole table, so an on-demand audit
    stays a small query.
    """
    names = {i.name for i in recipe.ingredients}
    if not names:
        return {}
    rows = session.execute(
        select(RecipeIngredient.name, RecipeIngredient.unit, RecipeIngredient.amount).where(
            RecipeIngredient.name.in_(sorted(names))
        )
    ).all()
    supported = Counter(
        normalize_name(name)
        for name, unit, amount in rows
        if name and unit in _MASS_UNITS and amount is not None and amount >= 10
    )
    medians = _typical_grams_by_name(rows)
    return {k: v for k, v in medians.items() if supported[k] >= MIN_WEIGHT_EVIDENCE}


def composition_blockers(recipe: Recipe, typical: dict[str, float] | None = None) -> list[str]:
    """Why this recipe's ingredients cannot support a nutrition calculation.

    A recipe's macros are the sum of what goes into it, so an ingredient with no
    recorded weight — or one carrying a placeholder instead of a quantity — does
    not merely add uncertainty, it silently subtracts from the total. Computing
    macros anyway yields a confident underestimate, which would then "correct"
    perfectly good source numbers down to it.

    Two things are deliberately *not* treated as problems. A spoon-measured amount
    is trustworthy however small, because the source chose that unit on purpose
    ("Sugar for the Sauce, ½ tsp" really is 2.5 g). And an ingredient with no
    established norm is left alone rather than guessed about.

    Checked *before* the model call, so an unanswerable question costs nothing.
    """
    typical = typical if typical is not None else {}
    blockers: list[str] = []
    edible = [i for i in recipe.ingredients if not is_non_food(i.name)]

    unweighed = sorted(i.name for i in edible if i.amount_g is None)
    if unweighed:
        blockers.append(f"no weight recorded for {', '.join(unweighed)}")

    suspect: list[str] = []
    for ing in edible:
        if ing.amount_g is None or ing.amount_g <= 0:
            continue
        # A deliberate spoon or count measurement is not a placeholder.
        if ing.unit not in _MASS_UNITS:
            continue
        norm = typical.get(normalize_name(ing.name))
        if norm and ing.amount_g < norm * IMPLAUSIBLY_SMALL_RATIO:
            # Quote the source's own figure, and what it is being judged against.
            suspect.append(
                f"{ing.name} ({ing.amount:g} {ing.unit}, typically {norm:g} g)"
            )
    if suspect:
        blockers.append(f"implausible quantity for {', '.join(sorted(suspect))}")
    return blockers


def _composition_key(name: str) -> str:
    """Normalise an ingredient name for matching a model's echo back to a line.

    The prompt lists each ingredient with its weight — ``- Leek (150 g)`` — so
    asking the model to echo "the name" gets ``Leek`` from one run and
    ``Leek (150 g)`` from the next. Dropping a trailing parenthetical settles it
    without loosening the match enough for two different ingredients to collide.
    """
    return re.sub(r"\s*\([^)]*\)\s*$", "", name or "").strip().lower()


def macros_from_composition(
    ingredients: list[RecipeIngredient], composition: dict, servings: int
) -> dict[str, float] | None:
    """Sum per-100g composition over the real gram amounts, per serving.

    The arithmetic lives here rather than in the prompt so the model cannot assert
    a total, only the reference values behind one. ``ingredients`` must be the
    whole recipe: coverage is judged against all of it, because the ingredients
    left out are precisely the ones that would skew the answer.
    """
    entries = composition.get("ingredients", [])
    by_name: dict[str, dict] = {}
    for entry in entries:
        by_name.setdefault(_composition_key(entry.get("name") or ""), entry)
    totals = {"energy_kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
    matched = 0
    for position, ing in enumerate(ingredients):
        entry = by_name.get(_composition_key(ing.name))
        # The schema asks for one entry per ingredient in the order given, so when
        # a name still will not match, position is better evidence than discarding
        # the whole answer — but only when there is exactly one entry per
        # ingredient to be positional about.
        if entry is None and len(entries) == len(ingredients):
            entry = entries[position]
        if entry is None or not ing.amount_g:
            continue
        matched += 1
        scale = ing.amount_g / 100.0
        totals["energy_kcal"] += scale * float(entry.get("kcal_per_100g") or 0)
        totals["protein_g"] += scale * float(entry.get("protein_per_100g") or 0)
        totals["fat_g"] += scale * float(entry.get("fat_per_100g") or 0)
        totals["carbs_g"] += scale * float(entry.get("carbs_per_100g") or 0)
    # Too little of the recipe accounted for to trust a total built from it.
    if not matched or matched < len(ingredients):
        return None
    per_serving = max(1, servings)
    return {k: round(v / per_serving, 1) for k, v in totals.items()}


def check_against_composition(
    recipe: Recipe,
    completer: Completer,
    *,
    model: str | None = None,
    typical: dict[str, float] | None = None,
) -> list[Finding] | None:
    """Recompute the macros from the ingredients and correct whatever disagrees.

    Returns ``None`` when the check could not be carried out — no usable
    ingredients, or an answer covering too little of the recipe to sum. That is
    emphatically not the same as finding nothing wrong, and reporting it as one is
    how a recipe stating 79 g of protein against 16 g of ingredients came back
    "looks correct": the model had echoed the names with their weights attached,
    nothing matched, and an empty finding list read as a clean bill of health.
    """
    edible = edible_ingredients(recipe)
    if not edible or composition_blockers(recipe, typical):
        return None
    composition = completer(
        COMPOSITION_SYSTEM, build_composition_prompt(edible), COMPOSITION_SCHEMA
    )
    computed = macros_from_composition(edible, composition, recipe.base_yield or 2)
    if computed is None:
        return None

    findings: list[Finding] = []
    for field_name, value in computed.items():
        stated = getattr(recipe, field_name)
        if stated is None:
            findings.append(
                Finding(
                    field=field_name,
                    old_value=None,
                    new_value=value,
                    reason="computed from ingredient composition",
                    source="llm",
                )
            )
            continue
        if not stated:
            continue
        drift = abs(value - stated) / stated
        if drift <= TOLERANCE:
            continue
        findings.append(
            Finding(
                field=field_name,
                old_value=stated,
                new_value=value,
                reason=(
                    f"ingredients imply {value:g} against a stated {stated:g} "
                    f"({drift:.0%} off)"
                ),
                source="llm",
            )
        )
    return findings


# --------------------------------------------------------------------------
# Applying and reverting
# --------------------------------------------------------------------------

def apply_findings(
    session: Session, recipe: Recipe, findings: list[Finding], *, model: str | None = None
) -> list[RecipeEdit]:
    """Record each finding and project it onto the recipe row."""
    edits: list[RecipeEdit] = []
    for finding in findings:
        if finding.field not in MACRO_FIELDS:
            log.warning("ignoring unsupported audit field %r", finding.field)
            continue
        if finding.new_value is None:
            continue
        current = getattr(recipe, finding.field)
        # Skip a "correction" that would not move the number meaningfully.
        if current and abs(finding.new_value - current) / current <= CLOSE_ENOUGH:
            continue
        edit = RecipeEdit(
            recipe_id=recipe.id,
            field=finding.field,
            old_value=current,
            new_value=finding.new_value,
            status="applied",
            source=finding.source,
            reason=finding.reason,
            model=model if finding.source == "llm" else None,
        )
        session.add(edit)
        edits.append(edit)
        setattr(recipe, finding.field, finding.new_value)

    if edits:
        # Keep the derived signals consistent with the numbers just changed.
        recipe.protein_energy_ratio = protein_energy_ratio(recipe.protein_g, recipe.energy_kcal)
        recipe.macros_suspect = int(
            macros_suspect(recipe.protein_g, recipe.carbs_g, recipe.fat_g, recipe.energy_kcal)
        )
    return edits


def revert_recipe(session: Session, recipe_id: int) -> int:
    """Restore a recipe's original source numbers and mark its edits reverted.

    The value restored is ``old_value`` from the *earliest* applied edit for each
    field, which is the pristine source figure however many times it was since
    corrected.
    """
    recipe = session.get(Recipe, recipe_id)
    if recipe is None:
        raise ValueError(f"no recipe {recipe_id}")
    applied = session.scalars(
        select(RecipeEdit)
        .where(RecipeEdit.recipe_id == recipe_id, RecipeEdit.status == "applied")
        .order_by(RecipeEdit.created_at, RecipeEdit.id)
    ).all()
    originals: dict[str, float | None] = {}
    for edit in applied:
        originals.setdefault(edit.field, edit.old_value)
        edit.status = "reverted"
    for field_name, value in originals.items():
        setattr(recipe, field_name, value)
    if applied:
        recipe.protein_energy_ratio = protein_energy_ratio(recipe.protein_g, recipe.energy_kcal)
        recipe.macros_suspect = int(
            macros_suspect(recipe.protein_g, recipe.carbs_g, recipe.fat_g, recipe.energy_kcal)
        )
    recipe.flagged_suspicious = 0
    session.commit()
    return len(applied)


# --------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------

def audit_recipe(
    session: Session,
    recipe_id: int,
    *,
    completer: Completer | None = None,
    model: str | None = None,
) -> AuditResult:
    """Check one recipe and apply whatever it can justify correcting."""
    recipe = session.scalar(
        select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.ingredients))
    )
    if recipe is None:
        raise ValueError(f"no recipe {recipe_id}")

    result = AuditResult(recipe_id=recipe_id)
    result.checked.append("macro arithmetic")
    result.checked.append("ingredient quantities")
    typical = typical_weights(session, recipe)
    result.ingredient_gaps = composition_blockers(recipe, typical)
    findings = check_macro_arithmetic(recipe)

    # Only pay for a model when the arithmetic is clean but something still looks
    # off — an inconsistency is already explained by the numbers themselves.
    if not findings:
        concerns = check_plausibility(recipe)
        # A hand-raised flag is itself a reason to look. The plausibility checks
        # are a cost gate — they decide whether one model call is worth paying for
        # unprompted — not a second opinion on a person's report, and they were
        # never calibrated to be one. Mushroom and Pancetta Risotto states 79 g of
        # protein per serving against ingredients holding around 16 g, and passes
        # every one of them: its macros agree with each other to 3%, it is not
        # vegetarian, and 10.8 g per 100 kcal sits under the "more than food
        # allows" line. Answering that report with "looks correct" without having
        # run the one check that could settle it is the failure worth fixing.
        reasons = concerns or (["flagged by hand"] if recipe.flagged_suspicious else [])
        if reasons and result.ingredient_gaps:
            # The one check that could settle it needs quantities this recipe does
            # not have. Say so instead of computing a confident underestimate.
            result.verdict = "inconclusive"
            log.info(
                "recipe %d: %s, but ingredients are unusable: %s",
                recipe_id, reasons, result.ingredient_gaps,
            )
        elif reasons:
            result.checked.append("ingredient composition")
            if completer is None:
                result.verdict = "inconclusive"
                log.info("recipe %d: %s, but no completer available", recipe_id, reasons)
            else:
                result.used_llm = True
                computed = check_against_composition(
                    recipe, completer, model=model, typical=typical
                )
                if computed is None:
                    result.verdict = "inconclusive"
                    log.info(
                        "recipe %d: %s, but the composition answer did not cover it",
                        recipe_id, reasons,
                    )
                else:
                    findings = computed

    edits = apply_findings(session, recipe, findings, model=model)
    recipe.audited_at = _utcnow()
    # The flag has been answered either way; the edits are the lasting record.
    recipe.flagged_suspicious = 0
    session.commit()

    result.findings = [
        Finding(
            field=e.field,
            old_value=e.old_value,
            new_value=e.new_value,
            reason=e.reason or "",
            source=e.source,
        )
        for e in edits
    ]
    if result.findings:
        result.verdict = "corrected"
    elif result.verdict != "inconclusive":
        result.verdict = "ok"
    return result


# --------------------------------------------------------------------------
# Background job
# --------------------------------------------------------------------------

@dataclass
class AuditJob:
    job_id: str
    recipe_id: int
    status: str = "running"  # running | done | failed
    error: str | None = None
    result: dict | None = None

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "recipe_id": self.recipe_id,
            "status": self.status,
            "error": self.error,
            "result": self.result,
        }


class _AuditRegistry:
    """In-memory job tracking, mirroring the mapping generate registry.

    Not persisted: the edits themselves are committed, so losing a job record
    costs nothing but the progress spinner.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, AuditJob] = {}

    def get(self, job_id: str) -> AuditJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, recipe_id: int) -> AuditJob:
        with self._lock:
            job = AuditJob(job_id=uuid.uuid4().hex[:12], recipe_id=recipe_id)
            self._jobs[job.job_id] = job
            return job


REGISTRY = _AuditRegistry()


def _default_completer() -> Completer | None:
    """The cheap model, or None when no key is configured.

    A missing key must degrade to "arithmetic only" rather than failing the job:
    the deterministic checks are the valuable part and need nothing.
    """
    from app.mapping.openai_client import OpenAIError, OpenAIJSONClient

    try:
        return OpenAIJSONClient(model=config.AUDIT_MODEL)
    except OpenAIError as exc:
        log.warning("composition check unavailable: %s", exc)
        return None


def start_background(
    session_factory: sessionmaker[Session], recipe_id: int, *, completer: Completer | None = None
) -> AuditJob:
    """Run :func:`audit_recipe` on a worker thread and return the job handle."""
    job = REGISTRY.start(recipe_id)

    def run() -> None:
        try:
            resolved = completer if completer is not None else _default_completer()
            with session_factory() as session:
                result = audit_recipe(
                    session, recipe_id, completer=resolved, model=config.AUDIT_MODEL
                )
            job.result = result.as_dict()
            job.status = "done"
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
            log.exception("audit job failed for recipe %d", recipe_id)

    threading.Thread(target=run, name=f"audit-{job.job_id}", daemon=True).start()
    return job


def flag_recipe(session: Session, recipe_id: int) -> Recipe:
    """Mark a recipe as needing a second look, without running it yet."""
    recipe = session.get(Recipe, recipe_id)
    if recipe is None:
        raise ValueError(f"no recipe {recipe_id}")
    recipe.flagged_suspicious = 1
    session.commit()
    return recipe
