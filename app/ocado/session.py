"""httpx session wrapper for Ocado web requests."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

import httpx

from app import config
from app.ocado.auth import AuthLadder, AuthState

BASE_URL = "https://www.ocado.com"
CSRF_RE = re.compile(r'"csrf"\s*:\s*\{\s*"token"\s*:\s*"([^"]+)"')

#: The Ocado login session. ``aws-waf-token`` deliberately is *not* here - it is a
#: WAF challenge token that an entirely logged-out browser also carries, so
#: counting it would make a dead jar look authenticated.
AUTH_COOKIE_NAMES = {"global_sid", "ocado_session"}

#: Mirrors ``client.CHECKOUT_WALK_PATH``; duplicated to keep this module free of
#: an import cycle with the client.
AUTH_PROBE_PATH = "/api/cart/v1/carts/active/checkout-walk"


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
        self.jar_path = jar_path or (config.DATA_DIR / "ocado" / "session.json")
        self.auth = auth or AuthLadder()
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
        """Whether a login cookie is *present*. Says nothing about it working."""
        return any(cookie.name in AUTH_COOKIE_NAMES for cookie in self.client.cookies.jar)

    def probe_authenticated(self) -> bool:
        """Ask Ocado whether the jar still works.

        Presence of ``global_sid`` is not enough - it is a session cookie the
        server expires on its own schedule, so a stale one sits in the jar
        looking healthy. This costs one cheap request and gives a real answer.
        Deliberately bypasses ``request`` so a 401 here cannot recurse back into
        the auth ladder.
        """
        if not self.has_auth_cookies():
            return False
        try:
            response = self.client.get(AUTH_PROBE_PATH, headers={"accept": "application/json"})
        except httpx.HTTPError:
            return False
        return response.status_code != 401

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
        writes = method not in {"GET", "HEAD", "OPTIONS"}
        if writes:
            kwargs = self._with_csrf(kwargs, self.csrf())

        response = self.client.request(method, url, **kwargs)

        if self._is_csrf_failure(response):
            kwargs = self._with_csrf(kwargs, self.csrf(force=True))
            response = self.client.request(method, url, **kwargs)

        if response.status_code == 401:
            # The jar is provably dead, so tell the ladder not to re-check it.
            state = self.auth.ensure_authenticated(self, trust_existing=False)
            if state == AuthState.READY:
                # A fresh login means a fresh session, and the CSRF token is
                # scoped to the session - the cached one died with the old one.
                self._csrf_token = None
                if writes:
                    kwargs = self._with_csrf(kwargs, self.csrf())
                response = self.client.request(method, url, **kwargs)

        self.save()
        return response

    @staticmethod
    def _with_csrf(kwargs: dict[str, Any], token: str) -> dict[str, Any]:
        kwargs = dict(kwargs)
        headers = dict(kwargs.get("headers") or {})
        headers["x-csrf-token"] = token
        headers.setdefault("ecom-request-source", "web")
        kwargs["headers"] = headers
        return kwargs

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


@dataclass(slots=True)
class OcadoAccountRuntime:
    account: config.OcadoAccountConfig
    auth: AuthLadder
    session: OcadoSession


_RUNTIMES: dict[str, OcadoAccountRuntime] = {}


def _record_event(event: Any) -> None:
    """Forward a ladder event to whatever sink is installed, if any."""
    from app.ocado import events

    events.record(event)


def account_dir(account_id: str) -> Path:
    return config.DATA_DIR / "ocado" / "accounts" / account_id


def _account_config(account_id: str | None = None) -> config.OcadoAccountConfig:
    resolved = account_id or config.DEFAULT_OCADO_ACCOUNT_ID
    for account in config.OCADO_ACCOUNTS:
        if account.id == resolved:
            return account
    raise KeyError(resolved)


def get_account_runtime(account_id: str | None = None) -> OcadoAccountRuntime:
    account = _account_config(account_id)
    runtime = _RUNTIMES.get(account.id)
    if runtime is not None:
        return runtime
    root = account_dir(account.id)
    auth = AuthLadder(
        profile_dir=root / "browser-profile",
        email=account.email,
        password=account.password,
        otp_markers=account.otp_markers,
        account_id=account.id,
        # Imported here rather than at module scope: app.ocado.events imports the
        # models, and this module is reached from the scraper CLIs too.
        on_event=_record_event,
    )
    session = OcadoSession(jar_path=root / "session.json", auth=auth)
    runtime = OcadoAccountRuntime(account=account, auth=auth, session=session)
    _RUNTIMES[account.id] = runtime
    return runtime


def list_account_runtimes() -> list[OcadoAccountRuntime]:
    return [get_account_runtime(account.id) for account in config.OCADO_ACCOUNTS]


def get_shared_session(account_id: str | None = None) -> OcadoSession:
    """The process-wide session for one Ocado account.

    One session per process per account, not per request: each cookie jar and
    CSRF token is shared state, and the login flow parks a browser against a
    specific session across two separate HTTP requests - so a per-request
    session would be closed out from under the OTP step.
    """
    return get_account_runtime(account_id).session
