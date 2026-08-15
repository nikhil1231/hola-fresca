"""The cart ledger, now that more than one shop can be pushed to.

Both shops use the same default account id, so the retailer is the only thing
keeping their claims apart. If it stopped being applied, a Sainsbury's push would
merge against Ocado's ledger and "restore" products into the wrong trolley —
which is why every one of these is about isolation rather than storage.
"""
from __future__ import annotations

from app.cart.ledger import forget_ledger, read_ledger, write_ledger
from app.cart.merge import CartLedger, LedgerLine


def _ledger(**quantities: int) -> CartLedger:
    return CartLedger(
        lines=tuple(
            LedgerLine(sku=sku, quantity=qty) for sku, qty in quantities.items()
        ),
        synced=True,
    )


def test_each_shop_keeps_its_own_claims(factory):
    write_ledger(factory, _ledger(**{"ocado-sku": 2}), retailer="ocado")
    write_ledger(factory, _ledger(**{"sains-sku": 3}), retailer="sainsburys")

    assert read_ledger(factory, retailer="ocado").quantities == {"ocado-sku": 2}
    assert read_ledger(factory, retailer="sainsburys").quantities == {"sains-sku": 3}


def test_the_same_sku_can_be_claimed_at_both_shops(factory):
    # Nothing says two shops cannot stock a product under the same id, and the
    # unique constraint has to allow it or the second push raises.
    write_ledger(factory, _ledger(shared=1), retailer="ocado")
    write_ledger(factory, _ledger(shared=5), retailer="sainsburys")

    assert read_ledger(factory, retailer="ocado").quantities == {"shared": 1}
    assert read_ledger(factory, retailer="sainsburys").quantities == {"shared": 5}


def test_replacing_one_shop_s_ledger_leaves_the_other_alone(factory):
    write_ledger(factory, _ledger(**{"ocado-sku": 2}), retailer="ocado")
    write_ledger(factory, _ledger(**{"sains-sku": 3}), retailer="sainsburys")

    write_ledger(factory, _ledger(), retailer="sainsburys")

    assert read_ledger(factory, retailer="ocado").quantities == {"ocado-sku": 2}
    assert read_ledger(factory, retailer="sainsburys").quantities == {}


def test_forgetting_one_shop_leaves_the_other_synced(factory):
    write_ledger(factory, _ledger(**{"ocado-sku": 2}), retailer="ocado")
    write_ledger(factory, _ledger(**{"sains-sku": 3}), retailer="sainsburys")

    forget_ledger(factory, retailer="sainsburys")

    assert read_ledger(factory, retailer="ocado").synced is True
    # "Never synced" is not the same as "claims nothing" - the merge treats them
    # as opposites, so a forgotten shop has to read as the former again.
    assert read_ledger(factory, retailer="sainsburys").synced is False


def test_a_shop_never_pushed_to_reads_as_never_synced(factory):
    write_ledger(factory, _ledger(**{"ocado-sku": 2}), retailer="ocado")

    assert read_ledger(factory, retailer="sainsburys").synced is False


def test_the_default_retailer_is_the_one_ocado_s_callers_expect(factory):
    """Ocado's own module binds the retailer, so its call sites did not change."""
    from app.ocado.ledger import read_ledger as read_ocado

    write_ledger(factory, _ledger(**{"ocado-sku": 2}), retailer="ocado")

    assert read_ocado(factory).quantities == {"ocado-sku": 2}


def test_accounts_stay_separate_within_a_shop(factory):
    write_ledger(factory, _ledger(home=2), retailer="ocado", account_id="home")
    write_ledger(factory, _ledger(work=1), retailer="ocado", account_id="work")

    assert read_ledger(factory, retailer="ocado", account_id="home").quantities == {"home": 2}
    assert read_ledger(factory, retailer="ocado", account_id="work").quantities == {"work": 1}
