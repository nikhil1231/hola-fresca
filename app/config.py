"""Central configuration and filesystem paths for HolaFresca.

Everything is overridable via environment variables so tests can point at a
throwaway directory without touching the real data store.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Repository root (…/HolaFresca). This file lives at app/config.py.
ROOT_DIR = Path(__file__).resolve().parent.parent

# Load a repo-root .env (gitignored) so CLI jobs and the API both pick up secrets
# such as OPENAI_API_KEY without extra wiring.
load_dotenv(ROOT_DIR / ".env")

DATA_DIR = Path(os.environ.get("HOLAFRESCA_DATA_DIR", ROOT_DIR / "data"))
RAW_DIR = Path(os.environ.get("HOLAFRESCA_RAW_DIR", DATA_DIR / "raw"))
DB_PATH = Path(os.environ.get("HOLAFRESCA_DB_PATH", DATA_DIR / "holafresca.db"))

# HelloFresh CDN base for building absolute image URLs from stored image paths.
# Image paths are stored relative (e.g. "/image/foo.jpg"); the frontend can
# request whatever transformation size it needs.
HELLOFRESH_IMAGE_BASE = "https://img.hellofresh.com/hellofresh_s3"

# OpenAI settings for the ingredient→product mapping proposal pass. The key is
# never committed — it lives in the gitignored .env. The model is overridable so
# a different id can be used without a code change.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("HOLAFRESCA_OPENAI_MODEL", "gpt-5.6-luna")


def db_url(path: Path | None = None) -> str:
    """Return a SQLAlchemy URL for the SQLite database at ``path``."""
    return f"sqlite:///{(path or DB_PATH)}"


def ensure_dirs() -> None:
    """Create the data directories if they do not already exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
