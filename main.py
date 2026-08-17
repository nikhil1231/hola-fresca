from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
import logging
import mimetypes
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.account import router as account_router
from app.api.cart import router as cart_router
from app.api.mapping import router as mapping_router
from app.api.ocado import router as ocado_router
from app.api.pantry import router as pantry_router
from app.api.plan import router as plan_router
from app.api.planner import router as planner_router
from app.api.recipes import router as recipes_router
from app.api.retailers import router as retailers_router
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

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Install the auth-event sink, warm the planner, start the Ocado heartbeat.

    In that order, and all here rather than at import time: each needs the
    session factory this process will actually use, and the heartbeat talks to
    Ocado — none of it belongs in a module that the scraper CLIs and the test
    suite import. The heartbeat is off unless HOLAFRESCA_OCADO_HEARTBEAT is set,
    so a developer running the server locally does not quietly start probing
    Ocado. The warm-up only reads the local catalogue, so it is on by default;
    see app/planner/warmup.py for what it costs and what turns it off.
    """
    from app.api.deps import get_session_factory
    from app.ocado import events, heartbeat
    from app.planner import warmup

    events.set_sink(events.db_sink(get_session_factory))
    warmup.start(get_session_factory)
    heartbeat.start()
    try:
        yield
    finally:
        heartbeat.stop()
        warmup.stop()
        events.set_sink(None)


app = FastAPI(title="HolaFresca", lifespan=lifespan)

app.include_router(account_router)
app.include_router(recipes_router)
app.include_router(mapping_router)
app.include_router(planner_router)
app.include_router(cart_router)
app.include_router(ocado_router)
app.include_router(schedule_router)
app.include_router(plan_router)
app.include_router(pantry_router)
app.include_router(retailers_router)

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


#: Methods that change something. A cross-site GET is a read the browser was
#: always going to allow; these are the ones worth refusing.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Values of ``Sec-Fetch-Site`` that mean "this request came from us".
#: ``none`` is a direct navigation — typed, bookmarked — which no page caused.
_OWN_SITE = frozenset({"same-origin", "same-site", "none"})


class _CrossSiteWriteMiddleware(BaseHTTPMiddleware):
    """Refuse state-changing API calls that a different site caused.

    Cloudflare Access assertions arrive in a cookie as well as a header (that is
    how the page load carries one), and a cookie is attached by the browser to
    requests *any* site can make. So a page on another origin could POST to this
    API and have it arrive fully authenticated as whoever was signed in — push a
    basket, disconnect a shop, or hand a retailer password to the login endpoint.

    Refusing on positive evidence only, rather than requiring proof of same
    origin: a request is rejected when a header says it is cross-site, not when
    one is missing. Every browser capable of being the attacker here sends
    ``Sec-Fetch-Site``, and a cross-origin ``fetch`` or form post sends
    ``Origin`` regardless. What has neither is a script, curl, or the test
    suite — none of which can be aimed at somebody else's session by a web page,
    which is the whole threat.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if request.method in _WRITE_METHODS and path.startswith("/api/"):
            site = request.headers.get("sec-fetch-site", "").strip().lower()
            if site and site not in _OWN_SITE:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-site requests may not change anything"},
                )
            origin = request.headers.get("origin")
            if not site and origin and urlparse(origin).netloc != request.headers.get(
                "host", ""
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-site requests may not change anything"},
                )
        return await call_next(request)


# Added last, so it runs first: a refused request should not reach a route.
app.add_middleware(_NoCacheFrontendMiddleware)
app.add_middleware(_CrossSiteWriteMiddleware)


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
        client-side routes (e.g. /recipes/123) load the app instead of 404ing.

        Except under ``/api/``. This mount is last, so only an API path that
        matched no route reaches it, and answering that with the app's HTML and
        a 200 is a lie that costs debugging time — a removed or misspelled
        endpoint looks like it is working until somebody parses the response. An
        API path that got this far is a 404 and says so in the shape every other
        API error uses.
        """

        async def get_response(self, path: str, scope):
            if path == "api" or path.startswith("api/"):
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code == 404 and "." not in Path(path).name:
                    return await super().get_response("index.html", scope)
                raise

    # Mounted last, so the /api routes above take precedence.
    app.mount("/", _SPAStaticFiles(directory=_DIST, html=True), name="spa")
