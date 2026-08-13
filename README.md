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
