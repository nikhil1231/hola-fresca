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

## HelloFresh (your subscription)

HelloFresh is a connection, not a shop: nothing is priced or pushed there, so it
is not in `app/retailers.py`. Its account sits in `retailer_accounts` under
`retailer="hellofresh"`, under the same rule as the shops (the password crosses one
request; what's kept is the session). Login first sends an HTTP request to the
site's gateway (`https://www.hellofresh.co.uk/gw`). If Cloudflare challenges that
request, a temporary Playwright Chromium browser submits the site's login form
and captures its access/refresh token pair. Refreshing and account operations
always use HTTP. Tokens are saved at
`DATA_DIR/hellofresh/accounts/<key>/session.json`. A rejected token is refreshed once;
after that configured credentials restore an expired session on the next call.
Install the fallback browser with `.venv/bin/python -m playwright install chromium`.
A challenge that requires human verification still needs a browser sign-in and
a configured refresh token.
To obtain one, open your browser's developer tools before signing in to
HelloFresh, select Network, then find the `/gw/login` response and copy its
`refresh_token` into the appropriate env variable. Treat it as a password;
do not include it in logs, screenshots, or chat.

For local runs, credentials belong in the repository-root `.env` (gitignored).
The production infrastructure in `~/infra/stacks/holafresca/compose.yaml` reads
`~/secrets/holafresca.env` instead. After editing production secrets, recreate the
container with `docker compose up -d --force-recreate` from that stack directory;
a container restart alone does not reload its environment. Restart the API
after editing the local file. The first account uses `HOLAFRESCA_HELLOFRESH_EMAIL` and
`HOLAFRESCA_HELLOFRESH_PASSWORD`, with `HOLAFRESCA_HELLOFRESH_FOR` set to its
HolaFresca user's sign-in email (defaults to `HOLAFRESCA_ACCESS_OWNER_EMAIL`).
Add a second account using:

```dotenv
HOLAFRESCA_HELLOFRESH_2_FOR=partner@example.com
HOLAFRESCA_HELLOFRESH_2_EMAIL=partners-hellofresh@example.com
HOLAFRESCA_HELLOFRESH_2_PASSWORD=...
```

Alternatively set `HOLAFRESCA_HELLOFRESH_2_REFRESH_TOKEN`. Slots 2 through 9 are
supported, one account per HolaFresca user. To manage each other's accounts, set
`HOLAFRESCA_HOUSEHOLD` to both HolaFresca sign-in emails, separated by commas.
Account routes accept `?account=<HelloFresh email>`; `GET household/boxes` lists
upcoming boxes across the household.

Endpoints and request bodies were read off the site's own JavaScript, not guessed.
The reads (subscriptions, plans, deliveries, a week, its menu) have since been
confirmed against a real account and the answers kept, trimmed and anonymised, in
`tests/fixtures/hellofresh/`; the gateway answered a bearer token from httpx with
no Cloudflare cookies. Not yet seen live: the writes (skip/un-skip's `PATCH`,
cancel) and `/login` itself. Meals aren't in the deliveries list; each coming
box's are read from `/my-deliveries/menu`. Whether a week can still be skipped is
HelloFresh's own `allowedActions.pause`. Weeks are ISO weeks (`2030-W42`).

| Route (`/api/hellofresh/...`) | Upstream (`/gw/...`) |
|---|---|
| `POST login` `{email, password}`, `POST logout`, `GET status` | `/login`, `/refresh`, `/logout` |
| `GET boxes?weeks=4` (summary: week, date, cutoff, skipped, meals) | `GET /api/customers/me/deliveries?rangeStart&rangeEnd` |
| `POST weeks/{week-or-date}/skip` / `unskip` `{subscription_id?}` | resolves the one live subscription, refuses a week past its cutoff, then the `PATCH` below |
| `POST cancel` `{plan_id?, reason?}` | resolves the one live plan, then `POST /api/plans/{id}/cancel` |
| `GET subscriptions`, `GET subscriptions/{id}` | `/api/customers/me/subscriptions`, `/api/subscriptions/{id}` |
| `GET subscriptions/{id}/weeks/{week}` | `GET /api/subscriptions/{id}/delivery_dates/{week}` |
| `POST subscriptions/{id}/weeks/{week}/skip` / `unskip` | `PATCH` same, `status: PAUSED` / `RUNNING` |
| `GET subscriptions/{id}/product-options`, `POST plans/{id}/product` | `/api/subscriptions/{id}/product_options`, `PATCH /api/plans/{id}` `{productHandle}` |
| `GET plans`, `GET plans/{id}`, `POST plans/{id}/cancel` `{reason?}` | `/api/plans`, `POST /api/plans/{id}/cancel` + `/cancellation/reason` |
| `GET orders`, `GET menu/{week}`, `GET raw/customer`, `GET raw/deliveries` | `/api/customers/me/orders`, `/menus-service/menus`, `/api/customers/me` |

Everything that changes the subscription needs `{"confirm": true}` in the body.
Not mapped yet: choosing the week's meals (it goes through a separate carts
service whose calls weren't readable from the bundles), reactivation, and payment.

## Ocado orders, search and the trolley

`GET /api/ocado/orders?pending=true` lists the caller's orders from
`/api/order/v6/orders[/pending]`, reduced by `app/ocado/orders.py` to delivery
window, edit cutoff (`confirmOrderChangesBy`), total, item count and whether it's
a recurring order.

For shopping by hand, outside the plan (`app/ocado/shop.py`):

| Route (`/api/ocado/...`) | What |
|---|---|
| `GET search?q=&limit=20` | Ocado's search with the account's session (its region's prices and stock) |
| `GET basket` | the live trolley: named lines, total, `can_checkout` and Ocado's `restrictions` (`MISSING_SLOT`, `NOT_REACHED_THRESHOLD`) |
| `POST basket/items` `{items: [{sku, quantity}]}` | absolute quantities, `0` removes; held under the plan push's lock |
| `GET slots`, `POST slots/reserve` | the slot grid, and booking one for the trolley |

Lines set here count as the person's own to the push ledger, as if added on
ocado.com.

## Feed for Noodle

`GET /api/noodle/feed` (bearer `HOLAFRESCA_NOODLE_FEED_TOKEN`; unset turns it off)
is the read-only summary Noodle pulls, covering every connected Ocado and
HelloFresh account in the household:

```json
{"context": "plain text for answering questions",
 "upcoming": [{"title": "Ocado delivery (35 items, £92.34)", "at": "2030-10-20T10:00:00+01:00", "kind": "delivery"},
              {"title": "Ocado order: last chance to edit", "at": "2030-10-19T17:25:00+01:00", "kind": "deadline"}]}
```

The same contract is meant for every system that feeds Noodle. An account whose
session has died is reported in `context` and skipped.

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
