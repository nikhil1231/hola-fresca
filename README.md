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

`app.api.deps.get_current_user` answers *whose* data a request is about: the
address Cloudflare Access signed for, or — over the LAN, where there is no
assertion — the account the app bootstrapped. Every personal read and write goes
through it. Catalogue writes (mapping review, manual products, the recipe audit,
admitting a recipe to the library) are marked `require_admin`, because they
change what everyone else sees.

## The library, and what sits outside it

`app.db.models.in_library` is the one definition of what the app holds: a recipe
the curation rules admitted, **or** one admitted by hand, and not one ruled out.
It lives on the model because `app/api/recipes.py` and `app/planner/index.py`
both apply it, and a recipe browse will show but the planner will not price is a
dead end.

Curation is strict on purpose — it wants a rating count a new or niche dish may
never earn — so roughly two thirds of a complete scrape sits outside. That is a
lot of perfectly cookable food to be unable to *find*, so search can be widened
past the library with `show_uncurated`, and one recipe at a time can be brought
in for good with `POST /api/recipes/{id}/library`, which sets
`manually_included`. It survives the next re-curation for the same reason
`manually_excluded` does: it records a decision rather than a derivation.

Three things about the widened mode are load-bearing:

* **It is a strict superset.** `is_triageable` counts library membership on its
  own rather than demanding `is_complete`, which the scrape derives — otherwise a
  library recipe with a stale flag would *vanish* when the reader asked to see
  more.
* **It is a reading mode.** The detail page opens so there is something to judge,
  and nothing else follows: planning, rating, wishlisting and cooking all still
  go through `_require_library_recipe` and refuse until the recipe is admitted.
* **Uncurated recipes are exempt from the unmapped filter.** Mappings are
  proposed from library lines, so having none is the *normal* state out there;
  holding the triage set to that filter would hide almost everything the mode
  exists to show. Mapping is work that follows admitting a recipe.

Best fit is the exception that proves the second point: it ranks the library
against the week's basket, which is a question an uncurated recipe has no answer
to, so `/api/planner/suggestions` never widens. It only *counts*, and browse
turns that count into an offer to run a plain search instead.

### Connecting a shop

A retailer account belongs to a person, not to the process. `retailer_accounts`
is the registry: one row per user per shop, holding the address they sign in
with and an opaque `key` that names their cookie jar and browser profile on
disk. There is **no password column**, and that is the design rather than an
omission — credentials are an input to one interactive login and nothing more.
You type them into Settings, they cross one request, the login rung uses them,
and what survives is the session they produced.

What that costs is honest to state: when the quiet rungs of the auth ladder can
no longer revive a session, there is no stored password to fall back on and the
shop has to be signed into again. How often that happens is measured rather than
guessed — see the auth heartbeat below.

**No endpoint takes an account id.** `/api/cart/{retailer}/*` resolves the
caller's own row from their identity and the shop in the path; there is no
parameter with which to name somebody else's trolley, and no account picker in
the UI, because you have exactly one connection per shop. Signing out forgets
the session but keeps the row: its key names a browser profile Ocado has learned
to trust, and handing it a brand-new identity makes the next login's invisible
reCAPTCHA far more likely to stall.

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

### Why the scrape stopped needing a browser

Both shops were scraped by driving Chrome, Sainsbury's **headed** — headless was
refused outright, from the same profile, on the same machine. Neither shop needs
a browser at all, and the two of them were refusing for entirely different
reasons that both looked like "Akamai wants a real browser".

**Sainsbury's** checks the **TLS handshake**, not a session. A request carrying
no cookies is answered; the identical URL is denied the moment it arrives over
Python's TLS stack, warm profile or not. The browser was supplying a handshake
and nothing else, which is why headed worked and every attempt to trim it down
did not. `app/scraper/products/http_session.py` presents a browser's handshake
directly (`curl_cffi`, libcurl built against the browser TLS/HTTP-2 profiles).
Chrome, Firefox and Safari profiles are all accepted — the rule rejects
non-browsers rather than admitting one build.

**Ocado** was not checking anything of the sort. Its search endpoint answers a
bare `httpx` request. Only the decorate endpoint is guarded, and it wants the
**CSRF token** that every page carries — the same token `OcadoSession` has always
read for the basket's live stock refresh, against that very endpoint, over plain
HTTP, while the scrape beside it drove a browser to reach it. `OcadoClient` reads
the token once per session and re-reads it on the one refusal Ocado names in a
header (`ecom-csrf-failure`).

Measured against the live shops: a search costs ~0.5s instead of several seconds,
150 Sainsbury's searches ran 149/150 and 100 Ocado searches 100/100, and the
whole thing runs on a host with no display.

Playwright is still a dependency, for exactly one thing: the Ocado **login** in
`app/ocado/auth.py`, which faces a reCAPTCHA. Nothing in `app/scraper/` may
import it, and a test enforces that.

`registry.client(retailer)` returns whichever client an adapter exports as
`Client`; the pipeline and the live-search runner know nothing else about it.

The crash-recovery that `BrowserSession` used to provide is now `_RunHealth` in
the pipeline. Its reason for existing outlived the browser: one bad row is that
row's problem, but ten failures in a row mean the *run* is broken, and it stops
rather than walking the rest of the worklist marking every remaining item as its
own failure. That is the bug it was born from — one Chrome crash once turned 71
untried rows into 71 permanent-looking errors.

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
refresh without credentials, so it can never send anyone a one-time code.

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
