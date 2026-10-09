"""Browser fallback for a challenged HTTP login; account requests stay on httpx."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def browser_login(email: str, password: str) -> dict[str, Any]:
    # Imported only on escalation. Each call owns its browser on the API worker
    # thread; no browser objects or passwords survive the login.
    from playwright.sync_api import Error, sync_playwright

    from app.hellofresh.client import HelloFreshError

    tokens: list[dict[str, Any]] = []
    rejected: int | None = None

    def received(response):
        nonlocal rejected
        url = urlparse(response.url)
        if (url.hostname != "www.hellofresh.co.uk" or url.path != "/gw/login"
                or response.request.method != "POST"):
            return
        if response.status in (400, 401, 403):
            rejected = response.status
        if response.status == 200:
            try:
                token = response.json()
            except (ValueError, Error):
                return
            if isinstance(token, dict) and token.get("access_token"):
                tokens.append(token)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(locale="en-GB")
                page = context.new_page()
                page.set_default_timeout(15_000)
                page.on("response", received)
                page.goto("https://www.hellofresh.co.uk/login", wait_until="domcontentloaded")
                consent = page.locator("#onetrust-accept-btn-handler")
                if consent.is_visible():
                    consent.click()
                page.locator('input[name="username"], input[name="email"], input[type="email"]').first.fill(email)
                page.locator('input[name="password"]').fill(password)
                page.locator('button[type="submit"]').first.click()
                # Pump events until the site's own login produces a token. Never
                # persist a browser profile, trace, or page screenshot containing credentials.
                for _ in range(120):
                    if tokens or rejected:
                        break
                    page.wait_for_timeout(250)
            finally:
                browser.close()
    except Error as exc:
        # Playwright errors may embed DOM contents; keep secrets out of API errors.
        raise HelloFreshError(
            "HelloFresh browser login could not complete. Install Chromium with "
            "`.venv/bin/python -m playwright install chromium`, or configure a "
            "HelloFresh refresh token from a signed-in browser."
        ) from exc
    if not tokens and rejected == 403:
        raise HelloFreshError(
            "HelloFresh blocked the browser login (403). Sign in normally and "
            "configure the refresh token from the /gw/login response."
        )
    if not tokens:
        raise HelloFreshError(
            "HelloFresh browser login did not return a session. Check the credentials; "
            "if the site requires a challenge or verification, sign in on HelloFresh "
            "and configure its refresh token."
        )
    return tokens[-1]
