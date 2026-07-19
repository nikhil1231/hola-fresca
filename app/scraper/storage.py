"""Raw payload store.

The extracted source payload for each recipe is written verbatim to a gzipped
JSON file, keyed by ``<source>/<source_id>.json.gz``. This is the "never scrape
twice" layer: the normalize stage reads only from here, so the parser can be
improved and re-run over the whole catalogue with no network access.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from app import config


def raw_path(source: str, source_id: str, base_dir: Path | None = None) -> Path:
    base = base_dir or config.RAW_DIR
    return base / source / f"{source_id}.json.gz"


def write_raw(
    source: str, source_id: str, payload: Any, base_dir: Path | None = None
) -> Path:
    path = raw_path(source, source_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with gzip.open(path, "wb") as fh:
        fh.write(data)
    return path


def read_raw(source: str, source_id: str, base_dir: Path | None = None) -> Any:
    path = raw_path(source, source_id, base_dir)
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


def has_raw(source: str, source_id: str, base_dir: Path | None = None) -> bool:
    return raw_path(source, source_id, base_dir).exists()
