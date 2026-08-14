# Repository Instructions

- Keep the FastAPI backend at the repository root unless a ticket explicitly calls for a different structure.
- Keep the React/Vite app in `frontend/`.
- Run `.venv/bin/python -m pytest` and `npm --prefix frontend run build` before handing off changes.
- Do not commit `.venv`, local environment files, frontend build output, or dependency directories.
- Any schema change needs an alembic migration in `alembic/versions/`. `create_all` only runs on a database that has never been built, so a model without a migration passes the tests and is missing in production.
- Personal data hangs off `users`; the shared catalogue does not. New tables that hold someone's choices need a `user_id`, and reads of them need `get_current_user`.
- The catalogue is per-retailer. Anything reading `products`, `product_search_hits`, `ingredient_mappings` or `user_pack_preferences` takes a `retailer` argument; API endpoints get it from `get_active_retailer`, never from a module constant. Adding a shop should be a row in `app/retailers.py`, not a search for hardcoded `"ocado"`.
- Retailer capabilities are checked, not named. Branch on `Retailer.shoppable` / `.catalogued` rather than on the id, so a shop without a cart integration degrades to a shopping list instead of a broken button.
