# HolaFresca

HolaFresca is a full-stack app with a FastAPI backend at the repository root and a React/Vite frontend in `frontend/`.

## Backend

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn main:app --reload
```

The API health endpoint is available at `http://127.0.0.1:8000/api/health`.

## Frontend

```sh
npm --prefix frontend install
npm --prefix frontend run dev
```

The Vite dev server proxies `/api` requests to the FastAPI server.

## Accounts

Everything personal — the plan, ratings, wishlist, hidden recipes, the shopping
schedule, standing pack choices — belongs to a user. Everything shared — the
recipe library, the product cache, ingredient mappings — does not.

There is no login yet. `app.api.deps.get_current_user` resolves the single
account the app bootstraps on first run, and every personal read and write goes
through it, so adding sign-in is a change to that one function. Catalogue writes
(mapping review, manual products, the recipe audit) are already marked
`require_admin`.

## Retailers

The app can price a week at more than one shop. `app/retailers.py` is the list,
and two properties on it are what everything branches on:

* **catalogued** — products can be scraped, mapped to ingredients and priced.
* **shoppable** — a basket can be pushed into the retailer's own cart, which
  needs the whole of `app/ocado`: a login, a session, a cart API and a ledger to
  tell our items from yours.

Ocado is both. Sainsbury's is catalogued only, so a week planned there is priced
and turned into a shopping list you take to the shop yourself; the basket page
hides the checkout tab rather than offering a button that goes nowhere.

Which shop you are in is **per user** — `plan_settings.retailer`, resolved by
`app.api.deps.get_active_retailer`, the companion to `get_current_user`. Between
them they answer *whose* data and *where* they shop, which is what every priced
read needs. Endpoints depend on it rather than reaching for a constant.

The catalogue was already keyed by retailer (`products`, `product_search_hits`,
`ingredient_mappings`, `user_pack_preferences`), so nothing was restructured for
this. What changed is that the modules reading those tables no longer pin the
value to `RETAILER = "ocado"`.

**Product mappings are per-shop rows; ingredient aliases are shared.** An
ingredient approved at Ocado is not approved at Sainsbury's — the products are
different, so that judgement is different. But two recipe names declared to be
the same ingredient stay aliases at every retailer; the Sainsbury's queue does
not ask again whether “basil pesto” and “pesto” are synonyms. Each shop still has
its own review queue and coverage figure, and a shop whose catalogue has not
been scraped honestly reports nothing mapped. To fill one:

```sh
.venv/bin/python -m app.scraper.products --retailer sainsburys discover
.venv/bin/python -m app.scraper.products --retailer sainsburys fetch
.venv/bin/python -m app.scraper.products --retailer sainsburys normalize
.venv/bin/python -m app.mapping --retailer sainsburys propose
```

The order the accepted products come back in is **computed, not asked for**. The
model decides which candidates are the ingredient and what kind of match each is;
`app/mapping/ordering.py` then sorts them — match type first, then a blend of
unit price, confidence-adjusted rating and the model's own ordering — using the
same maths that colours the metric pills on the review page, so the order
explains itself. Retuning that balance costs a re-sort, not another pass:

```sh
.venv/bin/python -m app.mapping --retailer sainsburys reorder
```

Adding a third shop is a row in `app/retailers.py` plus an adapter module in
`app/scraper/products/` registered in `registry.py`. The adapter interface is
whatever `ocado.py` and `sainsburys.py` both expose — `tests/test_sainsburys_products.py`
asserts the two agree on it.

### A note on Sainsbury's

Their newer `/groceries` app is a Next.js build whose product data arrives
through server actions, keyed by a `next-action` build hash that changes on every
deploy and gated behind an A/B cookie. The adapter deliberately does not use it.
Underneath sits the older `/gol-ui` SPA — which is what a fresh session is
actually served — backed by a plain REST API that has been stable for years, and
that is the same catalogue.

Two shapes differ from Ocado and are easy to get wrong: there is **no pack-size
field** (the weight is in the product title, so `"4 x 415g"` has to be multiplied
out rather than read as 415 g), and **shelf life is a display label**
(`"Typical life 14 days"`) sitting in a list that also carries marketing badges.
Note *typical* against Ocado's guaranteed *minimum* — the two are not the same
promise, so Sainsbury's figures run slightly optimistic for the same food.

Both retailers sit behind Akamai and refuse a plain HTTP client, so both are
fetched from inside a real browser session with a persistent per-retailer profile
— see `app/scraper/products/browser.py`. Chrome does not reliably survive a few
hundred searches, so `BrowserSession` relaunches it and retries; without that,
one crash marked every remaining item in the worklist as its own failure. Run the
fetch **headed** (the default) — headless is challenged harder and has been seen
to hang rather than fail.

### Shelf life and the waste model

`app/planner/waste.py` values a leftover by how much of it survives to the next
shop, and reads `shelf_life_days` first. Retailers differ sharply in how much
they state: Ocado publishes a guaranteed minimum life for 33% of its range,
Sainsbury's a *typical* life for 7.5% (and only as a display label —
`"Typical life 14 days"` — alongside marketing badges).

That gap is covered by the category, which is why `SALVAGE_KEYWORDS_BY_RETAILER`
is keyed by shop. Ocado shelves under a storage class ("Fresh & Chilled Food >
…"); Sainsbury's gives a flat set of leaf aisles ("Pulses & beans"). A word is
only unambiguous inside one taxonomy — `"fresh"` is a chiller at Sainsbury's and
a brand range at Ocado — so the tables must not be shared. Without the
Sainsbury's table, 73% of that catalogue fell through to `SALVAGE_UNKNOWN`; with
it, 11%.

Two rules govern that table, both load-bearing: **order matters** (first match
wins, so `"peanut butter"` has to be settled before `"butter"` reaches the
chiller), and **where a word is ambiguous, guess low** — understating how well
something keeps only forgoes a saving, while overstating it buys a big bag of
something that rots.

## The Ocado auth heartbeat

`app.ocado.heartbeat` re-checks each Ocado session roughly daily — jittered,
staggered across accounts, and confined to waking hours. It stops at the silent
refresh (`allow_login=False`), so it can never send anyone a one-time code.

It runs in the server process rather than as a systemd timer beside the backup
job, and that is not incidental: the cookie jar and the browser profile are owned
by the process, so a second writer would refresh `session.json` underneath the
running server, which would then overwrite it from memory.

Every rung the ladder walks is recorded in `ocado_auth_events`, whatever
triggered it. `GET /api/ocado/auth-events` (admin) summarises the one number the
design hangs on: how many silent refreshes there are per full login, and the
longest measured stretch between two logins. A high ratio means an
interactively-logged-in account is a rare chore; a low one means the opposite,
and that anything built on top of it needs rethinking.

Off unless `HOLAFRESCA_OCADO_HEARTBEAT=1` — see `.env.example`.

## Migrations

The schema is evolved with alembic, and `init_db` runs it on start-up — a fresh
database is built from the models and stamped at head, an existing one is
migrated. **A new model needs a migration**, or it will exist in tests and be
missing in production. See `alembic/README`.

## Checks

```sh
.venv/bin/python -m pytest
npm --prefix frontend run build
```
