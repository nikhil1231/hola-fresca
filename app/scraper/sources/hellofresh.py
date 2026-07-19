"""HelloFresh UK source adapter.

Recipe pages are Next.js documents that embed the full recipe object as JSON in
a ``<script id="__NEXT_DATA__">`` tag, at ``props.pageProps.ssrPayload.recipe``.
That object — not the rendered HTML — is the source of truth. Quantities are
split from ingredient metadata: ``ingredients[]`` carries identity (name, type,
allergens, image) while ``yields[]`` carries per-serving-size amounts keyed by
ingredient id, so the two are joined here.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from app.scraper.models import (
    NormalizedAllergen,
    NormalizedIngredient,
    NormalizedNutrition,
    NormalizedRecipe,
    NormalizedStep,
    NormalizedTag,
)
from app.scraper.sources.base import HttpGet
from app.scraper.util import parse_iso8601_duration_minutes, strip_html

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>")
# HelloFresh ids are 24 hex characters; recipe URLs end in "-<id>".
_ID_RE = re.compile(r"-([0-9a-f]{24})/?$")

# Nutrition entry name -> IR field for the denormalised per-portion macros.
_MACRO_FIELDS = {
    "Energy (kcal)": "energy_kcal",
    "Protein": "protein_g",
    "Fat": "fat_g",
    "Carbohydrate": "carbs_g",
}


class RecipeExtractionError(ValueError):
    """Raised when a page does not contain a usable recipe payload."""


class HelloFreshSource:
    name = "hellofresh"
    image_base = "https://img.hellofresh.com/hellofresh_s3"
    sitemap_url = "https://www.hellofresh.co.uk/sitemap_recipe_pages.xml"

    # -- discovery ---------------------------------------------------------

    @staticmethod
    def source_id_from_url(url: str) -> str | None:
        match = _ID_RE.search(url)
        return match.group(1) if match else None

    def discover(self, http_get: HttpGet) -> Iterable[tuple[str, str]]:
        xml = http_get(self.sitemap_url).decode("utf-8")
        seen: set[str] = set()
        for url in _LOC_RE.findall(xml):
            source_id = self.source_id_from_url(url)
            if source_id and source_id not in seen:
                seen.add(source_id)
                yield source_id, url

    # -- extraction --------------------------------------------------------

    def extract(self, page_bytes: bytes, url: str) -> Any:
        html = page_bytes.decode("utf-8", errors="replace")
        match = _NEXT_DATA_RE.search(html)
        if not match:
            raise RecipeExtractionError(f"no __NEXT_DATA__ script found for {url}")
        try:
            data = json.loads(match.group(1))
            recipe = data["props"]["pageProps"]["ssrPayload"]["recipe"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RecipeExtractionError(f"recipe payload missing for {url}: {exc}") from exc
        if not isinstance(recipe, dict) or not recipe.get("recipeId"):
            raise RecipeExtractionError(f"recipe payload malformed for {url}")
        return recipe

    # -- normalisation -----------------------------------------------------

    def normalize(self, payload: Any, url: str) -> NormalizedRecipe:
        r = payload
        source_id = r.get("recipeId") or self.source_id_from_url(url) or ""

        recipe = NormalizedRecipe(
            source=self.name,
            source_id=source_id,
            url=r.get("canonicalLink") or r.get("websiteUrl") or url,
            name=r.get("name") or "",
            headline=r.get("headline") or None,
            slug=r.get("slug") or None,
            description=self._description(r),
            difficulty=r.get("difficulty"),
            prep_time_min=parse_iso8601_duration_minutes(r.get("prepTime")),
            total_time_min=parse_iso8601_duration_minutes(r.get("totalTime")),
            serving_size_g=_as_float(r.get("servingSize")),
            image_path=r.get("imagePath") or None,
        )

        recipe.ingredients = self._ingredients(r, recipe)
        recipe.steps = self._steps(r)
        recipe.nutrition = self._nutrition(r, recipe)
        recipe.tags = self._tags(r)
        recipe.cuisines = self._cuisines(r)
        recipe.allergens = self._allergens(r)
        return recipe

    # -- field helpers -----------------------------------------------------

    @staticmethod
    def _description(r: dict) -> str | None:
        for key in ("description", "descriptionMarkdown", "descriptionHTML", "seoDescription"):
            value = r.get(key)
            if value:
                return strip_html(value)
        return None

    def _ingredients(self, r: dict, recipe: NormalizedRecipe) -> list[NormalizedIngredient]:
        yields = r.get("yields") or []
        base = None
        for y in yields:
            n = y.get("yields")
            if n is None:
                continue
            if base is None or n < base.get("yields", n):
                base = y
        recipe.base_yield = base.get("yields") if base else None

        amounts: dict[str, dict] = {}
        if base:
            for entry in base.get("ingredients", []):
                iid = entry.get("id")
                if iid:
                    amounts[iid] = entry

        result: list[NormalizedIngredient] = []
        for ing in r.get("ingredients", []):
            iid = ing.get("id")
            qty = amounts.get(iid, {})
            result.append(
                NormalizedIngredient(
                    name=ing.get("name") or "",
                    source_ingredient_id=iid,
                    type=ing.get("type") or None,
                    slug=ing.get("slug") or None,
                    amount=_as_float(qty.get("amount")),
                    unit=qty.get("unit") or None,
                    image_path=ing.get("imagePath") or None,
                )
            )
        return result

    @staticmethod
    def _steps(r: dict) -> list[NormalizedStep]:
        steps: list[NormalizedStep] = []
        for i, step in enumerate(r.get("steps", []), start=1):
            html = step.get("instructionsHTML") or step.get("instructions")
            steps.append(
                NormalizedStep(
                    index=step.get("index") or i,
                    instructions_text=strip_html(html),
                    instructions_html=html or None,
                )
            )
        return steps

    @staticmethod
    def _nutrition(r: dict, recipe: NormalizedRecipe) -> list[NormalizedNutrition]:
        result: list[NormalizedNutrition] = []
        for entry in r.get("nutrition", []):
            name = entry.get("name")
            if not name:
                continue
            amount = _as_float(entry.get("amount"))
            result.append(NormalizedNutrition(name=name, amount=amount, unit=entry.get("unit")))
            field = _MACRO_FIELDS.get(name)
            if field is not None:
                setattr(recipe, field, amount)
        return result

    @staticmethod
    def _tags(r: dict) -> list[NormalizedTag]:
        tags: list[NormalizedTag] = []
        for tag in r.get("tags", []):
            name = tag.get("name")
            if name:
                tags.append(NormalizedTag(name=name, type=tag.get("type"), slug=tag.get("slug")))
        return tags

    @staticmethod
    def _cuisines(r: dict) -> list[str]:
        names = []
        for cuisine in r.get("cuisines", []):
            name = cuisine.get("name")
            if name:
                names.append(name)
        return names

    @staticmethod
    def _allergens(r: dict) -> list[NormalizedAllergen]:
        # Recipe-level allergens are often empty; the reliable signal is the
        # union of per-ingredient allergens.
        seen: dict[str, NormalizedAllergen] = {}
        sources = list(r.get("allergens") or [])
        for ing in r.get("ingredients", []):
            sources.extend(ing.get("allergens") or [])
        for allergen in sources:
            name = allergen.get("name")
            if name and name not in seen:
                seen[name] = NormalizedAllergen(name=name, slug=allergen.get("slug"))
        return list(seen.values())


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
