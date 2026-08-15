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
from app.ocado import otp_mail

log = logging.getLogger("holafresca.ocado")

if TYPE_CHECKING:  # pragma: no cover
    from app.ocado.session import OcadoSession

BASE_URL = "https://www.ocado.com"

#: The shortest gap between two browser-backed silent refreshes for one account.
#: Rung 2 launches a real Chromium and walks Ocado's SSO, so a dead session plus
#: a caller that retries on failure is a browser-launch loop pointed at Ocado.
#: Rung 1 is untouched by this: it is one cheap HTTP request and may run freely.
SILENT_MIN_INTERVAL_S = 3600.0

#: How long to wait for the password step to resolve into either a logged-in
#: session or an OTP prompt. Generous, because reCAPTCHA can add several seconds.
LOGIN_TIMEOUT_S = 90.0
#: How long a browser job may take before the caller gives up on the worker.
JOB_TIMEOUT_S = 180.0
#: How long to keep re-harvesting cookies after Ocado accepts the login. The SSO
#: redirect chain outlives the URL change that ends the login step, and the
#: session cookie the site actually runs on is issued at the end of it.
SESSION_SETTLE_TIMEOUT_S = 30.0
SESSION_SETTLE_POLL_S = 2.0
NETWORK_IDLE_TIMEOUT_MS = 10_000

EMAIL_SELECTORS = (
    "input[data-id=login-input]",
    "input[data-synthetics=username-input]",
    "input[type=email]",
    "input[name=usernamelogin]",
    "input[name*=email i]",
)
PASSWORD_SELECTORS = (
    "input[data-id=login-password-input]",
    "input[data-synthetics=password-input]",
    "input[type=password]",
    "input[name=passwordlogin]",
)
# A fresh profile has to load the Salesforce shell, reCAPTCHA and the login web
# component before the fields exist. Three seconds is routinely too short on a
# cold start, so field discovery gets one bounded wait of its own.
LOGIN_FIELD_TIMEOUT_S = 30.0
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
    # Ocado's actual login error ("We're having trouble confirming your login
    # details") renders as a bare <span> inside div.infoosp, in shadow DOM, with
    # no class and no role - so none of the selectors below it ever matched.
    # _error_text was therefore never firing, and a wrong password took the full
    # LOGIN_TIMEOUT_S to come back as a bare "timeout" instead of failing fast
    # with the reason. Confirmed against the live page, August 2026.
    "div.infoosp",
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
    NEEDS_PASSWORD = "needs_password"
    AWAITING_OTP = "awaiting_otp"
    READY = "ready"


class AuthStage(StrEnum):
    """Which rung of the ladder is running right now.

    Separate from ``AuthState``, which is where the login *ended up*. A full
    login can take three minutes - a browser launch, reCAPTCHA, then a wait for
    the emailed code - and for most of that the state is simply LOGGED_OUT. The
    stage is what a caller polling /status can show instead of a bare spinner.
    """

    IDLE = "idle"
    CHECKING_SESSION = "checking_session"
    SIGNING_IN = "signing_in"
    WAITING_FOR_CODE = "waiting_for_code"
    ENTERING_CODE = "entering_code"


@dataclass(frozen=True, slots=True)
class LadderEvent:
    """One rung of the ladder, as it happened.

    A plain value rather than a database row, because this module has no session
    and should not grow one: the browser worker, the OTP flow and the retry
    handler are all hard enough to test without a database in the fixture. The
    sink that persists these lives in :mod:`app.ocado.session`, which already
    knows about accounts.
    """

    account_id: str
    rung: str  # probe | silent | login | otp
    outcome: str  # ok | failed | skipped | awaiting_otp | error
    trigger: str  # heartbeat | request | retry
    detail: str | None = None
    duration_ms: int | None = None


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
    #: Where to read the emailed code from. ``None`` means ask a human for it.
    otp_mailbox: otp_mail.MailboxConfig | None = None
    #: What distinguishes this account's mail in a mailbox shared by both.
    otp_markers: tuple[str, ...] = ()
    #: Which account these events belong to. Only ever used for attribution.
    account_id: str = "default"
    #: Where rung outcomes go. ``None`` keeps this module inert, which is what
    #: every existing test wants.
    on_event: Callable[[LadderEvent], None] | None = field(default=None, repr=False)
    state: AuthState = AuthState.LOGGED_OUT
    #: Read from other request threads while a login runs; only ever a plain
    #: assignment, so it needs no lock.
    stage: AuthStage = AuthStage.IDLE
    _worker: _BrowserWorker | None = field(default=None, repr=False)
    _pending_session: "OcadoSession | None" = field(default=None, repr=False)
    #: Monotonic stamp of the last rung-2 attempt, for SILENT_MIN_INTERVAL_S.
    _last_silent_at: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.profile_dir = self.profile_dir or (config.DATA_DIR / "ocado" / "browser-profile")
        if self.headless is None:
            self.headless = config.OCADO_LOGIN_HEADLESS
        if self.otp_mailbox is None:
            self.otp_mailbox = otp_mail.mailbox_from_env()

    @property
    def worker(self) -> _BrowserWorker:
        if self._worker is None:
            self._worker = _BrowserWorker(self.profile_dir, bool(self.headless))
        return self._worker

    def _record(
        self,
        rung: str,
        outcome: str,
        *,
        trigger: str,
        detail: str | None = None,
        started_at: float | None = None,
    ) -> None:
        """Hand one rung outcome to the sink, if there is one.

        Swallows sink failures on purpose. This is bookkeeping about a login; it
        must never be the reason a login fails.
        """
        if self.on_event is None:
            return
        duration_ms = (
            int((time.monotonic() - started_at) * 1000) if started_at is not None else None
        )
        try:
            self.on_event(
                LadderEvent(
                    account_id=self.account_id,
                    rung=rung,
                    outcome=outcome,
                    trigger=trigger,
                    detail=detail,
                    duration_ms=duration_ms,
                )
            )
        except Exception:  # noqa: BLE001 - never fail a login over telemetry
            log.warning("ocado auth: could not record a ladder event", exc_info=True)

    def ensure_authenticated(
        self,
        session: "OcadoSession",
        *,
        trust_existing: bool = True,
        email: str | None = None,
        password: str | None = None,
        trigger: str = "request",
    ) -> AuthState:
        """Try the cheapest thing that could work, then escalate.

        ``trust_existing=False`` is what the 401 handler passes: the caller
        already has proof the jar is dead, so re-checking it would only burn a
        request confirming that and then wrongly report READY.

        The first two rungs need nothing from the user. Full login is reachable
        only when this call carries both credentials, so automatic callers cannot
        accidentally send an OTP merely by checking or retrying a session.

        ``trigger`` is recorded against every rung this call reaches. It is what
        separates the heartbeat's steady drum from somebody actually shopping,
        without which neither series means anything.
        """
        self.stage = AuthStage.CHECKING_SESSION
        try:
            if trust_existing:
                started = time.monotonic()
                if session.probe_authenticated():
                    log.info("ocado auth: existing jar still works")
                    self._record("probe", "ok", trigger=trigger, started_at=started)
                    self.state = AuthState.READY
                    return self.state
                self._record("probe", "failed", trigger=trigger, started_at=started)
            if self.try_silent(session, trigger=trigger):
                log.info("ocado auth: silent refresh succeeded")
                self.state = AuthState.READY
                return self.state
            if not email or not password:
                log.info("ocado auth: quiet refresh exhausted, a password is needed")
                self.state = AuthState.NEEDS_PASSWORD
                return self.state
            log.info("ocado auth: silent refresh failed, falling back to full login")
            return self.start_login(
                session, email=email, password=password, trigger=trigger
            )
        finally:
            self.stage = AuthStage.IDLE

    def try_silent(self, session: "OcadoSession", *, trigger: str = "request") -> bool:
        """Re-auth with no password, if the upstream SSO session is still alive.

        Rate-limited by ``SILENT_MIN_INTERVAL_S``. The cap is deliberately here
        rather than in the heartbeat: the 401 retry path in
        :meth:`OcadoSession.request` reaches this rung too, and a genuinely dead
        session would otherwise launch a browser on every request that failed.
        """

        def job(worker: _BrowserWorker) -> None:
            page = worker.start_browser()
            page.goto(f"{BASE_URL}/login?silent=true", wait_until="domcontentloaded")
            _export_cookies(worker, session)
            worker.stop_browser()

        now = time.monotonic()
        if self._last_silent_at is not None:
            waited = now - self._last_silent_at
            if waited < SILENT_MIN_INTERVAL_S:
                log.info(
                    "ocado auth: skipping silent refresh, last one was %.0fs ago (min %.0fs)",
                    waited,
                    SILENT_MIN_INTERVAL_S,
                )
                self._record(
                    "silent",
                    "skipped",
                    trigger=trigger,
                    detail=f"rate limited, {waited:.0f}s since last attempt",
                )
                return False
        self._last_silent_at = now

        started = time.monotonic()
        try:
            self.worker.submit(job)
        except Exception as exc:  # noqa: BLE001 - a failed refresh just means "escalate"
            self._record(
                "silent", "error", trigger=trigger, detail=str(exc), started_at=started
            )
            return False
        ok = session.probe_authenticated()
        self._record(
            "silent", "ok" if ok else "failed", trigger=trigger, started_at=started
        )
        return ok

    def start_login(
        self,
        session: "OcadoSession",
        *,
        email: str,
        password: str,
        trigger: str = "request",
    ) -> AuthState:
        """Run the credential rung without retaining credentials on the ladder.

        Python cannot promise in-place secret zeroing, but these values are only
        request-local arguments captured for the browser job; they are never
        copied into the process-wide account runtime or persisted configuration.
        """
        if not email or not password:
            self._record(
                "login", "skipped", trigger=trigger, detail="credentials not supplied"
            )
            self.state = AuthState.NEEDS_PASSWORD
            return self.state
        self.stage = AuthStage.SIGNING_IN

        def job(worker: _BrowserWorker) -> tuple[str, str | None]:
            page = worker.start_browser()
            page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            _dismiss_cookie_banner(page)
            _fill_first(page, EMAIL_SELECTORS, email)
            _fill_first(page, PASSWORD_SELECTORS, password)
            _dismiss_cookie_banner(page)
            _click_first(page, SUBMIT_SELECTORS)
            return _await_login_outcome(page)

        # Stamped before the password step, because that is what makes Ocado send
        # the mail. Anything already in the mailbox is from an earlier attempt and
        # is either expired or about to be.
        started_at = time.time()
        started = time.monotonic()
        try:
            outcome, detail = self.worker.submit(job)
        except Exception as exc:
            # Never leave a browser holding the profile lock: the next launch
            # against the same profile dir would fail with a cryptic error.
            self._safe_stop()
            self._record(
                "login", "error", trigger=trigger, detail=str(exc), started_at=started
            )
            self.state = AuthState.LOGGED_OUT
            raise

        log.info("ocado auth: password step outcome=%s detail=%s", outcome, detail)

        if outcome == "otp":
            self._record(
                "login", "awaiting_otp", trigger=trigger, started_at=started
            )
            self._pending_session = session
            self.state = AuthState.AWAITING_OTP
            self._auto_otp(since=started_at, trigger=trigger)
            # Not the value _auto_otp returned: a browser failure inside
            # submit_otp resets the state, and that is what the caller needs.
            return self.state

        if outcome in {"error", "timeout"}:
            self._safe_stop()
            self._record(
                "login",
                "failed",
                trigger=trigger,
                detail=detail or "timed out waiting for Ocado",
                started_at=started,
            )
            self.state = AuthState.LOGGED_OUT
            if outcome == "timeout":
                raise RuntimeError(
                    "Ocado did not finish signing in within 90 seconds; please try again"
                )
            raise RuntimeError(f"Ocado rejected the login: {detail}")

        self._finish(session)
        self._record(
            "login",
            "ok" if self.state == AuthState.READY else "failed",
            trigger=trigger,
            detail=None if self.state == AuthState.READY else "session never settled",
            started_at=started,
        )
        return self.state

    def _auto_otp(self, *, since: float, trigger: str = "request") -> bool:
        """Read the emailed code and submit it, so re-auth needs nobody present.

        Every failure here is soft, and deliberately so: the login is parked at
        AWAITING_OTP with the browser still open, so an unconfigured mailbox, a
        code that never arrives and a code Ocado rejects all leave the manual
        ``/ocado/otp`` endpoint able to finish the job by hand.
        """
        if self.otp_mailbox is None:
            log.info("ocado auth: no OTP mailbox configured, waiting for a code by hand")
            self._record(
                "otp", "skipped", trigger=trigger, detail="no mailbox configured"
            )
            return False
        self.stage = AuthStage.WAITING_FOR_CODE
        try:
            code = otp_mail.fetch_code(
                self.otp_mailbox,
                otp_mail.OtpQuery(markers=self.otp_markers),
                since=since,
                wait_s=config.OCADO_OTP_WAIT_S,
                poll_s=config.OCADO_OTP_POLL_S,
            )
        except Exception as exc:  # noqa: BLE001 - a broken mailbox must not fail the login
            log.warning("ocado auth: could not read the OTP mailbox", exc_info=True)
            self._record("otp", "error", trigger=trigger, detail=f"mailbox: {exc}")
            return False
        if code is None:
            log.info(
                "ocado auth: no OTP mail matched %s within %.0fs, waiting for a code by hand",
                self.otp_markers or "(any sender)",
                config.OCADO_OTP_WAIT_S,
            )
            self._record("otp", "failed", trigger=trigger, detail="no code arrived")
            return False
        log.info("ocado auth: read a %d-digit code from the mailbox", len(code))
        try:
            self.submit_otp(code, trigger=trigger)
        except Exception:  # noqa: BLE001
            log.warning("ocado auth: the emailed code was not accepted", exc_info=True)
            return False
        return self.state == AuthState.READY

    def submit_otp(self, code: str, *, trigger: str = "request") -> AuthState:
        code = code.strip()
        if not code:
            raise ValueError("OTP code is required")
        session = self._pending_session
        log.info("ocado auth: otp submitted, pending_session=%s", session is not None)
        if session is None:
            self._record(
                "otp", "failed", trigger=trigger, detail="no login was waiting for a code"
            )
            self.state = AuthState.LOGGED_OUT
            return self.state
        self.stage = AuthStage.ENTERING_CODE
        started = time.monotonic()

        def job(worker: _BrowserWorker) -> tuple[str, str | None]:
            page = worker.page
            if page is None:
                raise RuntimeError("the Ocado login browser is no longer open")
            _fill_first(page, OTP_SELECTORS, code)
            _click_first(page, OTP_SUBMIT_SELECTORS)
            return _await_login_outcome(page, watch_for_otp=False)

        try:
            try:
                outcome, detail = self.worker.submit(job)
            except Exception as exc:
                self._safe_stop()
                self._pending_session = None
                self._record(
                    "otp", "error", trigger=trigger, detail=str(exc), started_at=started
                )
                self.state = AuthState.LOGGED_OUT
                raise

            log.info("ocado auth: otp step outcome=%s detail=%s", outcome, detail)

            if outcome in {"error", "timeout"}:
                self._record(
                    "otp",
                    "failed",
                    trigger=trigger,
                    detail=detail if outcome == "error" else "timed out",
                    started_at=started,
                )
                # Keep the browser open: a mistyped or slow code deserves another go.
                raise RuntimeError(
                    f"Ocado rejected the code: {detail}"
                    if outcome == "error"
                    else "timed out waiting for Ocado to accept the code"
                )

            self._pending_session = None
            self._finish(session)
            self._record(
                "otp",
                "ok" if self.state == AuthState.READY else "failed",
                trigger=trigger,
                detail=None if self.state == AuthState.READY else "session never settled",
                started_at=started,
            )
            return self.state
        finally:
            # /ocado/otp calls this directly, so it cannot rely on the reset in
            # ensure_authenticated to clear the stage behind it.
            self.stage = AuthStage.IDLE

    def forget(self, session: "OcadoSession") -> None:
        """Forget both halves of the persisted Ocado login.

        The httpx cookie jar is only half the session. The dedicated Chromium
        profile holds the upstream SSO cookies used by silent refresh, so logout
        must remove its cookie database too or the next quiet check signs
        straight back in. The rest of the profile is deliberately preserved:
        replacing Ocado's known browser with a brand-new identity makes its
        invisible reCAPTCHA much more likely to stall the next login.
        """
        self._safe_stop()
        self._pending_session = None
        self._last_silent_at = None
        self.state = AuthState.LOGGED_OUT
        self.stage = AuthStage.IDLE
        session.forget()
        if self.profile_dir is not None and self.profile_dir.exists():
            for name in ("Cookies", "Cookies-journal"):
                for path in self.profile_dir.rglob(name):
                    if path.is_file():
                        path.unlink()

    def _finish(self, session: "OcadoSession") -> None:
        """Harvest cookies until the jar works, close the browser, record it."""
        authenticated = self._harvest_until_authenticated(session)
        self._safe_stop()
        log.info(
            "ocado auth: finished, cookies=%d authenticated=%s",
            len(session.client.cookies.jar),
            authenticated,
        )
        self.state = AuthState.READY if authenticated else AuthState.LOGGED_OUT

    def _harvest_until_authenticated(self, session: "OcadoSession") -> bool:
        """Re-harvest the browser's cookies until the jar proves itself.

        Harvesting once is a race the login usually loses.
        ``_await_login_outcome`` reports "ready" the moment the URL leaves
        /login, but Ocado's SSO is still redirecting then: the jar at that
        instant holds ``global_sid`` and the in-flight ``sso.codeVerifier`` /
        ``nonce`` / ``state``, and *not* the ``ocado_session`` the site issues at
        the end of the chain. So a login Ocado had accepted came back with 25
        cookies and authenticated=False.

        Probing is the check rather than watching for a cookie by name, because
        the question being asked is exactly "does this jar work yet".
        """
        deadline = time.monotonic() + SESSION_SETTLE_TIMEOUT_S
        attempt = 0
        while True:
            attempt += 1
            revisit = attempt > 1
            self.worker.submit(lambda worker: _settle_and_export(worker, session, revisit))
            if session.probe_authenticated():
                log.info("ocado auth: session settled after %d harvest(s)", attempt)
                return True
            if time.monotonic() >= deadline:
                log.warning(
                    "ocado auth: Ocado accepted the login but the session never became"
                    " usable after %d harvests over %.0fs",
                    attempt,
                    SESSION_SETTLE_TIMEOUT_S,
                )
                return False
            time.sleep(SESSION_SETTLE_POLL_S)

    def _safe_stop(self) -> None:
        if self._worker is None:
            return
        try:
            self._worker.submit(lambda worker: worker.stop_browser())
        except Exception:  # noqa: BLE001 - shutdown must not mask the real error
            pass


def _export_cookies(worker: _BrowserWorker, session: "OcadoSession") -> None:
    if worker.context is None:
        return
    session.import_playwright_cookies(worker.context.cookies())


def _settle_and_export(worker: _BrowserWorker, session: "OcadoSession", revisit: bool) -> None:
    """Let the login finish landing, then copy the cookies across.

    ``revisit`` loads the homepage first. The tail of the SSO redirect chain does
    not always issue the site's own session cookie; one plain request as the
    newly signed-in user does.
    """
    page = worker.page
    if page is not None:
        try:
            if revisit:
                page.goto(BASE_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - a page that stays busy is still worth harvesting
            pass
    _export_cookies(worker, session)


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


def _wait_visible(
    page: Any,
    selectors: tuple[str, ...],
    *,
    timeout: float = LOGIN_FIELD_TIMEOUT_S,
) -> Any:
    """Wait for a late-mounted shadow-DOM control within one shared deadline."""
    deadline = time.monotonic() + timeout
    while True:
        locator = _visible(page, selectors)
        if locator is not None:
            return locator
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        page.wait_for_timeout(min(500, max(1, int(remaining * 1000))))


def _fill_first(page: Any, selectors: tuple[str, ...], value: str) -> None:
    locator = _wait_visible(page, selectors)
    if locator is None:
        raise RuntimeError(f"could not find an Ocado login field matching {selectors[0]}")
    locator.fill(value)


def _click_first(page: Any, selectors: tuple[str, ...]) -> None:
    locator = _visible(page, selectors)
    if locator is None:
        raise RuntimeError("could not find the Ocado login submit button")
    locator.click()
