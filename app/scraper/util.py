"""Small parsing helpers shared by the source adapters."""
from __future__ import annotations

import re
from html.parser import HTMLParser

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def parse_iso8601_duration_minutes(value: str | None) -> int | None:
    """Convert an ISO-8601 duration (e.g. ``PT45M``) to whole minutes.

    Returns ``None`` for missing/unparseable input and for zero-length
    durations such as ``PT0S`` (the source uses these as "unknown").
    """
    if not value:
        return None
    match = _ISO_DURATION.match(value.strip())
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    total = parts["days"] * 1440 + parts["hours"] * 60 + parts["minutes"]
    total += round(parts["seconds"] / 60)
    return total or None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    @property
    def text(self) -> str:
        return "".join(self._chunks)


def strip_html(html: str | None) -> str | None:
    """Return the visible text of an HTML fragment, whitespace-collapsed."""
    if not html:
        return None
    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"\s+", " ", parser.text).strip()
    return text or None
