"""Mapping export: the CSVs are the only copy of the human review decisions.

What is worth pinning down is not the file format but the three properties that
make it a usable backup — every row survives, the surrogate ids that were
dropped were genuinely redundant, and re-exporting an unchanged database
produces an unchanged file (otherwise the git history fills with noise and a
real edit stops being visible).
"""
from __future__ import annotations

import csv

import pytest

from app.backup import exports as exports_mod
from app.db.models import IngredientMapping, IngredientMappingProduct
from app.db.session import init_db, make_engine, make_session_factory


@pytest.fixture
def seeded_db(tmp_path):
    """A throwaway database holding three mappings with two products apiece."""
    db_path = tmp_path / "mappings.db"
    engine = make_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        for key, name, status in (
            ("name:onion", "onion", "approved"),
            ("name:red onion", "red onion", "alias"),
            ("name:saffron", "saffron", "no_match"),
        ):
            mapping = IngredientMapping(
                retailer="ocado", ingredient_key=key, name=name, status=status, line_count=7
            )
            session.add(mapping)
            session.flush()
            for rank, sku in enumerate(("sku-a", "sku-b"), start=1):
                session.add(
                    IngredientMappingProduct(
                        mapping_id=mapping.id,
                        sku=sku,
                        rank=rank,
                        match_type="exact",
                        accepted=1 if rank == 1 else 0,
                        reason="cheapest own-brand" if rank == 1 else None,
                        source="human",
                    )
                )
        session.commit()
    engine.dispose()
    return db_path


def _read(path):
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_export_preserves_every_row_and_its_identity(seeded_db, tmp_path):
    out = tmp_path / "exports"
    results = exports_mod.export_all(db_path=seeded_db, out_dir=out)
    assert [r.rows for r in results] == [3, 6]

    mappings = _read(out / "ingredient_mappings.csv")
    products = _read(out / "ingredient_mapping_products.csv")

    # Natural keys stand in for the dropped surrogate ids: were they not unique,
    # a restore could not tell two rows apart.
    assert len({(r["retailer"], r["ingredient_key"]) for r in mappings}) == 3
    assert len({(r["retailer"], r["ingredient_key"], r["sku"]) for r in products}) == 6
    assert "id" not in mappings[0]
    assert not {"id", "mapping_id", "product_id"} & set(products[0])

    # Each product line carries its parent's natural key, so the join survives a
    # rebuild that renumbers ingredient_mappings.id.
    assert {r["ingredient_key"] for r in products} == {
        "name:onion", "name:red onion", "name:saffron"
    }

    # Sorted by natural key, so an unchanged decision never moves in the diff.
    assert [r["ingredient_key"] for r in mappings] == sorted(
        r["ingredient_key"] for r in mappings
    )

    # Ordinary columns and NULLs both survive the trip.
    saffron = next(r for r in mappings if r["ingredient_key"] == "name:saffron")
    assert saffron["status"] == "no_match"
    assert saffron["line_count"] == "7"
    assert saffron["alias_of"] == ""
    accepted = next(r for r in products if r["rank"] == "1")
    assert accepted["accepted"] == "1"
    assert accepted["reason"] == "cheapest own-brand"


def test_export_is_byte_stable(seeded_db, tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    exports_mod.export_all(db_path=seeded_db, out_dir=first)
    exports_mod.export_all(db_path=seeded_db, out_dir=second)
    for name in ("ingredient_mappings.csv", "ingredient_mapping_products.csv"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_export_refuses_a_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        exports_mod.export_all(db_path=tmp_path / "nope.db", out_dir=tmp_path / "out")
