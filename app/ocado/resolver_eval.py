"""How often the shopping resolver gets messy requests right, against the real history with Ocado faked.

    python -m app.ocado.resolver_eval --db /path/to/a/copy.db [--cases tests/fixtures/shopping_cases.json]

Runs every case in the test set through :func:`app.ocado.resolver.plan` with the real model and a fake
coming order (no Ocado calls, and live search is off), and counts how often it picks right, how often it
asks, and how often it's confidently wrong. The last is the one that matters: it decides where the
confidence line sits. Point it at a copy of the database, never the live one.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db.models import Product, Purchase, RetailerAccount, User
from app.db.session import make_engine, make_session_factory
from app.ocado import resolver

CASES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "shopping_cases.json"


class FakeOcado:
    def __init__(self, lines: dict[str, int]):
        self.lines = lines

    def orders(self, pending: bool = False) -> list[dict[str, Any]]:
        return [{"orderId": "eval", "status": "PLACED", "editStatus": "EDITABLE",
                 "dates": {"deliveryStartDate": "2030-10-17T10:00:00+01:00",
                           "confirmOrderChangesBy": "2030-10-16T23:00:00+01:00"}}]

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"items": [{"product": sku, "quantity": q} for sku, q in self.lines.items()]}

    def search(self, term: str, size: int = 20) -> dict[str, Any]:
        return {"products": []}  # the eval measures the history, not Ocado's search


def judge(case: dict[str, Any], out: dict[str, Any], have: dict[str, int], names: dict[str, str]) -> str:
    """right, asked (a right answer only where asking is allowed), or wrong."""
    if out["confidence"] == "unsure":
        return "right" if case.get("unsure") or case.get("unsure_ok") else "asked"
    got = {names.get(sku, sku): q for sku, q in resolver.targets(out["changes"], have).items()}
    if case.get("unsure"):
        return "right" if not got else "wrong"
    return "right" if got in [case["expect"], *case.get("either", [])] else "wrong"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="a copy of the database")
    parser.add_argument("--cases", default=str(CASES))
    parser.add_argument("--only", type=int, nargs="*", help="case numbers (1-based) to run")
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.cases).read_text())
    factory = make_session_factory(make_engine(Path(args.db)))
    tally: Counter[str] = Counter()
    with factory() as db:
        names = dict(db.execute(select(Product.sku, Product.name).where(Product.retailer == "ocado")).all())
        bought = set(db.scalars(select(Purchase.sku)))
        catalogue: dict[str, str] = {}  # name -> sku; a name on several products means the one bought
        for sku_, name in names.items():
            if name not in catalogue or sku_ in bought:
                catalogue[name] = sku_

        def sku(name: str) -> str:
            if name not in catalogue:
                raise SystemExit(f"not in the catalogue: {name}")
            return catalogue[name]

        # Noodle acts as the owner (the lowest user id), on every household Ocado account's history.
        user_id = db.scalar(select(User.id).order_by(User.id).limit(1))
        accounts = db.scalars(select(RetailerAccount).where(RetailerAccount.retailer == "ocado")).all()
        model = resolver.default_model()
        for n, case in enumerate(spec["cases"], 1):
            if args.only and n not in args.only:
                continue
            have = {sku(name): q for name, q in spec["order"].items()}
            pending = [{"op": "add", "sku": sku(name), "quantity": q, "replaces": None}
                       for name, q in (case.get("pending") or {}).items()]
            resolver._searches.clear()
            try:
                fake = FakeOcado(have)
                out = resolver.plan(db, user_id, [(account, fake) for account in accounts], case["said"],
                                    pending=pending, model=model)
                verdict = judge(case, out, have, names)
                shown = out["question"] if out["confidence"] == "unsure" else out["said"] or "(no change)"
            except Exception as exc:  # noqa: BLE001 - a crash is a wrong answer, and worth seeing
                verdict, out, shown = "wrong", {"confidence": "error", "summary": str(exc)}, ""
            tally[verdict] += 1
            print(f"{n:>2} {verdict:<5} {out['confidence']:<6} {' / '.join(case['said'])}\n"
                  f"   → {shown}  [{out.get('summary', '')}]" + (f"  refused: {out['refused']}" if out.get("refused") else ""))
    total = sum(tally.values())
    print(f"\n{tally['right']}/{total} right, {tally['asked']} asked when it needn't have, {tally['wrong']} wrong")


if __name__ == "__main__":
    main()
