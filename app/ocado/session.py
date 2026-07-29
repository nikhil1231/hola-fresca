"""httpx session wrapper for Ocado web requests."""
from __future__ import annotations

import json
import re
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any

import httpx

from app import config
from app.ocado.auth import AUTH, AuthLadder

BASE_URL = "https://www.ocado.com"
SESSION_PATH = config.DATA_DIR / "ocado" / "session.json"
CSRF_RE = re.compile(r'"csrf"\s*:\s*\{\s*"token"\s*:\s*"([^"]+)"')
AUTH_COOKIE_NAMES = {"global_sid", "ocado_session", "aws-waf-token"}


class OcadoSession:
    """Persisted httpx client with one bounded retry for auth and CSRF churn."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        jar_path: Path | None = None,
        auth: AuthLadder | None = None,
        base_url: str = BASE_URL,
    ):
        self.base_url = base_url.rstrip("/")
        self.jar_path = jar_path or SESSION_PATH
        self.auth = auth or AUTH
        self.client = client or httpx.Client(
            base_url=self.base_url,
            follow_redirects=True,
            timeout=30.0,
            headers={
                "accept": "application/json, text/plain, */*",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
                ),
            },
        )
        self._csrf_token: str | None = None
        self.load()

    def close(self) -> None:
        self.client.close()

    def has_auth_cookies(self) -> bool:
        return any(cookie.name in AUTH_COOKIE_NAMES for cookie in self.client.cookies.jar)

    def load(self) -> None:
        if not self.jar_path.exists():
            return
        payload = json.loads(self.jar_path.read_text(encoding="utf-8"))
        for item in payload.get("cookies", []):
            self.client.cookies.jar.set_cookie(_cookie_from_json(item))
        self._csrf_token = payload.get("csrf_token")

    def save(self) -> None:
        self.jar_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "csrf_token": self._csrf_token,
            "cookies": [_cookie_to_json(cookie) for cookie in self.client.cookies.jar],
        }
        self.jar_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def import_playwright_cookies(self, cookies: list[dict[str, Any]]) -> None:
        for cookie in cookies:
            self.client.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain") or ".ocado.com",
                path=cookie.get("path") or "/",
            )
        self.save()

    def csrf(self, *, force: bool = False) -> str:
        if self._csrf_token and not force:
            return self._csrf_token
        response = self.client.get("/basket")
        response.raise_for_status()
        match = CSRF_RE.search(response.text)
        if not match:
            raise RuntimeError("could not find Ocado CSRF token")
        self._csrf_token = match.group(1)
        self.save()
        return self._csrf_token

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        method = method.upper()
        if method not in {"GET", "HEAD", "OPTIONS"}:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("x-csrf-token", self.csrf())
            headers.setdefault("ecom-request-source", "web")
            kwargs["headers"] = headers

        response = self.client.request(method, url, **kwargs)
        if self._is_csrf_failure(response):
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["x-csrf-token"] = self.csrf(force=True)
            headers.setdefault("ecom-request-source", "web")
            kwargs["headers"] = headers
            response = self.client.request(method, url, **kwargs)
        if response.status_code == 401:
            state = self.auth.ensure_authenticated(self)
            if state == "ready":
                response = self.client.request(method, url, **kwargs)
        self.save()
        return response

    @staticmethod
    def _is_csrf_failure(response: httpx.Response) -> bool:
        return (
            response.status_code == 403
            and response.headers.get("ecom-csrf-failure", "").lower() == "true"
        )


def _cookie_to_json(cookie: Cookie) -> dict[str, Any]:
    return {
        "version": cookie.version,
        "name": cookie.name,
        "value": cookie.value,
        "port": cookie.port,
        "port_specified": cookie.port_specified,
        "domain": cookie.domain,
        "domain_specified": cookie.domain_specified,
        "domain_initial_dot": cookie.domain_initial_dot,
        "path": cookie.path,
        "path_specified": cookie.path_specified,
        "secure": cookie.secure,
        "expires": cookie.expires,
        "discard": cookie.discard,
        "comment": cookie.comment,
        "comment_url": cookie.comment_url,
        "rest": cookie._rest,
        "rfc2109": cookie.rfc2109,
    }


def _cookie_from_json(item: dict[str, Any]) -> Cookie:
    return Cookie(
        version=item.get("version", 0),
        name=item["name"],
        value=item["value"],
        port=item.get("port"),
        port_specified=item.get("port_specified", False),
        domain=item.get("domain") or ".ocado.com",
        domain_specified=item.get("domain_specified", True),
        domain_initial_dot=item.get("domain_initial_dot", True),
        path=item.get("path") or "/",
        path_specified=item.get("path_specified", True),
        secure=item.get("secure", True),
        expires=item.get("expires"),
        discard=item.get("discard", False),
        comment=item.get("comment"),
        comment_url=item.get("comment_url"),
        rest=item.get("rest") or {},
        rfc2109=item.get("rfc2109", False),
    )


def clear_cookie_jar(jar: CookieJar) -> None:
    jar.clear()

