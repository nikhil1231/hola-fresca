"""Complete Cloudflare's browser verification before submitting the login JSON.

The account UI is large and unnecessary for login. Serve the gateway's own
verification script on a small page, retain its cookies in Chromium, then send
exactly the same password request as HelloFresh's account page.
"""
from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
from contextlib import contextmanager
from typing import Any


def verification_document(html: str) -> str:
    scripts = [script for script in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
               if "/cdn-cgi/challenge-platform/" in script and "__CF$cv$params" in script]
    return ("<html><head></head><body></body>"
            + "".join(f"<script>{script}</script>" for script in scripts) + "</html>")


@contextmanager
def display_options():
    """Use a normal browser in Docker's Xvfb without changing process-wide DISPLAY."""
    from app.hellofresh.client import HelloFreshError

    environment = os.environ.copy()
    if environment.get("DISPLAY"):
        yield {"headless": False, "env": environment}
        return
    executable = shutil.which("Xvfb")
    if not executable:
        yield {"headless": True}
        return
    display = subprocess.Popen(
        [executable, "-displayfd", "1", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        if not select.select([display.stdout], [], [], 20)[0]:
            raise HelloFreshError("HelloFresh's verification display did not start")
        number = display.stdout.readline().strip()
        if not number.isdecimal():
            raise HelloFreshError("HelloFresh's verification display could not start")
        environment["DISPLAY"] = f":{number}"
        yield {"headless": False, "env": environment}
    finally:
        display.terminate()
        try:
            display.wait(timeout=5)
        except subprocess.TimeoutExpired:
            display.kill()
            display.wait(timeout=5)
        display.stdout.close()


def browser_login(email: str, password: str) -> dict[str, Any]:
    from playwright.sync_api import Error, sync_playwright
    from app.hellofresh.client import BASE_URL, COUNTRY, LOCALE, HelloFreshError

    origin = BASE_URL.removesuffix("/gw")
    try:
        with display_options() as options, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                **options, ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled", "--disable-gpu"],
            )
            try:
                context = browser.new_context(locale=LOCALE, timezone_id="Europe/London")
                page = context.new_page()
                page.set_default_timeout(20_000)

                def load_verification(route):
                    response = route.fetch(timeout=20_000)
                    route.fulfill(response=response, content_type="text/html",
                                  body=verification_document(response.text()))

                page.route(f"{origin}/login", load_verification)
                page.goto(f"{origin}/login", wait_until="domcontentloaded", timeout=30_000)
                for _ in range(40):
                    if any(cookie["name"] == "cf_clearance" for cookie in context.cookies()):
                        break
                    page.wait_for_timeout(500)
                # Credentials cross one same-origin JSON request. No browser
                # profile, trace, screenshot or cookies are persisted.
                result = page.evaluate("""async ([country, locale, email, password]) => {
                    const query = new URLSearchParams({country, locale});
                    const response = await fetch('/gw/login?' + query, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json',
                                  'Accept': 'application/json, text/plain, */*'},
                        body: JSON.stringify({username: email, password}),
                        signal: AbortSignal.timeout(20000)
                    });
                    return {status: response.status, token: await response.json().catch(() => null)};
                }""", [COUNTRY, LOCALE, email, password])
            finally:
                browser.close()
    except Error as exc:
        # Browser errors can include request contents; do not expose them.
        raise HelloFreshError(
            "HelloFresh browser verification could not complete. Install Chromium "
            "with `.venv/bin/python -m playwright install chromium` and Xvfb for "
            "server login (both are included in the production image)."
        ) from exc
    if result["status"] == 403:
        raise HelloFreshError(
            "HelloFresh blocked the browser login (403) after verification. "
            "Sign in normally and configure a refresh token."
        )
    token = result.get("token")
    if result["status"] != 200 or not isinstance(token, dict) or not token.get("access_token"):
        raise HelloFreshError(f"HelloFresh browser login returned no session ({result['status']})")
    return token
