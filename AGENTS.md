# Repository Instructions

- Keep the FastAPI backend at the repository root unless a ticket explicitly calls for a different structure.
- Keep the React/Vite app in `frontend/`.
- Run `.venv/bin/python -m pytest` and `npm --prefix frontend run build` before handing off changes.
- Do not commit `.venv`, local environment files, frontend build output, or dependency directories.
- Any schema change needs an alembic migration in `alembic/versions/`. `create_all` only runs on a database that has never been built, so a model without a migration passes the tests and is missing in production.
- Personal data hangs off `users`; the shared catalogue does not. New tables that hold someone's choices need a `user_id`, and reads of them need `get_current_user`.
