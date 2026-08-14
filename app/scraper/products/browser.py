"""Fetch retailer JSON from inside a real browser session.

Both Ocado and Sainsbury's sit behind Akamai, and neither will answer an httpx
client: a bare request is met with an edge 403 before it reaches the origin, and
Sainsbury's additionally A/B-buckets the caller into one of two entirely
different front-ends depending on cookies it only sets for a real browser. Rather
than forge tokens, this drives Chromium with a persistent profile and issues the
API call with ``fetch`` from inside the page, so every cookie, header and WAF
challenge is whatever the site itself just negotiated.

The profile is persistent and per-retailer, which is what makes this affordable:
the expensive part is the first visit, and every later run starts already
trusted.

Playwright's sync API is thread-affine. Nothing here enforces that — callers that
need to serialise onto one thread do it themselves; see
:class:`app.mapping.live_search.LiveSearchRunner`.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from app.scraper.ratelimit import AdaptiveThrottle

log = logging.getLogger(__name__)

#: Ceiling on a single in-page API call. Generous — these are real page loads
#: behind a WAF — but finite, because the alternative is a scrape that stops
#: making progress and never says so.
REQUEST_TIMEOUT_S = 60.0

#: Ceiling on the warm-up navigation. Playwright defaults to 30s; stated here so
#: both halves of "getting a usable page" are bounded in one place.
WARMUP_TIMEOUT_S = 60.0


class BrowserJsonClient:
    """A Chromium context parked on a retailer's site, used as a JSON fetcher.

    Subclasses supply the profile directory and the page to warm up on, and add
    whatever request methods that retailer needs on top of :meth:`json_fetch`.
    """

    #: Where the browser is sent once, to pick up cookies and clear any WAF
    #: interstitial before the first API call.
    warmup_url: str = ""

    def __init__(self, *, profile_dir: Path, headless: bool = False):
        self.profile_dir = profile_dir
        self.headless = headless
        self._playwright = None
        self._context = None
        self._page = None

    def __enter__(self) -> "BrowserJsonClient":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - exercised only without optional dep
            raise RuntimeError(
                "playwright is required for browser-session product fetching"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            # Real Chrome first: it is challenged less often than bundled
            # Chromium, which matters for how long a profile stays trusted.
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless, channel="chrome"
            )
        except Exception:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless
            )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        if self.warmup_url:
            self._page.goto(
                self.warmup_url,
                wait_until="domcontentloaded",
                timeout=WARMUP_TIMEOUT_S * 1000,
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()

    def json_fetch(
        self,
        method: str,
        url: str,
        body: Any,
        throttle: AdaptiveThrottle,
        *,
        headers: dict[str, str] | None = None,
        timeout_s: float = REQUEST_TIMEOUT_S,
    ) -> dict[str, Any] | list[Any]:
        """One API call from inside the page, with adaptive backoff.

        A non-200 or a non-JSON content type both count as being throttled: the
        WAF answers a rate-limited caller with an HTML challenge page and a 200,
        so status alone would not notice being slowed down.

        The in-page request is bounded by ``timeout_s``. It has to be: Playwright
        puts no default timeout on ``evaluate``, so a ``fetch`` that never
        settles — which is how a WAF interstitial behaves in a headless browser —
        hangs the whole scrape silently and indefinitely. A timed-out request is
        recoverable; a stalled process is not, because nothing ever reports it.
        """
        if self._page is None:
            raise RuntimeError(f"{type(self).__name__} must be used as a context manager")

        if throttle.delay > 0:
            time.sleep(throttle.delay)

        result = self._page.evaluate(
            """async ({ method, url, body, headers, timeoutMs }) => {
                const options = {
                    method,
                    headers: { "Accept": "application/json; charset=utf-8", ...(headers || {}) },
                    signal: AbortSignal.timeout(timeoutMs),
                };
                if (body !== null) {
                    options.headers["Content-Type"] = "application/json; charset=utf-8";
                    options.body = JSON.stringify(body);
                }
                try {
                    const response = await fetch(url, options);
                    const text = await response.text();
                    return {
                        status: response.status,
                        contentType: response.headers.get("content-type") || "",
                        text
                    };
                } catch (err) {
                    // Reported rather than thrown: a rejection inside evaluate
                    // surfaces as an opaque Playwright error, and "the request
                    // timed out" is worth keeping legible in the scrape state.
                    return { aborted: true, reason: String(err && err.name || err) };
                }
            }""",
            {
                "method": method,
                "url": url,
                "body": body,
                "headers": headers or {},
                "timeoutMs": int(timeout_s * 1000),
            },
        )
        if result.get("aborted"):
            _on_throttle(throttle)
            raise RuntimeError(
                f"{url} did not respond within {timeout_s:g}s ({result.get('reason')})"
            )
        status = int(result["status"])
        content_type = result["contentType"]
        text = result["text"]
        if status != 200 or "json" not in content_type.lower():
            _on_throttle(throttle)
            raise RuntimeError(f"{url} returned {status} {content_type or 'unknown content-type'}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            _on_throttle(throttle)
            raise RuntimeError(f"{url} returned non-JSON content") from exc
        _on_success(throttle)
        return payload


#: Playwright's wording when the browser has gone away underneath us, rather
#: than when a request failed. Matched on the message because the exception type
#: varies with how it died — a crash, a closed window, a killed subprocess.
_DEAD_BROWSER_MARKERS = (
    "target page, context or browser has been closed",
    "target crashed",
    "browser has been closed",
    "browser closed",
    "connection closed",
    "playwright was closed",
)


def is_dead_browser(exc: BaseException) -> bool:
    """Whether this failure means the browser is gone, not that the call failed.

    The distinction decides who is to blame. A 404 for one product is that
    product's problem and belongs on its row; a closed browser is the *run's*
    problem, and recording it against whichever row happened to be next is how
    one crash turns into a hundred rows falsely marked bad.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _DEAD_BROWSER_MARKERS)


class BrowserSession:
    """A browser client that reopens itself when the browser dies under it.

    A product scrape is a long single-threaded walk — a few hundred searches over
    several minutes — and Chrome does not always survive it. It had been held
    open for the whole run with no recovery, so the first crash failed every
    remaining item in the worklist: 179 searches succeeded, the browser closed,
    and the last 71 were each marked as their own error without ever being tried.

    :class:`~app.mapping.live_search.LiveSearchRunner` had solved this already by
    dropping its client on failure. This is the same idea, made reusable and
    bounded: relaunch and retry the call once, and give up after
    ``max_restarts`` so a permanently broken browser cannot spawn Chrome once per
    remaining row.
    """

    def __init__(self, open_client, *, max_restarts: int = 3):
        self._open_client = open_client
        self._client = None
        self.max_restarts = max_restarts
        self.restarts = 0

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._close()

    @property
    def client(self):
        """The live client, launched on first use and after each restart."""
        if self._client is None:
            client = self._open_client()
            client.__enter__()
            self._client = client
        return self._client

    def call(self, fn):
        """Run ``fn(client)``, relaunching the browser once if it has died.

        Only a dead browser is retried. Anything else is the call's own failure
        and is raised for the caller to record against whatever it was doing.
        """
        try:
            return fn(self.client)
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is the browser
            if not is_dead_browser(exc):
                raise
            if self.restarts >= self.max_restarts:
                log.error(
                    "browser died again after %d restarts, giving up on this run",
                    self.restarts,
                )
                raise
            self.restarts += 1
            log.warning(
                "browser died (%s); restarting it (%d/%d) and retrying",
                exc,
                self.restarts,
                self.max_restarts,
            )
            self._close()
            return fn(self.client)

    def _close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - it is already broken; nothing to salvage
            pass
        self._client = None


def _on_success(throttle: AdaptiveThrottle) -> None:
    if throttle.delay > 0:
        throttle.delay = max(0.0, throttle.delay * throttle.recover_factor - 0.01)


def _on_throttle(throttle: AdaptiveThrottle) -> None:
    throttle.delay = min(
        throttle.max_delay, throttle.delay * throttle.backoff_factor + throttle.backoff_floor
    )
