"""HelloFresh account API: the endpoints behind hellofresh.co.uk's own account pages.

Unlike Ocado there is no browser in the loop. The site's gateway (``/gw``) takes a
plain username/password POST and answers with an OAuth-style token pair, so the
whole ladder is: a saved access token, else the refresh token, else ask the person
for their password again. The token pair is the session; it is saved per account
under the data dir, and the password is never written anywhere.

Paths, methods and bodies are read off the site's JavaScript (the Next.js chunks
for the account, deliveries and cancellation pages), not guessed. What that
leaves unconfirmed until a real login is the exact *response* shapes of the
customer endpoints, so the summaries in :mod:`app.hellofresh.boxes` read them
defensively and every endpoint is also exposed raw.

Weeks are ISO weeks, ``"2026-W42"`` (the site formats them ``GGGG-[W]WW``). A
delivery's ``status`` is ``RUNNING`` (coming), ``PAUSED`` (skipped), then
``PREPARING``/``SHIPPED``/``DELIVERED``. Skipping a week is a PATCH of that
delivery to ``PAUSED``; un-skipping is the same PATCH back to ``RUNNING``.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("holafresca.hellofresh")

BASE_URL = "https://www.hellofresh.co.uk/gw"
COUNTRY = "GB"
LOCALE = "en-GB"
#: The site's public client id, used only for the anonymous token (menus).
PUBLIC_CLIENT_ID = "senf"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/141.0 Safari/537.36")
#: Refresh this long before the access token says it expires.
EXPIRY_MARGIN_S = 300

LOGIN_PATH = "/login"
REFRESH_PATH = "/refresh"
LOGOUT_PATH = "/logout"
PUBLIC_TOKEN_PATH = "/auth/token"
CUSTOMER_PATH = "/api/customers/me"
SUBSCRIPTIONS_PATH = "/api/customers/me/subscriptions"
DELIVERIES_PATH = "/api/customers/me/deliveries"
ORDERS_PATH = "/api/customers/me/orders"
SUBSCRIPTION_PATH = "/api/subscriptions/{subscription_id}"
DELIVERY_PATH = "/api/subscriptions/{subscription_id}/delivery_dates/{week}"
PRODUCT_OPTIONS_PATH = "/api/subscriptions/{subscription_id}/product_options"
PLANS_PATH = "/api/plans"
PLAN_PATH = "/api/plans/{plan_id}"
CANCEL_PLAN_PATH = "/api/plans/{plan_id}/cancel"
CANCEL_REASON_PATH = "/api/plans/{plan_id}/cancellation/reason"
MENU_WEEKS_PATH = "/menus-service/weeks"
MENUS_PATH = "/menus-service/menus"
#: One week's menu as the deliveries page shows it, with the chosen meals marked.
WEEK_MENU_PATH = "/my-deliveries/menu"

RUNNING = "RUNNING"
PAUSED = "PAUSED"


class HelloFreshError(RuntimeError):
    pass


class NeedsLogin(HelloFreshError):
    """No usable token: the person has to sign in again (with their password)."""


@dataclass
class TokenStore:
    """The token pair for one account, on disk next to the other shops' sessions."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    def load(self) -> TokenStore:
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        return self

    def save(self, token: dict[str, Any]) -> None:
        expires_in = int(token.get("expires_in") or 0)
        self.data = {
            "access_token": token.get("access_token"),
            "refresh_token": token.get("refresh_token") or self.data.get("refresh_token"),
            "expires_at": time.time() + expires_in if expires_in else None,
            "user_data": token.get("user_data") or self.data.get("user_data"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        self.path.chmod(0o600)

    def forget(self) -> None:
        self.data = {}
        self.path.unlink(missing_ok=True)

    @property
    def access_token(self) -> str | None:
        return self.data.get("access_token")

    @property
    def refresh_token(self) -> str | None:
        return self.data.get("refresh_token")

    def fresh(self) -> bool:
        expires_at = self.data.get("expires_at")
        return bool(self.access_token) and (not expires_at or expires_at - EXPIRY_MARGIN_S > time.time())


class HelloFreshClient:
    """One account's HelloFresh session. Reads are safe to call freely; the
    methods that change the subscription (``skip_week``, ``cancel_plan``...) do
    exactly what the site's buttons do, so callers should confirm first."""

    def __init__(self, tokens: TokenStore, *, http: httpx.Client | None = None) -> None:
        self.tokens = tokens
        self.http = http or httpx.Client(base_url=BASE_URL, timeout=20,
                                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    # ---- session --------------------------------------------------------------
    def login(self, email: str, password: str) -> dict[str, Any]:
        response = self.http.post(LOGIN_PATH, params={"country": COUNTRY},
                                  json={"username": email, "password": password})
        if response.status_code in (400, 401, 403):
            raise HelloFreshError(f"HelloFresh refused the login ({response.status_code}): {_detail(response)}")
        response.raise_for_status()
        token = response.json()
        if not token.get("access_token"):
            raise HelloFreshError(f"HelloFresh login answered without a token: {str(token)[:200]}")
        self.tokens.save(token)
        return token

    def refresh(self) -> None:
        if not self.tokens.refresh_token:
            raise NeedsLogin("No HelloFresh session: sign in again")
        response = self.http.post(REFRESH_PATH, params={"country": COUNTRY, "locale": LOCALE},
                                  json={"refresh_token": self.tokens.refresh_token})
        if response.status_code >= 400:
            raise NeedsLogin(f"HelloFresh session expired ({response.status_code}): sign in again")
        self.tokens.save(response.json())

    def logout(self) -> None:
        if self.tokens.refresh_token:
            try:
                self.http.post(LOGOUT_PATH, params={"country": COUNTRY, "locale": LOCALE},
                               json={"refresh_token": self.tokens.refresh_token})
            except httpx.HTTPError as exc:  # signing out locally is what matters
                log.info("hellofresh logout call failed: %s", exc)
        self.tokens.forget()

    def status(self) -> str:
        """ready | expired (a refresh will try) | logged_out. Costs no request."""
        if self.tokens.fresh():
            return "ready"
        return "expired" if self.tokens.refresh_token else "logged_out"

    def ensure_ready(self) -> None:
        if not self.tokens.fresh():
            self.refresh()

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                json_body: Any = None) -> Any:
        """One authenticated call, refreshing the token once if it's been rejected."""
        self.ensure_ready()
        query = {"country": COUNTRY, "locale": LOCALE, **(params or {})}
        for attempt in range(2):
            response = self.http.request(method, path, params=query, json=json_body,
                                         headers={"Authorization": f"Bearer {self.tokens.access_token}"})
            if response.status_code == 401 and not attempt:
                self.refresh()
                continue
            break
        if response.status_code == 401:
            raise NeedsLogin("HelloFresh rejected the session: sign in again")
        if response.status_code >= 400:
            raise HelloFreshError(f"HelloFresh {method} {path} failed ({response.status_code}): {_detail(response)}")
        return response.json() if response.content else {}

    # ---- reads ----------------------------------------------------------------
    def customer(self) -> Any:
        return self.request("GET", CUSTOMER_PATH)

    def subscriptions(self) -> Any:
        return self.request("GET", SUBSCRIPTIONS_PATH)

    def subscription(self, subscription_id: str) -> Any:
        return self.request("GET", SUBSCRIPTION_PATH.format(subscription_id=subscription_id))

    def deliveries(self, range_start: str, range_end: str) -> Any:
        """Every subscription's deliveries for the weeks ``range_start``..``range_end`` (ISO weeks)."""
        return self.request("GET", DELIVERIES_PATH, params={"rangeStart": range_start, "rangeEnd": range_end})

    def delivery(self, subscription_id: str, week: str) -> Any:
        return self.request("GET", DELIVERY_PATH.format(subscription_id=subscription_id, week=week))

    def week_menu(self, params: dict[str, str]) -> Any:
        """One week's menu, chosen meals marked; ``params`` from :func:`app.hellofresh.boxes.menu_params`."""
        return self.request("GET", WEEK_MENU_PATH, params=params)

    def orders(self, limit: int = 10) -> Any:
        return self.request("GET", ORDERS_PATH, params={"limit": limit})

    def product_options(self, subscription_id: str) -> Any:
        return self.request("GET", PRODUCT_OPTIONS_PATH.format(subscription_id=subscription_id))

    def plans(self) -> Any:
        return self.request("GET", PLANS_PATH)

    def plan(self, plan_id: str) -> Any:
        return self.request("GET", PLAN_PATH.format(plan_id=plan_id))

    def menu(self, week: str) -> Any:
        """The week's full menu (all recipes on offer, not only the ones chosen)."""
        return self.request("GET", MENUS_PATH, params={"weeks": week, "is-active": "true"})

    def menu_weeks(self) -> Any:
        return self.request("GET", MENU_WEEKS_PATH, params={"brand": "hellofresh"})

    # ---- changes (each one is a button on the site) ----------------------------
    def set_delivery_status(self, subscription_id: str, week: str, status: str, *,
                            cutoff_date: str | None, delivery_date: str | None) -> Any:
        """The site sends the delivery's own cutoff and date back with the new status."""
        body = {"delivery": {"cutoffDate": cutoff_date, "deliveryDate": delivery_date, "status": status,
                             "subscriptionId": subscription_id, "id": week}}
        return self.request("PATCH", DELIVERY_PATH.format(subscription_id=subscription_id, week=week), json_body=body)

    def skip_week(self, subscription_id: str, week: str) -> Any:
        delivery = unwrap(self.delivery(subscription_id, week))
        return self.set_delivery_status(subscription_id, week, PAUSED, cutoff_date=delivery.get("cutoffDate"),
                                        delivery_date=delivery.get("deliveryDate"))

    def unskip_week(self, subscription_id: str, week: str) -> Any:
        delivery = unwrap(self.delivery(subscription_id, week))
        return self.set_delivery_status(subscription_id, week, RUNNING, cutoff_date=delivery.get("cutoffDate"),
                                        delivery_date=delivery.get("deliveryDate"))

    def cancel_plan(self, plan_id: str, reason: str | None = None) -> Any:
        """Cancel the whole plan: every future delivery stops. The site posts the
        (optional) reason separately, alongside the cancel."""
        result = self.request("POST", CANCEL_PLAN_PATH.format(plan_id=plan_id), json_body={})
        if reason:
            try:
                self.request("POST", CANCEL_REASON_PATH.format(plan_id=plan_id), json_body={"reason": reason})
            except HelloFreshError as exc:  # the cancel stands either way
                log.warning("hellofresh cancellation reason not recorded: %s", exc)
        return result

    def change_plan_product(self, plan_id: str, product_handle: str) -> Any:
        """Switch the plan's box (meals per week x people), by a handle from ``product_options``."""
        return self.request("PATCH", PLAN_PATH.format(plan_id=plan_id), json_body={"productHandle": product_handle})


def public_token(http: httpx.Client | None = None) -> str:
    """An anonymous token, enough for menus. Lasts about a month."""
    http = http or httpx.Client(base_url=BASE_URL, timeout=20, headers={"User-Agent": USER_AGENT})
    response = http.post(PUBLIC_TOKEN_PATH, params={"grant_type": "client_credentials", "client_id": PUBLIC_CLIENT_ID})
    response.raise_for_status()
    return response.json()["access_token"]


def unwrap(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("delivery"), dict):
        return payload["delivery"]
    return payload if isinstance(payload, dict) else {}


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error_description") or body.get("error") or body)[:200]
    return str(body)[:200]
