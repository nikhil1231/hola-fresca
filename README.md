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
