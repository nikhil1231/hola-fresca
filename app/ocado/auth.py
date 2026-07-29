"""Browser-only Ocado authentication ladder.

The hot path uses httpx. Playwright is kept here, only for refreshing cookies and
walking login/OTP when the persisted jar is no longer accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app import config

if TYPE_CHECKING:  # pragma: no cover
    from app.ocado.session import OcadoSession

BASE_URL = "https://www.ocado.com"


class AuthState(StrEnum):
    LOGGED_OUT = "logged_out"
    AWAITING_OTP = "awaiting_otp"
    READY = "ready"


@dataclass
class AuthLadder:
    """Refresh the Ocado cookie jar, escalating from free checks to full login."""

    profile_dir: Path | None = None
    headless: bool = False
    state: AuthState = AuthState.LOGGED_OUT
    _playwright: Any = None
    _context: Any = None
    _page: Any = None
    _pending_session: "OcadoSession | None" = None

    def __post_init__(self) -> None:
        self.profile_dir = self.profile_dir or (config.DATA_DIR / "ocado" / "browser-profile")

    def ensure_authenticated(self, session: "OcadoSession") -> AuthState:
        """Try silent refresh, then full login if credentials are available."""
        if session.has_auth_cookies():
            self.state = AuthState.READY
            return self.state
        if self.try_silent(session):
            self.state = AuthState.READY
            return self.state
        return self.start_login(session)

    def try_silent(self, session: "OcadoSession") -> bool:
        with self._browser() as page:
            page.goto(f"{BASE_URL}/login?silent=true", wait_until="domcontentloaded")
            self._export_cookies(session)
        return session.has_auth_cookies()

    def start_login(self, session: "OcadoSession") -> AuthState:
        email = config.OCADO_EMAIL
        password = config.OCADO_PASSWORD
        if not email or not password:
            self.state = AuthState.LOGGED_OUT
            return self.state

        page = self._start_browser()
        page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
        self._fill_first(page, ["input[type=email]", "input[name=email]", "#email"], email)
        self._fill_first(
            page,
            ["input[type=password]", "input[name=password]", "#password"],
            password,
        )
        self._click_first(
            page,
            [
                "button[type=submit]",
                "button:has-text('Sign in')",
                "button:has-text('Log in')",
                "button:has-text('Continue')",
            ],
        )
        page.wait_for_load_state("domcontentloaded")
        self._export_cookies(session)
        if session.has_auth_cookies():
            self._stop_browser()
            self.state = AuthState.READY
            return self.state

        self._pending_session = session
        self.state = AuthState.AWAITING_OTP
        return self.state

    def submit_otp(self, code: str) -> AuthState:
        if self._page is None or self._pending_session is None:
            self.state = AuthState.LOGGED_OUT
            return self.state
        code = code.strip()
        if not code:
            raise ValueError("OTP code is required")
        self._fill_first(
            self._page,
            [
                "input[autocomplete='one-time-code']",
                "input[name*=otp i]",
                "input[name*=code i]",
                "input[type=tel]",
                "input[type=text]",
            ],
            code,
        )
        self._click_first(
            self._page,
            [
                "button[type=submit]",
                "button:has-text('Verify')",
                "button:has-text('Continue')",
                "button:has-text('Submit')",
            ],
        )
        self._page.wait_for_load_state("domcontentloaded")
        self._export_cookies(self._pending_session)
        self._stop_browser()
        self.state = AuthState.READY if self._pending_session.has_auth_cookies() else AuthState.LOGGED_OUT
        self._pending_session = None
        return self.state

    def _browser(self):
        ladder = self

        class BrowserContext:
            def __enter__(self):
                return ladder._start_browser()

            def __exit__(self, exc_type, exc, tb):
                ladder._stop_browser()

        return BrowserContext()

    def _start_browser(self):
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
                str(self.profile_dir),
                headless=self.headless,
                channel="chrome",
            )
        except Exception:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
            )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def _stop_browser(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = None
        self._context = None
        self._page = None

    def _export_cookies(self, session: "OcadoSession") -> None:
        if self._context is None:
            return
        session.import_playwright_cookies(self._context.cookies())

    @staticmethod
    def _fill_first(page, selectors: list[str], value: str) -> None:
        for selector in selectors:
            locator = page.locator(selector).first
            if locator.count():
                locator.fill(value)
                return
        raise RuntimeError("could not find Ocado login field")

    @staticmethod
    def _click_first(page, selectors: list[str]) -> None:
        for selector in selectors:
            locator = page.locator(selector).first
            if locator.count():
                locator.click()
                return
        raise RuntimeError("could not find Ocado login submit button")


AUTH = AuthLadder()

