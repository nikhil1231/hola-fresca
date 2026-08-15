"""Ocado's view of the cart ledger.

The ledger stopped being Ocado's alone when Sainsbury's became shoppable, so the
storage moved to :mod:`app.cart.ledger` and is keyed by retailer. This module is
what is left: the same three functions with the retailer already bound, so
Ocado's callers read exactly as they did.
"""
from __future__ import annotations

from functools import partial

from app.cart.ledger import forget_ledger as _forget_ledger
from app.cart.ledger import read_ledger as _read_ledger
from app.cart.ledger import write_ledger as _write_ledger

RETAILER = "ocado"

read_ledger = partial(_read_ledger, retailer=RETAILER)
write_ledger = partial(_write_ledger, retailer=RETAILER)
forget_ledger = partial(_forget_ledger, retailer=RETAILER)

__all__ = ["read_ledger", "write_ledger", "forget_ledger", "RETAILER"]
