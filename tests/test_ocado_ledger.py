"""Persisting what the last sync put in the Ocado cart.

The ledger is the only thing standing between a sync and your own shopping, so
what matters here is that it survives a round trip intact, that it is *replaced*
rather than accumulated, and above all that "no claims" and "never synced" stay
distinguishable - the merge treats them as opposites.
"""
from __future__ import annotations

from app.ocado.ledger import forget_ledger, read_ledger, write_ledger
from app.ocado.sync import CartLedger, LedgerLine


def _ledger(*lines: LedgerLine) -> CartLedger:
    return CartLedger(lines=lines, synced=True)


def test_the_ledger_round_trips(factory):
    write_ledger(
        factory,
        _ledger(
            LedgerLine(
                sku="sku-a",
                quantity=2,
                name="Maris Piper Potatoes 2kg",
                ingredient="Potatoes",
                ingredient_key="name:potatoes",
            )
        ),
        week_start="2026-08-03",
    )

    stored = read_ledger(factory)

    assert stored.synced is True
    assert stored.quantities == {"sku-a": 2}
    (line,) = stored.lines
    assert (line.name, line.ingredient, line.ingredient_key) == (
        "Maris Piper Potatoes 2kg",
        "Potatoes",
        "name:potatoes",
    )


def test_never_synced_and_owning_nothing_are_told_apart(factory):
    assert read_ledger(factory).synced is False, "a fresh database has never synced"

    write_ledger(factory, CartLedger(lines=(), synced=True))

    after = read_ledger(factory)
    assert after.synced is True, "an emptied cart is not a first run"
    assert after.quantities == {}


def test_writing_replaces_rather_than_accumulates(factory):
    write_ledger(
        factory,
        _ledger(
            LedgerLine(sku="sku-a", quantity=2),
            LedgerLine(sku="sku-b", quantity=1),
        ),
    )
    # The week dropped the recipe sku-b was bought for, so the next sync gives
    # it back and must stop claiming it - a stale claim is how a later push
    # "removes" something you bought yourself.
    write_ledger(factory, _ledger(LedgerLine(sku="sku-a", quantity=3)))

    assert read_ledger(factory).quantities == {"sku-a": 3}


def test_a_zero_claim_is_not_stored(factory):
    write_ledger(
        factory,
        _ledger(
            LedgerLine(sku="sku-a", quantity=2),
            LedgerLine(sku="sku-b", quantity=0),
        ),
    )

    assert read_ledger(factory).quantities == {"sku-a": 2}


def test_forgetting_the_ledger_returns_to_a_first_run(factory):
    write_ledger(factory, _ledger(LedgerLine(sku="sku-a", quantity=2)))

    forget_ledger(factory)

    reset = read_ledger(factory)
    assert (reset.synced, reset.quantities) == (False, {})
