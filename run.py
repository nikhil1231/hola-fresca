"""LAN / testing entry point: serve the API and the built SPA on one port.

    HOST=0.0.0.0 PORT=8100 python run.py

Defaults to 0.0.0.0:8100 so the app is reachable across the LAN/VPN. The
frontend must be built first (`npm --prefix frontend run build`) for the SPA to
be served; without it, only the API is available. If a `.env.local` file exists
at the project root (KEY=VALUE per line), it is loaded first.
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_env_local() -> None:
    path = os.path.join(ROOT, ".env.local")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip().lstrip("﻿"), value.strip())


_load_env_local()

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8100"))
    reload = os.environ.get("UVICORN_RELOAD", "false").lower() in {"1", "true", "yes"}
    uvicorn.run("main:app", host=host, port=port, reload=reload)
