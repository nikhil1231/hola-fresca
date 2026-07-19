"""Normalizer and extraction tests, driven by saved HelloFresh fixtures."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.scraper.sources.hellofresh import HelloFreshSource, RecipeExtractionError
from app.scraper.util import parse_iso8601_duration_minutes, strip_html

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def source() -> HelloFreshSource:
    return HelloFreshSource()


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_source_id_from_url(source: HelloFreshSource) -> None:
    url = "https://www.hellofresh.co.uk/recipes/crispy-chicken-693adbb51101204cae74ecbc"
    assert source.source_id_from_url(url) == "693adbb51101204cae74ecbc"
    assert source.source_id_from_url("https://example.com/recipes/no-id-here") is None


def test_normalize_complete_recipe(source: HelloFreshSource) -> None:
    recipe = source.normalize(_load("hellofresh_complete.json"), url="")

    assert recipe.source == "hellofresh"
    assert recipe.source_id == "693adbb51101204cae74ecbc"
    assert recipe.name == "Crispy Chicken Goujons and Cheesy Wedges"
    assert recipe.is_complete is True
    assert recipe.prep_time_min == 45
    assert recipe.difficulty == 2
    assert recipe.base_yield == 2

    # Macros are pulled onto the recipe from the nutrition block.
    assert recipe.energy_kcal == 713
    assert recipe.protein_g == 51.5
    assert recipe.fat_g == 26.2
    assert recipe.carbs_g == 76.7


def test_ingredients_joined_with_base_yield_amounts(source: HelloFreshSource) -> None:
    recipe = source.normalize(_load("hellofresh_complete.json"), url="")
    assert len(recipe.ingredients) == 13

    potatoes = next(i for i in recipe.ingredients if i.name == "Potatoes")
    # Base yield (2 servings) amount from the yields[] join.
    assert potatoes.amount == 450
    assert potatoes.unit == "grams"
    assert potatoes.source_ingredient_id == "5a39929ec9fd0814815939c2"


def test_steps_have_text_and_html(source: HelloFreshSource) -> None:
    recipe = source.normalize(_load("hellofresh_complete.json"), url="")
    assert recipe.steps
    first = recipe.steps[0]
    assert first.index == 1
    assert "<p>" in (first.instructions_html or "")
    assert "<p>" not in (first.instructions_text or "")
    assert "Preheat" in (first.instructions_text or "")


def test_allergens_aggregated_from_ingredients(source: HelloFreshSource) -> None:
    recipe = source.normalize(_load("hellofresh_complete.json"), url="")
    names = {a.name for a in recipe.allergens}
    # Mayonnaise contributes Egg and Mustard.
    assert "Egg" in names
    assert "Mustard" in names


def test_normalize_stub_recipe_is_incomplete(source: HelloFreshSource) -> None:
    recipe = source.normalize(_load("hellofresh_stub.json"), url="")
    assert recipe.source_id == "5252b1ec301bbff3428b4757"
    assert recipe.name  # name still present
    assert recipe.ingredients == []
    assert recipe.nutrition == []
    assert recipe.is_complete is False


def test_extract_rejects_page_without_payload(source: HelloFreshSource) -> None:
    with pytest.raises(RecipeExtractionError):
        source.extract(b"<html><body>nope</body></html>", "http://x")


def test_extract_reads_next_data() -> None:
    source = HelloFreshSource()
    payload = json.dumps(
        {"props": {"pageProps": {"ssrPayload": {"recipe": {"recipeId": "abc", "name": "X"}}}}}
    )
    html = (
        b'<html><script id="__NEXT_DATA__" type="application/json">'
        + payload.encode()
        + b"</script></html>"
    )
    recipe = source.extract(html, "http://x")
    assert recipe["recipeId"] == "abc"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("PT45M", 45),
        ("PT1H30M", 90),
        ("PT0S", None),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_iso_duration(value, expected) -> None:
    assert parse_iso8601_duration_minutes(value) == expected


def test_strip_html() -> None:
    assert strip_html("<p>Hello <strong>world</strong></p>") == "Hello world"
    assert strip_html(None) is None
