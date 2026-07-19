"""Pipeline tests: discover, normalize, and upsert idempotency.

These run fully offline. Discover uses a saved sitemap slice via a stub HTTP
getter; fetch is exercised indirectly by seeding the raw store, so no network is
touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config as app_config
from app.db.models import Recipe, ScrapeState
from app.db.session import init_db, make_engine, make_session_factory
from app.scraper import pipeline, storage
from app.scraper.sources.hellofresh import HelloFreshSource

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DB + raw store rooted in a temp dir."""
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(app_config, "DB_PATH", tmp_path / "test.db")
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    return factory


@pytest.fixture
def source() -> HelloFreshSource:
    return HelloFreshSource()


def test_discover_populates_scrape_state(env, source, monkeypatch):
    sitemap = (FIXTURES / "sitemap_sample.xml").read_bytes()
    monkeypatch.setattr(source, "sitemap_url", "http://stub/sitemap.xml")

    def fake_get(url: str) -> bytes:
        return sitemap

    monkeypatch.setattr(source, "discover", lambda http_get: HelloFreshSource.discover(source, fake_get))

    result = pipeline.discover(source, env)
    assert result.discovered_new == 6

    with env() as session:
        rows = session.query(ScrapeState).all()
        assert len(rows) == 6
        assert all(r.status == "discovered" for r in rows)

    # Re-running discovers nothing new (idempotent).
    result2 = pipeline.discover(source, env)
    assert result2.discovered_new == 0


def test_normalize_from_seeded_raw(env, source):
    payload = json.loads((FIXTURES / "hellofresh_complete.json").read_text())
    source_id = payload["recipeId"]

    with env() as session:
        session.add(
            ScrapeState(
                source="hellofresh",
                source_id=source_id,
                url=payload["canonicalLink"],
                status="fetched",
            )
        )
        session.commit()
    storage.write_raw("hellofresh", source_id, payload, base_dir=app_config.RAW_DIR)

    result = pipeline.normalize(source, env)
    assert result.normalized == 1
    assert result.incomplete == 0

    with env() as session:
        recipe = session.query(Recipe).one()
        assert recipe.source_id == source_id
        assert recipe.is_complete == 1
        assert len(recipe.ingredients) == 13
        assert recipe.protein_g == 51.5
        state = session.query(ScrapeState).one()
        assert state.status == "normalized"
        assert state.normalized_at is not None


def test_normalize_is_idempotent(env, source):
    payload = json.loads((FIXTURES / "hellofresh_complete.json").read_text())
    source_id = payload["recipeId"]
    with env() as session:
        session.add(
            ScrapeState(source="hellofresh", source_id=source_id, url="x", status="fetched")
        )
        session.commit()
    storage.write_raw("hellofresh", source_id, payload, base_dir=app_config.RAW_DIR)

    pipeline.normalize(source, env)
    pipeline.normalize(source, env, force=True)

    with env() as session:
        # Still exactly one recipe and one set of children after re-normalizing.
        assert session.query(Recipe).count() == 1
        recipe = session.query(Recipe).one()
        assert len(recipe.ingredients) == 13


def test_normalize_stub_flags_incomplete(env, source):
    payload = json.loads((FIXTURES / "hellofresh_stub.json").read_text())
    source_id = payload["recipeId"]
    with env() as session:
        session.add(
            ScrapeState(source="hellofresh", source_id=source_id, url="x", status="fetched")
        )
        session.commit()
    storage.write_raw("hellofresh", source_id, payload, base_dir=app_config.RAW_DIR)

    result = pipeline.normalize(source, env)
    assert result.normalized == 1
    assert result.incomplete == 1

    with env() as session:
        assert session.query(Recipe).one().is_complete == 0
