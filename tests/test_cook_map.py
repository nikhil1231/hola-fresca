from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import pytest

from app import cook_map
from app.db.models import Recipe, RecipeCookMap, RecipeIngredient, RecipeStep


class FakeCompleter:
    model = "fake-map-model"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, system, user, schema):
        self.calls += 1
        return self.answers.pop(0)


def make_recipe(session):
    recipe = Recipe(
        source="test",
        source_id="cook-map",
        url="https://example.com/cook-map",
        name="Mapped supper",
        curated=1,
        is_complete=1,
    )
    recipe.ingredients = [
        RecipeIngredient(name="Onion", amount=1, unit="unit(s)", position=1),
        RecipeIngredient(name="Oil", amount=1, unit="tbsp", position=2),
        RecipeIngredient(name="Chicken", amount=250, unit="grams", position=3),
    ]
    recipe.steps = [
        RecipeStep(index=1, instructions_text="Slice the onion and heat the oil."),
        RecipeStep(index=2, instructions_text="Fry the chicken for 4 minutes, then serve."),
    ]
    session.add(recipe)
    session.commit()
    return recipe


def valid_answer(recipe):
    onion, oil, chicken = [line.id for line in cook_map.actionable_ingredients(recipe)]
    return {
        "lanes": [
            {"id": "prep", "name": "Onion prep"},
            {"id": "pan", "name": "Pan"},
        ],
        "nodes": [
            {
                "id": "slice",
                "lane_id": "prep",
                "source_step_index": 1,
                "title": "Slice onion",
                "detail": "Slice the onion.",
                "kind": "active",
                "duration_seconds": None,
                "ingredient_ids": [onion],
                "depends_on": [],
            },
            {
                "id": "heat",
                "lane_id": "pan",
                "source_step_index": 1,
                "title": "Heat oil",
                "detail": "Heat the oil in the pan.",
                "kind": "active",
                "duration_seconds": None,
                "ingredient_ids": [oil],
                "depends_on": [],
            },
            {
                "id": "fry",
                "lane_id": "pan",
                "source_step_index": 2,
                "title": "Fry chicken",
                "detail": "Fry the chicken for 4 minutes.",
                "kind": "active",
                "duration_seconds": 240,
                "ingredient_ids": [chicken],
                "depends_on": ["heat", "slice"],
            },
            {
                "id": "serve",
                "lane_id": "pan",
                "source_step_index": 2,
                "title": "Plate supper",
                "detail": "Serve the finished dish.",
                "kind": "active",
                "duration_seconds": None,
                "ingredient_ids": [],
                "depends_on": ["fry"],
            },
        ],
    }


def test_generation_validates_and_lays_out_a_graph(factory):
    with factory() as session:
        recipe = make_recipe(session)
        fake = FakeCompleter(valid_answer(recipe))
        graph = cook_map.generate_graph(recipe, fake)

    assert fake.calls == 1
    assert graph["columns"] == 4
    assert graph["row_count"] == 3
    assert [node["ref"] for node in graph["nodes"]] == ["1a", "1b", "2a", "2b"]
    assert max(node["col"] for node in graph["nodes"]) <= 3
    assert graph["nodes"][-1]["title"] == "Plate supper"


def test_invalid_first_answer_gets_one_repair(factory):
    with factory() as session:
        recipe = make_recipe(session)
        broken = valid_answer(recipe)
        broken["nodes"][-1]["depends_on"] = ["missing"]
        fake = FakeCompleter(broken, valid_answer(recipe))

        graph = cook_map.generate_graph(recipe, fake)

    assert graph["nodes"][-1]["id"] == "serve"
    assert fake.calls == 2


def test_second_invalid_answer_fails(factory):
    with factory() as session:
        recipe = make_recipe(session)
        broken = valid_answer(recipe)
        broken["nodes"][0]["ingredient_ids"] = [999999]
        fake = FakeCompleter(broken, broken)

        with pytest.raises(cook_map.GraphValidationError):
            cook_map.generate_graph(recipe, fake)

    assert fake.calls == 2


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda graph: graph["nodes"][0].update(depends_on=["serve"]), "acyclic"),
        (lambda graph: graph["nodes"][2].update(depends_on=["heat"]), "final sink"),
        (lambda graph: graph["nodes"][0].update(lane_id="missing"), "unknown lane"),
        (lambda graph: graph["nodes"][0].update(source_step_index=99), "unknown source step"),
    ],
)
def test_validator_rejects_broken_graphs(factory, mutate, message):
    with factory() as session:
        recipe = make_recipe(session)
        answer = valid_answer(recipe)
        mutate(answer)
        with pytest.raises(cook_map.GraphValidationError) as exc:
            cook_map.validate_graph(
                answer,
                ingredient_ids={line.id for line in cook_map.actionable_ingredients(recipe)},
                step_indices={step.index for step in recipe.steps},
            )
    assert message in str(exc.value)


def test_layout_collapses_a_fifth_concurrent_tributary():
    lanes = [{"id": "main", "name": "Main"}]
    nodes = []
    ingredient_ids = set()
    for index in range(4):
        lane_id = f"prep-{index}"
        lanes.append({"id": lane_id, "name": lane_id})
        ingredient_ids.add(index + 1)
        nodes.append(
            {
                "id": lane_id,
                "lane_id": lane_id,
                "source_step_index": 1,
                "title": "Prep item",
                "detail": "Prepare it.",
                "kind": "active",
                "duration_seconds": None,
                "ingredient_ids": [index + 1],
                "depends_on": [],
            }
        )
    nodes.append(
        {
            "id": "serve",
            "lane_id": "main",
            "source_step_index": 2,
            "title": "Combine all",
            "detail": "Combine and serve.",
            "kind": "active",
            "duration_seconds": None,
            "ingredient_ids": [],
            "depends_on": [node["id"] for node in nodes],
        }
    )
    validated = cook_map.validate_graph(
        {"lanes": lanes, "nodes": nodes},
        ingredient_ids=ingredient_ids,
        step_indices={1, 2},
    )

    graph = cook_map.layout_graph(validated)

    assert graph["columns"] == 4
    assert sum(node["collapsed"] for node in graph["nodes"]) == 1


def test_fingerprint_changes_with_recipe_content(factory):
    with factory() as session:
        recipe = make_recipe(session)
        before = cook_map.source_fingerprint(recipe)
        recipe.steps[0].instructions_text = "Dice the onion and heat the oil."
        after = cook_map.source_fingerprint(recipe)
    assert before != after


def _wait_for_status(factory, recipe_id, expected, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with factory() as session:
            status = session.get(RecipeCookMap, recipe_id).status
        if status == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"cook map did not reach {expected}")


def test_background_cache_is_reused_and_stale_content_regenerates(factory):
    with factory() as session:
        recipe = make_recipe(session)
        recipe_id = recipe.id
        fake = FakeCompleter(valid_answer(recipe), valid_answer(recipe))
        row, started = cook_map.ensure_background(
            session, factory, recipe, completer_factory=lambda: fake
        )
        assert row.status == "processing"
        assert started
    _wait_for_status(factory, recipe_id, "ready")

    with factory() as session:
        recipe = session.get(Recipe, recipe_id)
        _row, started = cook_map.ensure_background(
            session, factory, recipe, completer_factory=lambda: fake
        )
        assert not started
        recipe.steps[0].instructions_text = "Dice the onion and heat the oil."
        session.commit()
        _row, started = cook_map.ensure_background(
            session, factory, recipe, completer_factory=lambda: fake
        )
        assert started
    _wait_for_status(factory, recipe_id, "ready")
    assert fake.calls == 2


def test_failed_job_waits_for_manual_retry(factory):
    with factory() as session:
        recipe = make_recipe(session)
        recipe_id = recipe.id
        broken = valid_answer(recipe)
        broken["nodes"][0]["ingredient_ids"] = [999]
        bad = FakeCompleter(broken, broken)
        cook_map.ensure_background(
            session, factory, recipe, completer_factory=lambda: bad
        )
    _wait_for_status(factory, recipe_id, "failed")

    with factory() as session:
        recipe = session.get(Recipe, recipe_id)
        _row, started = cook_map.ensure_background(
            session, factory, recipe, completer_factory=lambda: bad
        )
        assert not started
        good = FakeCompleter(valid_answer(recipe))
        _row, started = cook_map.ensure_background(
            session, factory, recipe, force=True, completer_factory=lambda: good
        )
        assert started
    _wait_for_status(factory, recipe_id, "ready")


def test_missing_api_key_fails_before_starting_worker(factory, monkeypatch):
    monkeypatch.setattr(cook_map.config, "OPENAI_API_KEY", None)
    with factory() as session:
        recipe = make_recipe(session)
        recipe_id = recipe.id
        row, started = cook_map.ensure_background(session, factory, recipe)

    assert not started
    assert row.status == "failed"
    assert row.attempts == 0
    assert "OPENAI_API_KEY" in row.error_message

    with factory() as session:
        recipe = session.get(Recipe, recipe_id)
        row, started = cook_map.ensure_background(session, factory, recipe, force=True)

    assert not started
    assert row.status == "failed"
    assert row.attempts == 0


def test_abandoned_processing_job_becomes_retryable(factory):
    with factory() as session:
        recipe = make_recipe(session)
        row = RecipeCookMap(
            recipe_id=recipe.id,
            status="processing",
            source_fingerprint=cook_map.source_fingerprint(recipe),
            schema_version=cook_map.SCHEMA_VERSION,
            prompt_version=cook_map.PROMPT_VERSION,
            attempts=1,
            generation_id="abandoned",
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
            updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        session.add(row)
        session.commit()

        returned, started = cook_map.ensure_background(session, factory, recipe)

    assert not started
    assert returned.status == "failed"
    assert "interrupted" in returned.error_message
