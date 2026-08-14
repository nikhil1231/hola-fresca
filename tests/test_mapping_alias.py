"""Ingredient aliases: linking near-duplicates onto one canonical mapping."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import IngredientMapping
from app.mapping import service
from app.mapping.candidates import gather_candidates

from tests.conftest import seed_candidates

PESTO = [{"sku": "pesto1", "name": "Sacla Classic Basil Pesto", "price": 2.5, "pack_value": 190, "pack_unit": "g"}]

CANON = "name:basil pesto"
ALIAS = "name:fresh pesto"


def _seed_pair(factory):
    """Two separately-mapped ingredients that are really the same thing."""
    with factory() as s:
        seed_candidates(s, CANON, "Basil Pesto", PESTO, line_count=200)
        seed_candidates(s, ALIAS, "Fresh Pesto", PESTO, line_count=90)
        for key in (CANON, ALIAS):
            ic = gather_candidates(s, key)
            service.save_decision(
                s,
                ic,
                service.DecisionInput(
                    status="approved", accepted=[service.AcceptedInput(sku="pesto1", rank=1)]
                ),
            )


def test_set_alias_links_and_flags_status(factory):
    _seed_pair(factory)
    with factory() as s:
        service.set_alias(s, ALIAS, CANON)

    with factory() as s:
        detail = service.get_detail(s, gather_candidates(s, ALIAS))
        assert detail.alias_of == CANON
        assert detail.status == "alias"
        assert detail.alias_of_name == "Basil Pesto"
        assert service.resolve_alias(s, ALIAS) == CANON
        # The canonical is untouched.
        assert service.resolve_alias(s, CANON) == CANON


def test_clearing_alias_returns_to_review_queue(factory):
    _seed_pair(factory)
    with factory() as s:
        service.set_alias(s, ALIAS, CANON)
        service.set_alias(s, ALIAS, None)

    with factory() as s:
        detail = service.get_detail(s, gather_candidates(s, ALIAS))
        assert detail.alias_of is None
        assert detail.status == "proposed"
        # Its own accepted products survived the round trip.
        assert [c.candidate.sku for c in detail.candidates if c.accepted] == ["pesto1"]


def test_self_alias_is_rejected(factory):
    _seed_pair(factory)
    with factory() as s:
        with pytest.raises(ValueError, match="itself"):
            service.set_alias(s, ALIAS, ALIAS)


def test_alias_cycles_are_rejected(factory):
    _seed_pair(factory)
    with factory() as s:
        service.set_alias(s, ALIAS, CANON)
        with pytest.raises(ValueError, match="cycle"):
            service.set_alias(s, CANON, ALIAS)


def test_chains_are_flattened_to_the_root(factory):
    _seed_pair(factory)
    third = "name:green pesto"
    with factory() as s:
        seed_candidates(s, third, "Green Pesto", PESTO, line_count=40)
        service.save_decision(
            s,
            gather_candidates(s, third),
            service.DecisionInput(status="proposed", accepted=[]),
        )
        service.set_alias(s, ALIAS, CANON)
        # Aliasing onto an alias should point at that alias's root instead.
        service.set_alias(s, third, ALIAS)

    with factory() as s:
        detail = service.get_detail(s, gather_candidates(s, third))
        assert detail.alias_of == CANON
        assert service.resolve_alias(s, third) == CANON


def test_list_aliases(factory):
    _seed_pair(factory)
    with factory() as s:
        service.set_alias(s, ALIAS, CANON)
    with factory() as s:
        rows = service.list_aliases(s)
        assert rows == [(ALIAS, "Fresh Pesto", CANON, "Basil Pesto")]


def test_aliases_are_shared_across_retailers(factory):
    _seed_pair(factory)
    with factory() as s:
        service.set_alias(s, ALIAS, CANON, retailer="ocado")

    with factory() as s:
        row = s.scalar(
            select(IngredientMapping).where(
                IngredientMapping.retailer == "sainsburys",
                IngredientMapping.ingredient_key == ALIAS,
            )
        )
        assert row is not None
        assert row.alias_of == CANON
        assert row.status == "alias"
        assert service.resolve_alias(s, ALIAS, "sainsburys") == CANON


def test_clearing_an_alias_clears_every_retailer(factory):
    _seed_pair(factory)
    with factory() as s:
        service.set_alias(s, ALIAS, CANON)
        service.set_alias(s, ALIAS, None, retailer="sainsburys")

    with factory() as s:
        rows = list(
            s.scalars(
                select(IngredientMapping).where(
                    IngredientMapping.ingredient_key == ALIAS
                )
            )
        )
        assert {row.retailer for row in rows} == {"ocado", "sainsburys"}
        assert all(row.alias_of is None and row.status == "proposed" for row in rows)


def test_a_retailer_decision_cannot_overwrite_a_shared_alias(factory):
    _seed_pair(factory)
    with factory() as s:
        service.set_alias(s, ALIAS, CANON)
        seed_candidates(
            s, ALIAS, "Fresh Pesto", PESTO, line_count=90, retailer="sainsburys"
        )
        service.save_decision(
            s,
            gather_candidates(s, ALIAS, retailer="sainsburys"),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="pesto1", rank=1)],
            ),
            retailer="sainsburys",
        )

    with factory() as s:
        row = s.scalar(
            select(IngredientMapping).where(
                IngredientMapping.retailer == "sainsburys",
                IngredientMapping.ingredient_key == ALIAS,
            )
        )
        assert row.alias_of == CANON
        assert row.status == "alias"
