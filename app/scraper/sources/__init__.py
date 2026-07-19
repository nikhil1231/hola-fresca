"""Source adapters. Each adapter turns a source into normalized recipes."""
from __future__ import annotations

from app.scraper.sources.base import RecipeSource
from app.scraper.sources.hellofresh import HelloFreshSource

# Registry of available sources, keyed by their ``name``.
SOURCES: dict[str, RecipeSource] = {
    src.name: src for src in (HelloFreshSource(),)
}

__all__ = ["RecipeSource", "HelloFreshSource", "SOURCES"]
