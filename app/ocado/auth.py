"""Browser-only Ocado authentication ladder.

The hot path uses httpx. Playwright is kept here, only for refreshing cookies and
walking login/OTP when the persisted jar is no longer accepted, because Ocado's
SSO puts reCAPTCHA in front of the password step and no HTTP client can clear it.

Two things shape this module:

* Playwright's sync API is thread-affine, and the OTP flow spans two HTTP
  requests which FastAPI serves from a threadpool - so a request could easily
  land on a different thread than the one that opened the browser. Every browser
  touch is therefore funnelled onto one long-lived worker thread, the same shape
  as ``app.mapping.live_search``.
* Logging in is an XHR on a Salesforce-rendered page, not a navigation, so there
  is no load event to wait for. Progress is detected by polling for the outcome.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app import config

log = logging.getLogger("holafresca.ocado")

if TYPE_CHECKING:  # pragma: no cover
    from app.ocado.session import OcadoSession

BASE_URL = "https://www.ocado.com"

#: How long to wait for the password step to resolve into either a logged-in
#: session or an OTP prompt. Generous, because reCAPTCHA can add several seconds.
LOGIN_TIMEOUT_S = 90.0
#: How long a browser job may take before the caller gives up on the worker.
JOB_TIMEOUT_S = 180.0

EMAIL_SELECTORS = ("input[type=email]", "input[name=usernamelogin]", "input[name*=email i]")
PASSWORD_SELECTORS = ("input[type=password]", "input[name=passwordlogin]")
# Ocado's SSO is a Salesforce LWR app: the form lives in shadow DOM (Playwright
# locators pierce it, plain querySelectorAll does not) and the submit control is a
# styled <div>, not a <button> - so `button[type=submit]` matches nothing here.
SUBMIT_SELECTORS = (
    "div.login__submit",
    "[id^=login-submit-button]",
    "div.button:has-text('Log in')",
    "button[type=submit]",
)
# The consent banner sits over the form and swallows the click.
COOKIE_BANNER_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#accept-recommended-btn-handler",
)
ERROR_SELECTORS = (
    ".error_message",
    ".login__error",
    "[role=alert]",
    ".slds-form-element__help",
)
# Deliberately narrow: a blanket `input[type=text]` matches the site search box
# and would type the OTP into it.
OTP_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[name*=otp i]",
    "input[id*=otp i]",
    "input[name*=verification i]",
    "input[name*=code i]",
    "input[id*=code i]",
    "input[type=tel]",
)
OTP_SUBMIT_SELECTORS = (
    "div.button:has-text('Verify')",
    "div.button:has-text('Confirm')",
    "div.button:has-text('Continue')",
    "div.button:has-text('Submit')",
    "[id^=otp-submit]",
    "div.button",
    "button[type=submit]",
)


class AuthState(StrEnum):
    LOGGED_OUT = "logged_out"
    AWAITING_OTP = "awaiting_otp"
    READY = "ready"


@dataclass
class _Job:
    fn: Callable[["_BrowserWorker"], Any]
    future: Future


class _BrowserWorker:
    """Owns the Playwright objects, and runs every browser job on one thread."""

    def __init__(self, profile_dir: Path, headless: bool):
        self.profile_dir = profile_dir
        self.headless = headless
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    # -- called from any thread -------------------------------------------

    def submit(self, fn: Callable[["_BrowserWorker"], Any], timeout: float = JOB_TIMEOUT_S) -> Any:
        job = _Job(fn=fn, future=Future())
        with self._lock:
            self._ensure_thread()
            self._queue.put(job)
        return job.future.result(timeout=timeout)

    def close(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return
        try:
            self.submit(lambda worker: worker.stop_browser())
        finally:
            self._queue.put(None)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ocado-auth", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                job.future.set_result(job.fn(self))
            except BaseException as exc:  # noqa: BLE001 - travels to the caller
                job.future.set_exception(exc)

    # -- called only on the worker thread ---------------------------------

    @property
    def page(self) -> Any:
        return self._page

    @property
    def context(self) -> Any:
        return self._context

    def start_browser(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("playwright is required for Ocado login") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless, channel="chrome"
            )
        except Exception:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless
            )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def stop_browser(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # pragma: no cover - browser already gone
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # pragma: no cover
                pass
        self._playwright = None
        self._context = None
        self._page = None


@dataclass
class AuthLadder:
    """Refresh the Ocado cookie jar, escalating from free checks to full login."""

    profile_dir: Path | None = None
    headless: bool | None = None
    state: AuthState = AuthState.LOGGED_OUT
    _worker: _BrowserWorker | None = field(default=None, repr=False)
    _pending_session: "OcadoSession | None" = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.profile_dir = self.profile_dir or (config.DATA_DIR / "ocado" / "browser-profile")
        if self.headless is None:
            self.headless = config.OCADO_LOGIN_HEADLESS

    @property
    def worker(self) -> _BrowserWorker:
        if self._worker is None:
            self._worker = _BrowserWorker(self.profile_dir, bool(self.headless))
        return self._worker

    def ensure_authenticated(
        self, session: "OcadoSession", *, trust_existing: bool = True
    ) -> AuthState:
        """Try the cheapest thing that could work, then escalate.

        ``trust_existing=False`` is what the 401 handler passes: the caller
        already has proof the jar is dead, so re-checking it would only burn a
        request confirming that and then wrongly report READY.
        """
        if trust_existing and session.probe_authenticated():
            log.info("ocado auth: existing jar still works")
            self.state = AuthState.READY
            return self.state
        if self.try_silent(session):
            log.info("ocado auth: silent refresh succeeded")
            self.state = AuthState.READY
            return self.state
        log.info("ocado auth: silent refresh failed, falling back to full login")
        return self.start_login(session)

    def try_silent(self, session: "OcadoSession") -> bool:
        """Re-auth with no password, if the upstream SSO session is still alive."""

        def job(worker: _BrowserWorker) -> None:
            page = worker.start_browser()
            page.goto(f"{BASE_URL}/login?silent=true", wait_until="domcontentloaded")
            _export_cookies(worker, session)
            worker.stop_browser()

        try:
            self.worker.submit(job)
        except Exception:  # noqa: BLE001 - a failed refresh just means "escalate"
            return False
        return session.probe_authenticated()

    def start_login(self, session: "OcadoSession") -> AuthState:
        email = config.OCADO_EMAIL
        password = config.OCADO_PASSWORD
        if not email or not password:
            self.state = AuthState.LOGGED_OUT
            return self.state

        def job(worker: _BrowserWorker) -> tuple[str, str | None]:
            page = worker.start_browser()
            page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            _dismiss_cookie_banner(page)
            _fill_first(page, EMAIL_SELECTORS, email)
            _fill_first(page, PASSWORD_SELECTORS, password)
            _click_first(page, SUBMIT_SELECTORS)
            return _await_login_outcome(page)

        try:
            outcome, detail = self.worker.submit(job)
        except Exception:
            # Never leave a browser holding the profile lock: the next launch
            # against the same profile dir would fail with a cryptic error.
            self._safe_stop()
            self.state = AuthState.LOGGED_OUT
            raise

        log.info("ocado auth: password step outcome=%s detail=%s", outcome, detail)

        if outcome == "otp":
            self._pending_session = session
            self.state = AuthState.AWAITING_OTP
            return self.state

        if outcome == "error":
            self._safe_stop()
            self.state = AuthState.LOGGED_OUT
            raise RuntimeError(f"Ocado rejected the login: {detail}")

        self._finish(session)
        return self.state

    def submit_otp(self, code: str) -> AuthState:
        code = code.strip()
        if not code:
            raise ValueError("OTP code is required")
        session = self._pending_session
        log.info("ocado auth: otp submitted, pending_session=%s", session is not None)
        if session is None:
            self.state = AuthState.LOGGED_OUT
            return self.state

        def job(worker: _BrowserWorker) -> tuple[str, str | None]:
            page = worker.page
            if page is None:
                raise RuntimeError("the Ocado login browser is no longer open")
            _fill_first(page, OTP_SELECTORS, code)
            _click_first(page, OTP_SUBMIT_SELECTORS)
            return _await_login_outcome(page, watch_for_otp=False)

        try:
            outcome, detail = self.worker.submit(job)
        except Exception:
            self._safe_stop()
            self._pending_session = None
            self.state = AuthState.LOGGED_OUT
            raise

        log.info("ocado auth: otp step outcome=%s detail=%s", outcome, detail)

        if outcome in {"error", "timeout"}:
            # Keep the browser open: a mistyped or slow code deserves another go.
            raise RuntimeError(
                f"Ocado rejected the code: {detail}"
                if outcome == "error"
                else "timed out waiting for Ocado to accept the code"
            )

        self._pending_session = None
        self._finish(session)
        return self.state

    def _finish(self, session: "OcadoSession") -> None:
        """Harvest cookies, close the browser, and record what we ended up with."""
        self.worker.submit(lambda worker: _export_cookies(worker, session))
        self._safe_stop()
        authenticated = session.probe_authenticated()
        log.info(
            "ocado auth: finished, cookies=%d authenticated=%s",
            len(session.client.cookies.jar),
            authenticated,
        )
        self.state = AuthState.READY if authenticated else AuthState.LOGGED_OUT

    def _safe_stop(self) -> None:
        try:
            self.worker.submit(lambda worker: worker.stop_browser())
        except Exception:  # noqa: BLE001 - shutdown must not mask the real error
            pass


def _export_cookies(worker: _BrowserWorker, session: "OcadoSession") -> None:
    if worker.context is None:
        return
    session.import_playwright_cookies(worker.context.cookies())


def _dismiss_cookie_banner(page: Any) -> None:
    locator = _visible(page, COOKIE_BANNER_SELECTORS)
    if locator is None:
        return
    try:
        locator.click(timeout=5000)
        page.wait_for_timeout(1500)
    except Exception:  # noqa: BLE001 - the banner is best-effort, not required
        pass


def _await_login_outcome(
    page: Any, timeout: float = LOGIN_TIMEOUT_S, *, watch_for_otp: bool = True
) -> tuple[str, str | None]:
    """Poll until a login step resolves.

    Returns one of ``ready`` / ``otp`` / ``error`` / ``timeout``, with the error
    text where there is one. There is no navigation to wait on - the SSO page
    signs in over XHR - so the signals are the OTP prompt appearing, an error
    rendering, or the browser landing back on the Ocado site.

    ``watch_for_otp`` must be off once the OTP has been submitted: the prompt is
    still on screen at that moment, so treating it as a signal matches instantly
    and reports the step "finished" before the code has even been checked.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = page.url or ""
        if url.startswith(BASE_URL) and "/login" not in url:
            return "ready", None
        if watch_for_otp and _visible(page, OTP_SELECTORS):
            return "otp", None
        message = _error_text(page)
        if message:
            return "error", message
        page.wait_for_timeout(500)
    return "timeout", None


def _error_text(page: Any) -> str | None:
    """Surface a rendered validation error, so a bad password fails fast."""
    locator = _visible(page, ERROR_SELECTORS)
    if locator is None:
        return None
    try:
        text = (locator.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None


def _visible(page: Any, selectors: tuple[str, ...]) -> Any:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                return locator
        except Exception:  # noqa: BLE001 - page mid-navigation
            continue
    return None


def _fill_first(page: Any, selectors: tuple[str, ...], value: str) -> None:
    locator = _visible(page, selectors)
    if locator is None:
        raise RuntimeError(f"could not find an Ocado login field matching {selectors[0]}")
    locator.fill(value)


def _click_first(page: Any, selectors: tuple[str, ...]) -> None:
    locator = _visible(page, selectors)
    if locator is None:
        raise RuntimeError("could not find the Ocado login submit button")
    locator.click()


AUTH = AuthLadder()
