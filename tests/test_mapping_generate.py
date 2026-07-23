"""The load-more generate job: worklist selection and the three outcomes.

Both the browser and the LLM are faked, so these stay offline.
"""
from __future__ import annotations

import csv

from sqlalchemy import select

from app.db.models import IngredientMapping
from app.mapping import generate as gen
from app.mapping import live_search

from tests.conftest import seed_candidates

ROWS = [
    (1, "name:broccoli florets", "Broccoli Florets", 120),
    (2, "name:water for the sauce", "Water for the Sauce", 900),
    (3, "name:premium tomato mix", "Premium Tomato Mix", 80),
    (4, "name:macaroni", "Macaroni", 60),
]


def _csv(tmp_path):
    path = tmp_path / "freq.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "rank", "ingredient_key", "source_ingredient_ids", "name", "recipe_count",
                "recipe_pct", "line_count", "metric_unit", "metric_known_pct",
                "median_metric_amount", "mean_metric_amount", "p25_metric_amount",
                "p75_metric_amount", "common_native_amounts", "name_variants",
            ],
        )
        w.writeheader()
        for rank, key, name, lines in ROWS:
            w.writerow({
                "rank": rank, "ingredient_key": key, "source_ingredient_ids": f"sid{rank}",
                "name": name, "recipe_count": 1, "recipe_pct": 0, "line_count": lines,
                "metric_unit": "g", "metric_known_pct": 100, "median_metric_amount": 100,
                "mean_metric_amount": 100, "p25_metric_amount": 50, "p75_metric_amount": 150,
                "common_native_amounts": "", "name_variants": "",
            })
    return path


class FakeRunner:
    """Returns products for some terms and nothing for others."""

    def __init__(self, empty_for=()):
        self.empty_for = set(empty_for)
        self.terms = []

    def search(self, term, timeout=None):
        self.terms.append(term)
        if term in self.empty_for:
            return {"products": []}
        return {"products": [{
            "productId": f"sku-{term[:6].lower().replace(' ', '')}",
            "retailerProductId": "12345",
            "name": f"Ocado {term}",
            "price": {"amount": "1.50", "currency": "GBP"},
            "packSizeDescription": "300g",
        }]}


def fake_complete(system, user, schema):
    import json

    payload = json.loads(user.split("\n", 1)[1])
    skus = [c["sku"] for c in payload["candidates"]]
    return {
        "accepted": [{"sku": s, "rank": i + 1, "match_type": "exact", "reason": "ok"}
                     for i, s in enumerate(skus)],
        "each_to_grams": None,
        "needs_substitution": False,
        "note": "",
    }


def test_pantry_lines_are_detected():
    assert gen.is_pantry_line("Water for the Sauce")
    assert gen.is_pantry_line("Olive Oil for the Dressing")
    assert gen.is_pantry_line("Salt")
    assert not gen.is_pantry_line("Broccoli Florets")
    assert not gen.is_pantry_line("Chicken Breast")


def test_worklist_skips_already_covered(factory, tmp_path):
    with factory() as s:
        seed_candidates(s, "name:broccoli florets", "Broccoli Florets",
                        [{"sku": "b1", "name": "Broccoli"}], line_count=120)
    with factory() as s:
        work = gen.pending_worklist(s, count=10, csv_path=_csv(tmp_path))
    keys = [k for _, k, _, _ in work]
    assert "name:broccoli florets" not in keys  # already has candidates
    assert keys == ["name:water for the sauce", "name:premium tomato mix", "name:macaroni"]


def test_generate_files_the_three_outcomes(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(live_search.storage, "write_raw", lambda *a, **k: None)
    runner = FakeRunner(empty_for={"Premium Tomato Mix"})

    job = gen.generate(
        factory, count=10, complete=fake_complete, runner=runner, csv_path=_csv(tmp_path)
    )

    assert job.total == 4
    assert job.staples == 1      # Water for the Sauce
    assert job.no_match == 1     # Premium Tomato Mix (search returned nothing)
    assert job.added == 2        # Broccoli + Macaroni got proposals
    assert job.errors == 0
    assert job.status == "done"

    # The pantry line never hit Ocado.
    assert "Water for the Sauce" not in runner.terms

    # Assert inside the session: ``products`` is a lazy relationship.
    with factory() as s:
        by_key = {m.ingredient_key: m for m in s.scalars(select(IngredientMapping)).all()}
        staple = by_key["name:water for the sauce"]
        assert staple.status == "approved" and staple.pantry_staple == 1
        assert staple.line_count == 900
        assert not staple.products
        assert by_key["name:premium tomato mix"].status == "no_match"
        proposed = by_key["name:macaroni"]
        assert proposed.status == "proposed" and proposed.products


def test_generate_is_resumable(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(live_search.storage, "write_raw", lambda *a, **k: None)
    runner = FakeRunner()
    gen.generate(factory, count=2, complete=fake_complete, runner=runner, csv_path=_csv(tmp_path))
    first = runner.terms.copy()

    # A second run picks up where the first stopped rather than redoing work.
    gen.generate(factory, count=2, complete=fake_complete, runner=runner, csv_path=_csv(tmp_path))
    assert set(first).isdisjoint(runner.terms[len(first):])
    with factory() as s:
        assert s.query(IngredientMapping).count() == 4


def _boom(*a, **k):
    raise RuntimeError("OPENAI_API_KEY is not set")


def test_missing_llm_still_files_pantry_lines(factory, tmp_path, monkeypatch):
    """A missing API key must not throw away the work that needs no LLM."""
    monkeypatch.setattr(live_search.storage, "write_raw", lambda *a, **k: None)
    runner = FakeRunner()

    job = gen.generate(
        factory, count=10, complete=_boom, runner=runner, csv_path=_csv(tmp_path)
    )

    assert job.status == "done"
    assert job.staples == 1        # the pantry line was filed for free
    assert job.errors == 3         # the three that needed a proposal
    assert job.added == 0

    with factory() as s:
        by_key = {m.ingredient_key: m for m in s.scalars(select(IngredientMapping)).all()}
        assert by_key["name:water for the sauce"].pantry_staple == 1


def test_failed_proposal_is_not_orphaned(factory, tmp_path, monkeypatch):
    """Candidates cached + no mapping row would make the key invisible forever."""
    monkeypatch.setattr(live_search.storage, "write_raw", lambda *a, **k: None)
    gen.generate(factory, count=10, complete=_boom, runner=FakeRunner(), csv_path=_csv(tmp_path))

    with factory() as s:
        by_key = {m.ingredient_key: m for m in s.scalars(select(IngredientMapping)).all()}
        # Searched fine, proposal failed -> visible and reviewable by hand.
        assert by_key["name:macaroni"].status == "needs_review"
        assert "proposal step failed" in by_key["name:macaroni"].llm_notes
        # And it is not offered again as pending work (it has a mapping row now).
        work = gen.pending_worklist(s, count=10, csv_path=_csv(tmp_path))
        assert "name:macaroni" not in [k for _, k, _, _ in work]
