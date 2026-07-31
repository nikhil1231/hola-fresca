"""Persistence for the cart ledger - what the last sync put in the Ocado cart.

The ledger belongs to the Ocado account, not to a browser: one login, one live
cart, and syncing from the laptop after syncing from the phone must see the same
claims. That is why this is a table and not the localStorage the week's plan and
owned-item flags live in - a per-device ledger would read an empty one on the
second device and buy the week twice.

The merge that consumes it lives in :mod:`app.ocado.sync`; nothing here decides
anything, it only reads and replaces.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import OcadoCartLedger, OcadoCartSync
from app.ocado.sync import CartLedger, LedgerLine

#: The sync row is a singleton; its id is fixed so the upsert needs no lookup.
SYNC_ROW_ID = 1


def read_ledger(factory: sessionmaker[Session]) -> CartLedger:
    with factory() as session:
        rows = session.execute(
            select(OcadoCartLedger).order_by(OcadoCartLedger.sku)
        ).scalars().all()
        sync = session.get(OcadoCartSync, SYNC_ROW_ID)
        synced_at = sync.synced_at if sync else None
        week_start = sync.week_start if sync else None
    return CartLedger(
        lines=tuple(
            LedgerLine(
                sku=row.sku,
                quantity=row.quantity,
                name=row.name,
                ingredient=row.ingredient_name,
                ingredient_key=row.ingredient_key,
            )
            for row in rows
            if row.quantity > 0
        ),
        synced=sync is not None,
        synced_at=synced_at,
        week_start=week_start,
    )


def write_ledger(
    factory: sessionmaker[Session],
    ledger: CartLedger,
    *,
    week_start: str | None = None,
) -> None:
    """Replace the ledger wholesale.

    Wholesale rather than merged: a product the week no longer needs has to
    *disappear* from the ledger, and reconciling row by row is how you end up
    with a stale claim on something HF gave back years ago.
    """
    now = datetime.now(timezone.utc)
    with factory() as session:
        session.execute(delete(OcadoCartLedger))
        session.add_all(
            OcadoCartLedger(
                sku=line.sku,
                quantity=line.quantity,
                name=line.name,
                ingredient_key=line.ingredient_key,
                ingredient_name=line.ingredient,
                week_start=week_start,
                synced_at=now,
            )
            for line in ledger.lines
            if line.quantity > 0
        )
        row = session.get(OcadoCartSync, SYNC_ROW_ID)
        if row is None:
            session.add(OcadoCartSync(id=SYNC_ROW_ID, week_start=week_start, synced_at=now))
        else:
            row.week_start = week_start
            row.synced_at = now
        session.commit()


def forget_ledger(factory: sessionmaker[Session]) -> None:
    """Drop every claim and the sync marker with it.

    For starting over after the cart has been meddled with beyond recognition.
    Not needed for checkout: an emptied cart already merges to "HF owns nothing"
    on its own, and the next push writes that back.
    """
    with factory() as session:
        session.execute(delete(OcadoCartLedger))
        session.execute(delete(OcadoCartSync))
        session.commit()
