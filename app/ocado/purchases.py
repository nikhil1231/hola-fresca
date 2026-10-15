"""What the household has bought, kept for good: one row per line of each delivered order.

"Not the usual protein bread, the other one" is a question about the household's
own buying, not about Ocado's shelves: the usual loaf is the one on thirteen
orders, the other one the loaf on three. So the order history is read into
``purchases`` and stays there, and the shopping matcher and Noodle's resolver ask
it instead of guessing.

Reading it is gentle on purpose. Ocado bot-checks bulk use (twenty searches in a
row once had the connection challenged for half an hour, which blocks the app's
own calls too), so this never searches: one call lists an account's orders, one
call per order not yet read fetches its lines, with a pause between them, and
products missing from the catalogue are filled with the batched products call.
The first run backfills everything; after that a run reads only the orders
delivered since. It rides the Ocado heartbeat, which already runs about once a
day per account in waking hours, so a delivery is in the history by the next day.

Each bought product also gets a line about its packaging ("green and white bag,
Warburtons logo"), read once from its image by a vision model, for "the one in
the green packaging".
"""
from __future__ import annotations

import argparse
import logging
import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.db.models import Product, ProductDescription, Purchase, RetailerAccount
from app.ocado.availability import BATCH_SIZE, PRODUCTS_PATH, _is_product_id, _product_nodes
from app.ocado.client import OcadoClient

log = logging.getLogger(__name__)

RETAILER = "ocado"
#: Orders read in one run. The backfill of half a year is about twenty-five; the
#: rest waits for the next run rather than making one long burst of calls.
MAX_ORDERS = 40
#: Seconds between order reads, chosen at random from this range.
PAUSE = (3.0, 8.0)
#: Packaging descriptions written in one run.
MAX_DESCRIBE = 60

#: ``(image_url, product name) -> one line about the packaging``.
Describer = Callable[[str, str], str]


def _pause() -> None:
    time.sleep(random.uniform(*PAUSE))


# --- reading orders ----------------------------------------------------------


def read_orders(db: Session, account: RetailerAccount, client: OcadoClient, *,
                pause: Callable[[], None] = _pause, limit: int = MAX_ORDERS) -> int:
    """Record ``account``'s delivered orders not read yet. Returns how many were read."""
    seen = set(db.scalars(select(Purchase.order_id).where(Purchase.retailer == RETAILER,
                                                          Purchase.account_key == account.key)))
    new = [o for o in client.orders() if o.get("status") == "DELIVERED" and str(o.get("orderId")) not in seen]
    new.sort(key=lambda o: (o.get("dates") or {}).get("deliveryStartDate") or "")  # oldest first
    read = 0
    for order in new[:limit]:
        order_id = str(order["orderId"])
        if read:
            pause()
        lines: dict[str, int] = {}
        for item in client.get_order(order_id).get("items") or []:
            if item.get("product") and item.get("quantity"):
                lines[str(item["product"])] = lines.get(str(item["product"]), 0) + int(item["quantity"])
        delivered = ((order.get("dates") or {}).get("deliveryStartDate") or "")[:10]
        db.add_all(Purchase(user_id=account.user_id, retailer=RETAILER, account_key=account.key, order_id=order_id,
                            sku=sku, quantity=quantity, delivered_on=delivered) for sku, quantity in lines.items())
        db.commit()  # an order at a time, so a failure part way keeps what was read
        read += 1
    if len(new) > limit:
        log.info("purchases: %s has %d more orders to read next time", account.key, len(new) - limit)
    return read


def fill_catalogue(db: Session, client: OcadoClient) -> int:
    """Put bought products the catalogue doesn't have into it, so every purchase has
    a name, brand, pack size and image. One batched call per fifty products."""
    from app.scraper.products.ocado import normalize_product
    from app.scraper.products.pipeline import upsert_product

    bought = set(db.scalars(select(Purchase.sku).where(Purchase.retailer == RETAILER).distinct()))
    known = set(db.scalars(select(Product.sku).where(Product.retailer == RETAILER, Product.sku.in_(bought))))
    missing = [sku for sku in sorted(bought - known) if _is_product_id(sku)]
    added = 0
    for start in range(0, len(missing), BATCH_SIZE):
        response = client.session.request("PUT", PRODUCTS_PATH, json=missing[start:start + BATCH_SIZE],
                                          reauthenticate=False)
        if response.status_code >= 400:
            log.info("purchases: products call answered %s; leaving %d products unnamed", response.status_code,
                     len(missing) - start)
            break
        for node in _product_nodes(response.json() if response.content else {}):
            try:
                upsert_product(db, normalize_product(node))
                added += 1
            except ValueError:
                continue
        db.commit()
    return added


def sync(db: Session, accounts: Iterable[tuple[RetailerAccount, OcadoClient]], *,
         pause: Callable[[], None] = _pause, describer: Describer | None = None) -> dict[str, int]:
    """Read new orders on each account, fill the catalogue, describe new products."""
    orders = products = 0
    last: OcadoClient | None = None
    for account, client in accounts:
        try:
            orders += read_orders(db, account, client, pause=pause)
            last = client
        except Exception as exc:  # noqa: BLE001 - one account's dead session isn't the other's
            db.rollback()
            log.warning("purchases: %s not read: %s", account.key, exc)
    if last is not None:
        try:
            products = fill_catalogue(db, last)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.warning("purchases: catalogue not filled: %s", exc)
    described = describe(db, describer) if describer is not None else 0
    return {"orders": orders, "products": products, "described": described}


def sync_account(account_key: str) -> None:
    """The heartbeat's hook: after a good check of ``account_key``, read its new orders."""
    from app.ocado.session import _default_account_factory, get_shared_session

    with _default_account_factory(config.DB_PATH)() as db:
        account = db.scalar(select(RetailerAccount).where(RetailerAccount.retailer == RETAILER,
                                                          RetailerAccount.key == account_key))
        if account is None:
            return
        result = sync(db, [(account, OcadoClient(get_shared_session(account.key)))], describer=default_describer())
        if any(result.values()):
            log.info("purchases: %s: %s", account_key, result)


# --- what was bought ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bought:
    sku: str
    orders: int
    quantity: int
    first: str
    last: str


def bought(db: Session, user_ids: Iterable[int], skus: Iterable[str] | None = None) -> dict[str, Bought]:
    """``{sku: Bought}`` across the given people's accounts, optionally only for ``skus``."""
    query = (select(Purchase.sku, func.count(func.distinct(Purchase.order_id)), func.sum(Purchase.quantity),
                    func.min(Purchase.delivered_on), func.max(Purchase.delivered_on))
             .where(Purchase.retailer == RETAILER, Purchase.user_id.in_(list(user_ids)))
             .group_by(Purchase.sku))
    if skus is not None:
        query = query.where(Purchase.sku.in_(list(skus)))
    return {sku: Bought(sku, orders, quantity, first, last) for sku, orders, quantity, first, last in db.execute(query)}


def order_count(db: Session, user_ids: Iterable[int]) -> int:
    return db.scalar(select(func.count(func.distinct(Purchase.order_id)))
                     .where(Purchase.retailer == RETAILER, Purchase.user_id.in_(list(user_ids)))) or 0


_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> set[str]:
    """Lower-case words with a trailing plural ``s`` dropped, for loose matching."""
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
            for w in _WORD.findall(text.lower())}


# --- what it looks like ------------------------------------------------------


def describe(db: Session, describer: Describer, *, limit: int = MAX_DESCRIBE) -> int:
    """Describe the packaging of bought products that have an image and no description yet."""
    done = select(ProductDescription.sku).where(ProductDescription.retailer == RETAILER)
    rows = db.execute(
        select(Product.sku, Product.name, Product.image_url)
        .where(Product.retailer == RETAILER, Product.image_url.is_not(None),
               Product.sku.in_(select(Purchase.sku).where(Purchase.retailer == RETAILER)),
               Product.sku.not_in(done))
        .limit(limit)
    ).all()
    written = 0
    for sku, name, image in rows:
        try:
            text = describer(image, name).strip()
        except Exception as exc:  # noqa: BLE001 - the next run tries again
            log.info("purchases: couldn't describe %s: %s", name, exc)
            continue
        if text:
            db.add(ProductDescription(retailer=RETAILER, sku=sku, packaging=text[:300],
                                      model=getattr(describer, "model", None)))
            db.commit()
            written += 1
    return written


class OpenAIDescriber:
    """A vision model's one line about a product's packaging."""

    PROMPT = ("This is a supermarket product photo. In under 15 words, describe the packaging as a shopper would "
              "remember it: main colours, kind of pack (bag, box, tub, bottle...), the brand or logo, anything "
              "distinctive. No price, no ingredients. Example: \"green and white bag, Warburtons logo, seeded "
              "loaf showing through\".")

    def __init__(self, model: str | None = None):
        from openai import OpenAI

        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self.model = model or config.VISION_MODEL
        self._client = OpenAI(api_key=config.OPENAI_API_KEY)

    def __call__(self, image_url: str, name: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": f"{self.PROMPT}\n\nProduct: {name}"},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]}],
        )
        return (response.choices[0].message.content or "").strip().strip('"')


def default_describer() -> Describer | None:
    try:
        return OpenAIDescriber()
    except Exception as exc:  # noqa: BLE001 - history still works without pictures
        log.info("purchases: no vision model: %s", exc)
        return None


def descriptions(db: Session, skus: Iterable[str]) -> dict[str, str]:
    return dict(db.execute(select(ProductDescription.sku, ProductDescription.packaging)
                           .where(ProductDescription.retailer == RETAILER,
                                  ProductDescription.sku.in_(list(skus)))).all())


# --- by hand -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """``python -m app.ocado.purchases``: read every connected account's new orders now.

    Only with the server stopped: it owns the Ocado sessions, and a second process refreshing the same
    cookie jar writes underneath it. With it running, ``POST /api/noodle/purchases/sync`` does the same.
    """
    from app.ocado.session import _default_account_factory, get_shared_session

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--no-describe", action="store_true", help="skip the packaging descriptions")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    with _default_account_factory(config.DB_PATH)() as db:
        accounts = db.scalars(select(RetailerAccount).where(RetailerAccount.retailer == RETAILER,
                                                            RetailerAccount.status == "connected")).all()
        result = sync(db, [(a, OcadoClient(get_shared_session(a.key))) for a in accounts],
                      describer=None if args.no_describe else default_describer())
    print(result)


if __name__ == "__main__":
    main()
