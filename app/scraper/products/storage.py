"""Gzipped JSON raw-cache helpers for retailer product scraping."""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app import config

_SAFE_RE = re.compile(r"[^a-z0-9._-]+")


def cache_key(value: str) -> str:
    normalized = value.strip().lower()
    slug = _SAFE_RE.sub("-", normalized).strip("-")[:80] or "item"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def raw_path(
    retailer: str, kind: str, key: str, base_dir: Path | None = None
) -> Path:
    base = base_dir or config.RAW_DIR
    return base / retailer / kind / f"{cache_key(key)}.json.gz"


def write_raw(
    retailer: str, kind: str, key: str, payload: Any, base_dir: Path | None = None
) -> Path:
    path = raw_path(retailer, kind, key, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return path


def read_raw(retailer: str, kind: str, key: str, base_dir: Path | None = None) -> Any:
    with gzip.open(raw_path(retailer, kind, key, base_dir), "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))

