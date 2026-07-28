# Repository Instructions

- Keep the FastAPI backend at the repository root unless a ticket explicitly calls for a different structure.
- Keep the React/Vite app in `frontend/`.
- Run `.venv/bin/python -m pytest` and `npm --prefix frontend run build` before handing off changes.
- Do not commit `.venv`, local environment files, frontend build output, or dependency directories.
- `/data/` is gitignored scrape output. Committed reference data belongs in `app/data/`.
- The Seasoned Pioneers catalogue (`app/data/seasoned_pioneers_catalogue.json`) is a
  hand-captured snapshot, not a scrape target: the store is behind a Cloudflare
  managed challenge and refuses automated clients. Do not add a job that fetches
  it, and do not work around the challenge. Refresh it via
  `--retailer seasoned_pioneers refresh --from FILE`.
