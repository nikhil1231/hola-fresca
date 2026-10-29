"""Run Ocado's real checkout page, including its saved-card and provider flow.

The merchant page must initialize checkout and own the provider handoff: opening a
provider form in a synthetic parent can fail after card tokenization. Continue
available controls and observe the merchant's validated result. Initiate once.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
DONE = "checkout-result-response"
FLOW_SECONDS = 75
#: How long to keep going after the payment provider reports an error. Its SDK
#: retries and probes on its own, and the merchant's validated result is what
#: decides the outcome, so one provider 4xx is a symptom, not the verdict.
PROVIDER_GRACE_SECONDS = 20
_ACTION = re.compile(r"^(?:pay(?: now)?|continue(?: to payment)?|confirm(?: changes| payment| order)?|"
                     r"place (?:your )?order|submit|complete (?:purchase|order))$", re.I)
_SECRET = re.compile(r"cvv|cvc|security.?code|one.?time|otp|verification.?code|pass(?:word|code)|card.?number", re.I)


class PaymentFlowError(RuntimeError):
    """A concrete input/action or a technical failure prevented continuation."""


def checkout(client: Any, body: dict[str, Any], order_id: str) -> dict[str, Any]:
    """Start and finish checkout in the real merchant page and its provider iframe."""
    try:
        return _browser_checkout(client, body, str(order_id))
    except PaymentFlowError:
        raise
    except Exception as exc:
        # Browser exceptions often contain URLs with payment tokens: never include them in an inbox/log.
        raise PaymentFlowError(f"The payment browser failed ({type(exc).__name__}); check the order on ocado.com") from exc


def _cookies(client: Any) -> list[dict[str, Any]]:
    cookies = []
    for cookie in client.session.client.cookies.jar:
        domain = cookie.domain.lstrip(".")
        if domain != "ocado.com" and not domain.endswith(".ocado.com"):
            continue
        # Requests jars can lose the Secure flag; Chromium enforces these cookie prefixes.
        value = {"name": cookie.name, "value": cookie.value,
                 "secure": bool(cookie.secure or cookie.name.startswith(("__Secure-", "__Host-")))}
        if cookie.name.startswith("__Host-"):
            value["url"] = f"https://{domain}/"
        else:
            value.update(domain=cookie.domain, path=cookie.path or "/")
        cookies.append(value)
    return cookies


def _checkout_request(route: Any, client: Any, body: dict, order_id: str,
                      submitted: list, failures: list) -> None:
    if route.request.method != "POST":
        route.fallback()
        return
    if submitted:
        failures.append((False, time.monotonic(),
                         "The payment page attempted to start checkout again; check the saved order before retrying"))
        route.abort()
        return
    # Verify the active cart immediately before submission: never place a new/different order.
    summary = client.checkout_summary()
    if str(summary.get("orderId")) != order_id:
        failures.append((False, time.monotonic(), "The active checkout is no longer the requested order; checkout was stopped"))
        route.abort()
        return
    native = route.request.post_data_json
    native["paymentMethod"] = body["paymentMethod"]
    if body.get("billingAddressId"):
        native["billingAddressId"] = body["billingAddressId"]
    if body.get("recurringOrder") or native.get("recurringOrder"):
        native["recurringOrder"] = {"edit": {"replaceItems": False}}
    submitted.append(True)
    # Keep the real site's redirect URL, request headers and browser session.
    route.fallback(post_data=json.dumps(native))


def _browser_checkout(client: Any, body: dict, order_id: str) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            context = browser.new_context()
            context.add_cookies(_cookies(client))
            page = context.new_page()
            page.set_default_timeout(2000)
            return _native_checkout(page, client, body, order_id)
        finally:
            browser.close()


def _native_checkout(page: Any, client: Any, body: dict, order_id: str) -> dict[str, Any]:
    completed: list[dict[str, Any]] = []
    failures: list[str] = []
    submitted: list[bool] = []
    page.route("https://www.ocado.com/api/orderpayment/v2/checkout",
               lambda route: _checkout_request(route, client, body, order_id, submitted, failures))
    page.context.on("response", lambda response: _completion_response(response, completed, failures))
    page.goto("https://www.ocado.com/checkout/summary", wait_until="domcontentloaded", timeout=30000)
    return _drive(page, completed, failures)


def _completion_response(resp: Any, completed: list, failures: list) -> None:
    parts = urlsplit(resp.url)
    merchant = parts.hostname == "www.ocado.com" and parts.path in (
        "/api/orderpayment/v1/checkout/validate", "/api/orderpayment/v2/checkout")
    provider = bool(parts.hostname and parts.hostname.endswith(".ocado.com")
                    and parts.path.endswith("/checkout/complete"))
    if not merchant and not provider:
        return
    try:
        value = resp.json()
    except Exception:
        value = {}
    if not isinstance(value, dict):
        value = {}
    if merchant and resp.status < 400 and value.get("type") == DONE:
        completed.append(value)
    elif resp.status >= 400:
        code = value.get("code") or value.get("errorCode")
        detail = f"; code {code}" if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code) else ""
        source = "The payment provider" if provider else "Ocado"
        # Host and path only: queries can carry payment tokens.
        log.warning("ocado payment: %s answered HTTP %s%s", f"{parts.hostname}{parts.path}", resp.status, detail)
        message = f"{source} rejected payment completion (HTTP {resp.status}{detail})"
        failures.append((provider, time.monotonic(), message))


def _advance(page: Any, clicked: set[tuple]) -> bool:
    """Take an available ordinary checkout action, without entering unknown credentials."""
    frames = [frame for tab in page.context.pages if not tab.is_closed() for frame in tab.frames] \
        if hasattr(page, "context") else page.frames
    for frame in frames:
        # Select a saved card if the provider/merchant has not already selected one.
        for selector in ('[data-synthetics="wallet-item-type-card"]', '.braintree-payment-method'):
            rows = frame.locator(selector)
            if rows.count():
                row = rows.first
                key = (frame.url, "saved-card", selector)
                if row.is_visible() and key not in clicked:
                    row.click()
                    clicked.add(key)
                    return True
        radios = frame.get_by_role("radio")
        visible = [r for r in radios.all() if r.is_visible() and r.is_enabled()]
        if visible and not any(r.is_checked() for r in visible):
            key = (frame.url, "radio")
            if key not in clicked:
                visible[0].check()
                clicked.add(key)
                return True
        # Required terms/consent checkboxes are part of ordinary checkout, not a reason to stop.
        for box in frame.get_by_role("checkbox").all():
            label = " ".join(filter(None, [box.get_attribute("name"), box.get_attribute("aria-label")]))
            if box.is_visible() and box.is_enabled() and not box.is_checked() and re.search(r"terms|agree|consent", label, re.I):
                box.check()
                return True
        missing = []
        inputs = frame.locator("input:visible")
        for field in inputs.all():
            if field.is_enabled() and not field.input_value():
                name = " ".join(filter(None, [field.get_attribute("name"), field.get_attribute("id"),
                                              field.get_attribute("placeholder"), field.get_attribute("autocomplete")]))
                if _SECRET.search(name) and (field.get_attribute("required") is not None
                                             or field.get_attribute("aria-invalid") == "true"):
                    missing.append(name)
        for button in frame.get_by_role("button", name=_ACTION).all():
            if button.is_visible() and button.is_enabled():
                key = (frame.url, button.inner_text(), hash(frame.locator("body").inner_text()))
                if key not in clicked:
                    log.info("ocado payment: continuing %s", button.inner_text().strip())
                    button.click()
                    clicked.add(key)
                    return True
        if missing:
            raise PaymentFlowError("The payment form requires a card security code, verification code, or password that is not available")
    return False


def _drive(page: Any, completed: list[dict], failures: list[tuple[bool, float, str]]) -> dict[str, Any]:
    deadline = time.monotonic() + FLOW_SECONDS
    clicked: set[tuple] = set()
    while time.monotonic() < deadline:
        if completed:
            return completed[0]
        hard = next((message for provider, _at, message in failures if not provider), None)
        if hard:
            raise PaymentFlowError(hard)
        if failures and time.monotonic() - failures[0][1] > PROVIDER_GRACE_SECONDS:
            raise PaymentFlowError(failures[0][2])
        try:
            _advance(page, clicked)
        except PaymentFlowError:
            raise
        except Exception:
            # Frames/elements are replaced while the payment SDK loads. Inspect the new page next tick.
            pass
        page.wait_for_timeout(500)
    if failures:
        raise PaymentFlowError(failures[0][2])
    raise PaymentFlowError("The payment page timed out after continuing its available controls; "
                           "check the saved order on ocado.com")
