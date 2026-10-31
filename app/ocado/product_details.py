"""Read the product description published in Ocado's own structured page data."""
from __future__ import annotations

import json
from html.parser import HTMLParser


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class _StructuredData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.blocks.append("".join(self.current))
            self.current = None


def description(page: str, product_id: str) -> str | None:
    parser = _StructuredData()
    parser.feed(page)
    for block in parser.blocks:
        try:
            payload = json.loads(block)
        except (ValueError, TypeError):
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if isinstance(node.get("@graph"), list):
                nodes.extend(node["@graph"])
            if node.get("@type") != "Product" or str(node.get("sku")) != product_id:
                continue
            value = node.get("description")
            if isinstance(value, str) and value.strip():
                text = _Text()
                text.feed(value[:20000])
                return "\n".join(line.strip() for line in "".join(text.parts).splitlines() if line.strip()) or None
    return None
