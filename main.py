from pathlib import Path
import logging
import mimetypes
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.mapping import router as mapping_router
from app.api.ocado import router as ocado_router
from app.api.planner import router as planner_router
from app.api.recipes import router as recipes_router
from app.api.schedule import router as schedule_router

# uvicorn installs handlers for its own loggers but leaves the root logger at
# WARNING, so every log.info in the app was being dropped - including the auth
# ladder's step-by-step record, which is the only account of why a login failed.
# Set here rather than in run.py because the reloader's worker imports this
# module, not that one. Opt in our own namespaces only, so a debug level does not
# also turn on httpx and sqlalchemy.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
for _namespace in ("holafresca", "app"):
    logging.getLogger(_namespace).setLevel(os.environ.get("HOLAFRESCA_LOG_LEVEL", "INFO"))

app = FastAPI(title="HolaFresca")

app.include_router(recipes_router)
app.include_router(mapping_router)
app.include_router(planner_router)
app.include_router(ocado_router)
app.include_router(schedule_router)

# Windows can report Vite's module bundles as text/plain via the registry-backed
# mimetypes table, which modern browsers reject for <script type="module">.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


class _NoCacheFrontendMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


app.add_middleware(_NoCacheFrontendMiddleware)


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "HolaFresca"}


# Serve the built single-page app on the same origin as the API, so one server
# covers the whole app (used by the LAN testing deploy — see deploy/). This is a
# no-op in local dev, where the frontend runs under Vite and frontend/dist does
# not exist; there the API routes above are all that's registered.
_DIST = Path(__file__).parent / "frontend" / "dist"

if _DIST.is_dir():

    class _SPAStaticFiles(StaticFiles):
        """Static files with SPA fallback: unknown paths return index.html so
        client-side routes (e.g. /recipes/123) load the app instead of 404ing."""

        async def get_response(self, path: str, scope):
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code == 404 and "." not in Path(path).name:
                    return await super().get_response("index.html", scope)
                raise

    # Mounted last, so the /api routes above take precedence.
    app.mount("/", _SPAStaticFiles(directory=_DIST, html=True), name="spa")
