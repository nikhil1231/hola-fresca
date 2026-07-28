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

## Spice sourcing (Seasoned Pioneers)

Ocado does not sell HelloFresh's own spice blends — Chermoula, Central American,
North Indian and a couple of dozen more, together about 8,600 recipe lines.
Seasoned Pioneers do, so their catalogue is carried as a second retailer.

It is a **committed snapshot**, `app/data/seasoned_pioneers_catalogue.json`, not a
live scrape: the store sits behind a Cloudflare managed challenge that refuses
automated clients, and dried-spice prices move yearly rather than weekly. Load it
into the product cache with:

```sh
.venv/bin/python -m app.scraper.products --retailer seasoned_pioneers sync
.venv/bin/python -m app.scraper.products --retailer seasoned_pioneers status
```

Then use **Match catalogue** on the mapping page to score the range against every
ingredient the library ships in a sachet or pot, or the same button on a single
ingredient's review page. Matches are offered as candidates only — nothing is
approved automatically. Accepted products are costed by the planner but listed
apart from the online order, the same as manually sourced ones.

To refresh the snapshot, capture the catalogue as described in
`app/scraper/products/seasoned_pioneers.py`, then:

```sh
.venv/bin/python -m app.scraper.products --retailer seasoned_pioneers refresh --from captured.json
```

## Checks

```sh
.venv/bin/python -m pytest
npm --prefix frontend run build
```
