"""First-cook extraction, validation, layout and durable caching for cook maps."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import threading
import uuid
from typing import Any, Callable

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.db.models import Recipe, RecipeCookMap, RecipeIngredient
from app.mapping.openai_client import Completer, OpenAIJSONClient

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
PROMPT_VERSION = 1
MAX_COLUMNS = 4
PROCESSING_TIMEOUT = timedelta(minutes=15)
MISSING_API_KEY_ERROR = (
    "OPENAI_API_KEY is not set. Add it to the repo-root .env "
    "(see .env.example), then restart the backend."
)


class GraphValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


GRAPH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lanes", "nodes"],
    "properties": {
        "lanes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "name"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                },
            },
        },
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "lane_id",
                    "source_step_index",
                    "title",
                    "detail",
                    "kind",
                    "duration_seconds",
                    "ingredient_ids",
                    "depends_on",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "lane_id": {"type": "string"},
                    "source_step_index": {"type": "integer"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "kind": {"type": "string", "enum": ["active", "passive"]},
                    "duration_seconds": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}]
                    },
                    "ingredient_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}

SYSTEM_PROMPT = """You turn a linear recipe into a conservative cooking DAG.
Nodes are component states/actions; lanes are equipment or component paths. Split source
steps only where it improves clarity. Preserve source order inside every lane and infer
parallel work only when the recipe explicitly permits it. Every node after the first in a
lane must depend on the preceding node in that lane. Merge nodes depend on every incoming
component. There must be exactly one final plated-dish node and every node must reach it.
Use only the supplied ingredient IDs and attach every ingredient to at least one consuming
node. Titles are two or three words. Detail text stays faithful to the source and must not
invent quantities, timings, safety claims or techniques. Classify unattended simmering,
baking, boiling, resting or marinating waits as passive; hands-on work is active. Only emit
a duration when the source states one, in seconds, using the maximum when it gives a range.
Return only the strict JSON schema requested."""


def actionable_ingredients(recipe: Recipe) -> list[RecipeIngredient]:
    def visible(line: RecipeIngredient) -> bool:
        if line.amount is not None and line.amount <= 0:
            return False
        if line.amount_g is not None and line.amount_g <= 0:
            return False
        return line.amount is not None or line.amount_g is not None

    return [
        line
        for line in sorted(
            recipe.ingredients, key=lambda item: (item.position is None, item.position or 0, item.id)
        )
        if visible(line)
    ]


def source_payload(recipe: Recipe) -> dict[str, Any]:
    ingredients = actionable_ingredients(recipe)
    steps = sorted(recipe.steps, key=lambda step: (step.index, step.id))

    def number(value: float | None) -> float | None:
        # SQLAlchemy returns newly assigned integer-looking values as ints until
        # the relationship is reloaded, then SQLite returns floats. Canonicalise
        # before hashing so a process restart does not invalidate every graph.
        return float(value) if value is not None else None

    return {
        "recipe_id": recipe.id,
        "name": recipe.name,
        "ingredients": [
            {
                "id": line.id,
                "name": line.name,
                "amount": number(line.amount),
                "unit": line.unit,
                "amount_g": number(line.amount_g),
            }
            for line in ingredients
        ],
        "steps": [
            {"index": step.index, "text": step.instructions_text or ""}
            for step in steps
        ],
    }


def source_fingerprint(recipe: Recipe) -> str:
    encoded = json.dumps(source_payload(recipe), sort_keys=True, separators=(",", ":"))
    versioned = f"{SCHEMA_VERSION}:{PROMPT_VERSION}:{encoded}".encode("utf-8")
    return hashlib.sha256(versioned).hexdigest()


def _clean_string(value: Any, label: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return ""
    return value.strip()


def _topological_order(nodes: list[dict[str, Any]], errors: list[str]) -> list[str]:
    by_id = {node["id"]: node for node in nodes}
    indegree = {node_id: 0 for node_id in by_id}
    outgoing: dict[str, list[str]] = defaultdict(list)
    order_index = {node["id"]: index for index, node in enumerate(nodes)}
    for node in nodes:
        for dependency in node["depends_on"]:
            if dependency in by_id:
                indegree[node["id"]] += 1
                outgoing[dependency].append(node["id"])
    ready = sorted(
        (node_id for node_id, degree in indegree.items() if degree == 0),
        key=lambda node_id: order_index[node_id],
    )
    result: list[str] = []
    while ready:
        node_id = ready.pop(0)
        result.append(node_id)
        for target in sorted(outgoing[node_id], key=lambda item: order_index[item]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=lambda item: order_index[item])
    if len(result) != len(nodes):
        errors.append("dependencies must form an acyclic graph")
    return result


def validate_graph(
    raw: dict[str, Any],
    *,
    ingredient_ids: set[int],
    step_indices: set[int],
    step_texts: dict[int, str] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise GraphValidationError(["response must be an object"])
    raw_lanes = raw.get("lanes")
    raw_nodes = raw.get("nodes")
    if not isinstance(raw_lanes, list) or not raw_lanes:
        errors.append("lanes must be a non-empty list")
        raw_lanes = []
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append("nodes must be a non-empty list")
        raw_nodes = []

    lanes: list[dict[str, str]] = []
    lane_ids: set[str] = set()
    for index, item in enumerate(raw_lanes):
        if not isinstance(item, dict):
            errors.append(f"lane {index} must be an object")
            continue
        lane_id = _clean_string(item.get("id"), f"lane {index} id", errors)
        name = _clean_string(item.get("name"), f"lane {index} name", errors)
        if lane_id in lane_ids:
            errors.append(f"duplicate lane id {lane_id!r}")
        lane_ids.add(lane_id)
        lanes.append({"id": lane_id, "name": name})

    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for index, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            errors.append(f"node {index} must be an object")
            continue
        node_id = _clean_string(item.get("id"), f"node {index} id", errors)
        lane_id = _clean_string(item.get("lane_id"), f"node {node_id} lane_id", errors)
        title = _clean_string(item.get("title"), f"node {node_id} title", errors)
        detail = _clean_string(item.get("detail"), f"node {node_id} detail", errors)
        if node_id in node_ids:
            errors.append(f"duplicate node id {node_id!r}")
        node_ids.add(node_id)
        if lane_id not in lane_ids:
            errors.append(f"node {node_id!r} uses unknown lane {lane_id!r}")
        source_step = item.get("source_step_index")
        if not isinstance(source_step, int) or source_step not in step_indices:
            errors.append(f"node {node_id!r} uses unknown source step {source_step!r}")
            source_step = min(step_indices) if step_indices else 1
        kind = item.get("kind")
        if kind not in {"active", "passive"}:
            errors.append(f"node {node_id!r} kind must be active or passive")
            kind = "active"
        duration = item.get("duration_seconds")
        if duration is not None and (
            not isinstance(duration, int) or isinstance(duration, bool) or not 1 <= duration <= 86400
        ):
            errors.append(f"node {node_id!r} duration must be null or 1..86400 seconds")
            duration = None
        if (
            duration is not None
            and step_texts is not None
            and not re.search(r"\b(?:seconds?|minutes?|hours?)\b", step_texts.get(source_step, ""), re.I)
        ):
            errors.append(
                f"node {node_id!r} has a duration not stated by source step {source_step}"
            )
        ingredients = item.get("ingredient_ids")
        if not isinstance(ingredients, list) or any(
            not isinstance(value, int) or isinstance(value, bool) for value in ingredients
        ):
            errors.append(f"node {node_id!r} ingredient_ids must be integers")
            ingredients = []
        ingredients = list(dict.fromkeys(ingredients))
        unknown_ingredients = set(ingredients) - ingredient_ids
        if unknown_ingredients:
            errors.append(
                f"node {node_id!r} uses unknown ingredients {sorted(unknown_ingredients)}"
            )
        dependencies = item.get("depends_on")
        if not isinstance(dependencies, list) or any(
            not isinstance(value, str) for value in dependencies
        ):
            errors.append(f"node {node_id!r} depends_on must contain node ids")
            dependencies = []
        dependencies = list(dict.fromkeys(dependencies))
        nodes.append(
            {
                "id": node_id,
                "lane_id": lane_id,
                "source_step_index": source_step,
                "title": title,
                "detail": detail,
                "kind": kind,
                "duration_seconds": duration,
                "ingredient_ids": ingredients,
                "depends_on": dependencies,
            }
        )

    known_nodes = {node["id"] for node in nodes}
    for node in nodes:
        unknown = set(node["depends_on"]) - known_nodes
        if unknown:
            errors.append(f"node {node['id']!r} has unknown dependencies {sorted(unknown)}")
        if node["id"] in node["depends_on"]:
            errors.append(f"node {node['id']!r} cannot depend on itself")

    used_ingredients = {
        ingredient_id for node in nodes for ingredient_id in node["ingredient_ids"]
    }
    missing_ingredients = ingredient_ids - used_ingredients
    if missing_ingredients:
        errors.append(f"ingredients not consumed by any node: {sorted(missing_ingredients)}")

    topo = _topological_order(nodes, errors)
    by_id = {node["id"]: node for node in nodes}
    if len(topo) == len(nodes):
        outgoing: dict[str, set[str]] = {node_id: set() for node_id in topo}
        undirected: dict[str, set[str]] = {node_id: set() for node_id in topo}
        for node in nodes:
            for dependency in node["depends_on"]:
                if dependency not in by_id:
                    continue
                outgoing[dependency].add(node["id"])
                undirected[dependency].add(node["id"])
                undirected[node["id"]].add(dependency)
        sinks = [node_id for node_id, targets in outgoing.items() if not targets]
        if len(sinks) != 1:
            errors.append(f"graph must have exactly one final sink, found {len(sinks)}")
        if topo:
            visited = {topo[0]}
            queue = deque([topo[0]])
            while queue:
                current = queue.popleft()
                for neighbour in undirected[current] - visited:
                    visited.add(neighbour)
                    queue.append(neighbour)
            if visited != set(topo):
                errors.append("graph must be connected")

        position = {node_id: index for index, node_id in enumerate(topo)}
        ancestors: dict[str, set[str]] = {}
        for node_id in topo:
            direct = {dep for dep in by_id[node_id]["depends_on"] if dep in by_id}
            ancestors[node_id] = direct | {
                ancestor for dep in direct for ancestor in ancestors.get(dep, set())
            }
        by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in nodes:
            by_lane[node["lane_id"]].append(node)
        for lane_id, lane_nodes in by_lane.items():
            ordered = sorted(lane_nodes, key=lambda node: position[node["id"]])
            source_order = [node["source_step_index"] for node in ordered]
            if source_order != sorted(source_order):
                errors.append(f"lane {lane_id!r} reverses the source step order")
            for previous, current in zip(ordered, ordered[1:]):
                if previous["id"] not in ancestors[current["id"]]:
                    errors.append(
                        f"lane {lane_id!r} is not a chain between "
                        f"{previous['id']!r} and {current['id']!r}"
                    )

    if errors:
        raise GraphValidationError(errors)
    return {"lanes": lanes, "nodes": nodes}


def layout_graph(validated: dict[str, Any]) -> dict[str, Any]:
    nodes = [dict(node) for node in validated["nodes"]]
    by_id = {node["id"]: node for node in nodes}
    topo_errors: list[str] = []
    topo = _topological_order(nodes, topo_errors)
    outgoing: dict[str, list[str]] = defaultdict(list)
    rows: dict[str, int] = {}
    for node_id in topo:
        dependencies = by_id[node_id]["depends_on"]
        rows[node_id] = max((rows[dep] + 1 for dep in dependencies), default=0)
        for dependency in dependencies:
            outgoing[dependency].append(node_id)
    sink = next(node_id for node_id in topo if not outgoing[node_id])
    main_lane = by_id[sink]["lane_id"]

    lane_nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        lane_nodes[node["lane_id"]].append(node)
    spans = {
        lane_id: (min(rows[node["id"]] for node in members), max(rows[node["id"]] for node in members))
        for lane_id, members in lane_nodes.items()
    }
    merge_rows: dict[str, int] = {}
    for lane_id, members in lane_nodes.items():
        targets = [
            rows[target]
            for node in members
            for target in outgoing[node["id"]]
            if by_id[target]["lane_id"] != lane_id
        ]
        merge_rows[lane_id] = max(targets, default=spans[lane_id][1])

    lane_columns: dict[str, int | None] = {main_lane: 0}
    occupied: dict[int, list[tuple[int, int]]] = {1: [], 2: [], 3: []}
    tributaries = sorted(
        (lane_id for lane_id in lane_nodes if lane_id != main_lane),
        key=lambda lane_id: (
            len(lane_nodes[lane_id]) == 1
            and not any(node["duration_seconds"] for node in lane_nodes[lane_id]),
            merge_rows[lane_id],
            spans[lane_id][0],
            lane_id,
        ),
    )
    for lane_id in tributaries:
        start, end = spans[lane_id]
        column = next(
            (
                candidate
                for candidate in (1, 2, 3)
                if all(end < other_start or start > other_end for other_start, other_end in occupied[candidate])
            ),
            None,
        )
        lane_columns[lane_id] = column
        if column is not None:
            occupied[column].append((start, end))

    per_step_letters: dict[int, int] = defaultdict(int)
    chip_counts: dict[int, int] = defaultdict(int)
    laid_out: list[dict[str, Any]] = []
    for node_id in topo:
        node = dict(by_id[node_id])
        source_step = node["source_step_index"]
        letter_index = per_step_letters[source_step]
        per_step_letters[source_step] += 1
        collapsed = lane_columns[node["lane_id"]] is None
        chip_index = chip_counts[rows[node_id]] if collapsed else 0
        if collapsed:
            chip_counts[rows[node_id]] += 1
        node.update(
            {
                "ref": f"{source_step}{chr(ord('a') + letter_index)}",
                "row": rows[node_id],
                "col": lane_columns[node["lane_id"]] if not collapsed else 3,
                "collapsed": collapsed,
                "chip_index": chip_index,
            }
        )
        node.pop("depends_on")
        laid_out.append(node)

    edges = [
        {
            "source": dependency,
            "target": node["id"],
            "style": (
                "hold"
                if by_id[dependency]["kind"] == "passive"
                or by_id[dependency]["lane_id"] != node["lane_id"]
                else "lane"
            ),
        }
        for node in nodes
        for dependency in node["depends_on"]
    ]
    return {
        "columns": MAX_COLUMNS,
        "row_count": max(rows.values(), default=0) + 1,
        "lanes": validated["lanes"],
        "nodes": laid_out,
        "edges": edges,
    }


def _user_prompt(recipe: Recipe) -> str:
    return json.dumps(source_payload(recipe), ensure_ascii=False, indent=2)


def generate_graph(recipe: Recipe, completer: Completer) -> dict[str, Any]:
    ingredient_ids = {line.id for line in actionable_ingredients(recipe)}
    step_indices = {step.index for step in recipe.steps}
    step_texts = {step.index: step.instructions_text or "" for step in recipe.steps}
    first = completer(SYSTEM_PROMPT, _user_prompt(recipe), GRAPH_SCHEMA)
    try:
        validated = validate_graph(
            first,
            ingredient_ids=ingredient_ids,
            step_indices=step_indices,
            step_texts=step_texts,
        )
    except GraphValidationError as exc:
        repair = (
            "Repair this candidate graph. Keep all valid structure, fix every listed error, "
            "and return the complete graph again.\n\nErrors:\n- "
            + "\n- ".join(exc.errors)
            + "\n\nCandidate:\n"
            + json.dumps(first, ensure_ascii=False)
            + "\n\nRecipe:\n"
            + _user_prompt(recipe)
        )
        second = completer(SYSTEM_PROMPT, repair, GRAPH_SCHEMA)
        validated = validate_graph(
            second,
            ingredient_ids=ingredient_ids,
            step_indices=step_indices,
            step_texts=step_texts,
        )
    return layout_graph(validated)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _fresh(row: RecipeCookMap, fingerprint: str) -> bool:
    return (
        row.source_fingerprint == fingerprint
        and row.schema_version == SCHEMA_VERSION
        and row.prompt_version == PROMPT_VERSION
    )


CompleterFactory = Callable[[], Completer]


def ensure_background(
    session: Session,
    factory: sessionmaker[Session],
    recipe: Recipe,
    *,
    force: bool = False,
    completer_factory: CompleterFactory | None = None,
) -> tuple[RecipeCookMap, bool]:
    """Return the durable row and start exactly one worker when it needs work."""
    fingerprint = source_fingerprint(recipe)
    now = _utcnow()
    current = session.get(RecipeCookMap, recipe.id)
    if current is not None:
        fresh = _fresh(current, fingerprint)
        started = _aware(current.started_at)
        active = current.status == "processing" and started is not None and now - started < PROCESSING_TIMEOUT
        if active:
            return current, False
        if current.status == "processing" and not force:
            current.status = "failed"
            current.error_message = "Cook-map generation was interrupted. Retry when ready."
            current.completed_at = now
            current.updated_at = now
            session.commit()
            return current, False
        if current.status == "ready" and fresh and not force:
            return current, False
        if current.status == "failed" and fresh and not force:
            return current, False

    # Fail the preflight before claiming a generation. Without a key the
    # worker can only fail, often so quickly that Retry appears to do nothing.
    # An injected completer remains usable without process-level credentials.
    if completer_factory is None and not config.OPENAI_API_KEY:
        if current is None:
            current = RecipeCookMap(
                recipe_id=recipe.id,
                status="failed",
                source_fingerprint=fingerprint,
                schema_version=SCHEMA_VERSION,
                prompt_version=PROMPT_VERSION,
                model=config.COOK_MAP_MODEL,
                error_message=MISSING_API_KEY_ERROR,
                attempts=0,
                completed_at=now,
                updated_at=now,
            )
            session.add(current)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                current = session.get(RecipeCookMap, recipe.id)
                if current is None:  # pragma: no cover - defensive race fallback
                    raise RuntimeError("cook-map configuration failure disappeared")
            return current, False
        else:
            current.status = "failed"
            current.graph_json = None
            current.source_fingerprint = fingerprint
            current.schema_version = SCHEMA_VERSION
            current.prompt_version = PROMPT_VERSION
            current.model = config.COOK_MAP_MODEL
            current.error_message = MISSING_API_KEY_ERROR
            current.generation_id = None
            current.started_at = None
            current.completed_at = now
            current.updated_at = now
        session.commit()
        return current, False

    generation_id = uuid.uuid4().hex
    if current is None:
        row = RecipeCookMap(
            recipe_id=recipe.id,
            status="processing",
            source_fingerprint=fingerprint,
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            model=config.COOK_MAP_MODEL,
            attempts=1,
            generation_id=generation_id,
            created_at=now,
            started_at=now,
            updated_at=now,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.get(RecipeCookMap, recipe.id)
            if existing is None:  # pragma: no cover - defensive against a deleted race winner
                raise RuntimeError("cook-map claim disappeared")
            return existing, False
        claimed = True
    else:
        old_generation_id = current.generation_id
        condition = (
            RecipeCookMap.generation_id.is_(None)
            if old_generation_id is None
            else RecipeCookMap.generation_id == old_generation_id
        )
        result = session.execute(
            update(RecipeCookMap)
            .where(RecipeCookMap.recipe_id == recipe.id, condition)
            .values(
                status="processing",
                graph_json=None,
                source_fingerprint=fingerprint,
                schema_version=SCHEMA_VERSION,
                prompt_version=PROMPT_VERSION,
                model=config.COOK_MAP_MODEL,
                error_message=None,
                attempts=RecipeCookMap.attempts + 1,
                generation_id=generation_id,
                started_at=now,
                completed_at=None,
                updated_at=now,
            )
        )
        session.commit()
        claimed = result.rowcount == 1
        row = session.get(RecipeCookMap, recipe.id)
        if row is None:  # pragma: no cover - recipe cascade during the request
            raise RuntimeError("cook-map row disappeared")

    if claimed:
        maker = completer_factory or (
            lambda: OpenAIJSONClient(model=config.COOK_MAP_MODEL)
        )
        threading.Thread(
            target=_run_generation,
            args=(factory, recipe.id, generation_id, maker),
            name=f"cook-map-{recipe.id}-{generation_id[:8]}",
            daemon=True,
        ).start()
    return row, claimed


def _run_generation(
    factory: sessionmaker[Session],
    recipe_id: int,
    generation_id: str,
    completer_factory: CompleterFactory,
) -> None:
    try:
        with factory() as session:
            recipe = session.get(Recipe, recipe_id)
            if recipe is None:
                raise RuntimeError("recipe no longer exists")
            completer = completer_factory()
            graph = generate_graph(recipe, completer)
            now = _utcnow()
            session.execute(
                update(RecipeCookMap)
                .where(
                    RecipeCookMap.recipe_id == recipe_id,
                    RecipeCookMap.generation_id == generation_id,
                )
                .values(
                    status="ready",
                    graph_json=json.dumps(graph, ensure_ascii=False, separators=(",", ":")),
                    source_fingerprint=source_fingerprint(recipe),
                    model=getattr(completer, "model", None) or config.COOK_MAP_MODEL,
                    error_message=None,
                    completed_at=now,
                    updated_at=now,
                )
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 - persist a useful fallback state
        log.exception("cook-map generation failed for recipe %d", recipe_id)
        with factory() as session:
            now = _utcnow()
            session.execute(
                update(RecipeCookMap)
                .where(
                    RecipeCookMap.recipe_id == recipe_id,
                    RecipeCookMap.generation_id == generation_id,
                )
                .values(
                    status="failed",
                    error_message=str(exc)[:2000],
                    completed_at=now,
                    updated_at=now,
                )
            )
            session.commit()
