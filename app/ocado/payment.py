"""Finish Ocado checkout in its web UI, including the provider's payment flow.

A payment form/session is a continuation, not evidence of a bank challenge.
Open provider forms directly, or replay an SDK-session response into Ocado's UI.
Continue its available controls and validate the result before verifying the order.
Only one checkout is initiated for an attempted change.
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
_ACTION = re.compile(r"^(?:pay(?: now)?|continue(?: to payment)?|confirm(?: changes| payment| order)?|"
                     r"place (?:your )?order|submit|complete (?:purchase|order))$", re.I)
_SECRET = re.compile(r"cvv|cvc|security.?code|one.?time|otp|verification.?code|pass(?:word|code)|card.?number", re.I)


class PaymentFlowError(RuntimeError):
    """A concrete input/action or a technical failure prevented continuation."""


def complete(client: Any, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("type") == DONE:
        return result
    if result.get("type") not in ("checkout-form-response", "payment-session-response"):
        raise PaymentFlowError(f"Ocado returned an unsupported checkout response ({result.get('type') or 'missing type'})")
    try:
        return _browser_result(client, result)
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


def _browser_result(client: Any, initial: dict[str, Any]) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            context = browser.new_context()
            context.add_cookies(_cookies(client))
            page = context.new_page()
            page.set_default_timeout(2000)
            completed: list[dict[str, Any]] = []
            failures: list[str] = []
            replayed = False

            def replay(route):
                nonlocal replayed
                if route.request.method != "POST":
                    route.continue_()
                elif not replayed:
                    replayed = True
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(initial))
                else:
                    failures.append("The payment page attempted to start checkout again; check the saved order before retrying")
                    route.abort()

            def response(resp):
                _completion_response(resp, completed, failures)

            page.route("https://www.ocado.com/api/orderpayment/v2/checkout", replay)
            page.on("response", response)
            context.on("page", lambda popup: popup.on("response", response))
            if initial.get("type") == "checkout-form-response":
                _open_form(page, client, initial, completed, failures)
            else:
                page.goto("https://www.ocado.com/checkout", wait_until="domcontentloaded", timeout=30000)
            return _drive(page, completed, failures)
        finally:
            browser.close()


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
        failures.append(f"{source} rejected payment completion (HTTP {resp.status}{detail})")


def _open_form(page: Any, client: Any, initial: dict, completed: list, failures: list) -> None:
    url = initial.get("url") or ""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "pspweb.ocado.com":
        raise PaymentFlowError("Ocado returned an unsupported payment form address")

    def message(source, event):
        if source["frame"] != page.main_frame or not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "CHECKOUT_RESULT_TOKEN" and isinstance(event.get("resultToken"), str) and event["resultToken"]:
            if completed or failures:
                return
            try:
                validated = client.validate_checkout(event["resultToken"])
            except Exception as exc:
                failures.append(f"Ocado could not validate the payment result ({type(exc).__name__}); check the saved order")
                return
            if validated.get("type") == DONE:
                completed.append(validated)
            else:
                failures.append("Ocado did not confirm the validated payment result; check the saved order")
        elif kind in ("CHECKOUT_ERROR", "CHECKOUT_CANCELLED"):
            reason = event.get("reason")
            codes = re.findall(r"pspweb-[0-9]+|\b[A-Z][A-Z0-9_]{2,64}\b", str(reason or ""))
            detail = f" ({', '.join(dict.fromkeys(codes[:5]))})" if codes else ""
            failures.append("The payment provider returned an error" + detail if kind == "CHECKOUT_ERROR"
                            else "The payment provider cancelled checkout")

    page.expose_binding("ocadoPaymentMessage", message)
    # Use Ocado's normal parent origin and iframe message protocol, without restarting checkout.
    parent = "https://www.ocado.com/checkout/3ds/end"
    page.route(parent, lambda route: route.fulfill(content_type="text/html", body="<html><body></body></html>"))
    page.goto(parent, wait_until="domcontentloaded", timeout=10000)
    page.evaluate("""url => {
        window.addEventListener('message', event => {
            if (event.origin !== 'https://pspweb.ocado.com') return;
            let value = event.data;
            if (typeof value === 'string') { try { value = JSON.parse(value); } catch { return; } }
            if (value && typeof value === 'object') window.ocadoPaymentMessage(value);
        });
        const frame = document.createElement('iframe');
        frame.src = url; frame.style.width = '100%'; frame.style.height = '800px';
        document.body.appendChild(frame);
    }""", url)


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


def _drive(page: Any, completed: list[dict], failures: list[str]) -> dict[str, Any]:
    deadline = time.monotonic() + FLOW_SECONDS
    clicked: set[tuple] = set()
    while time.monotonic() < deadline:
        if completed:
            return completed[0]
        if failures:
            raise PaymentFlowError(failures[0])
        try:
            _advance(page, clicked)
        except PaymentFlowError:
            raise
        except Exception:
            # Frames/elements are replaced while the payment SDK loads. Inspect the new page next tick.
            pass
        page.wait_for_timeout(500)
    raise PaymentFlowError("The payment page timed out after continuing its available controls; "
                           "check the saved order on ocado.com")
