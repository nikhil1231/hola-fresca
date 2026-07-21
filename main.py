from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.recipes import router as recipes_router

app = FastAPI(title="HolaFresca")

app.include_router(recipes_router)


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
                if exc.status_code == 404:
                    return await super().get_response("index.html", scope)
                raise

    # Mounted last, so the /api routes above take precedence.
    app.mount("/", _SPAStaticFiles(directory=_DIST, html=True), name="spa")
