"""Thin, swappable wrapper around the OpenAI structured-output call.

Isolated to one module so the rest of the mapping code depends only on a
``complete(system, user, schema) -> dict`` callable. Tests inject a fake
completer; the propose CLI injects this real one. If a model needs the Responses
API instead of Chat Completions, only this file changes.
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from app import config


class Completer(Protocol):
    def __call__(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]: ...


class OpenAIError(RuntimeError):
    pass


class OpenAIJSONClient:
    def __init__(self, *, api_key: str | None = None, model: str | None = None):
        key = api_key or config.OPENAI_API_KEY
        if not key:
            raise OpenAIError(
                "OPENAI_API_KEY is not set. Add it to the repo-root .env "
                "(see .env.example)."
            )
        self.model = model or config.OPENAI_MODEL
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency is in requirements
            raise OpenAIError("the 'openai' package is not installed") from exc
        self._client = OpenAI(api_key=key)

    def __call__(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "mapping", "schema": schema, "strict": True},
                },
            )
        except Exception as exc:  # noqa: BLE001 - surface any API error with context
            raise OpenAIError(f"OpenAI request failed (model={self.model!r}): {exc}") from exc
        content = response.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenAIError(f"model returned non-JSON content: {content[:200]!r}") from exc
