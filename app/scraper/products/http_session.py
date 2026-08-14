"""Fetch retailer JSON over plain HTTP, presenting a browser's TLS fingerprint.

The sibling of :mod:`app.scraper.products.browser`, and the cheaper half of the
pair. Where that module drives Chromium and issues the API call from inside the
page, this one makes the request directly — and the only reason it can is that
it hands the server the same TLS ClientHello and HTTP/2 settings a browser
would.

That distinction is the whole story of why the browser was there. Akamai's edge
was never checking for a session, a token or a cookie on these endpoints: a
request with no cookies at all is answered, and the same URL is refused with an
"Access Denied" interstitial the moment it arrives over Python's TLS stack.
What it fingerprints is the *client*, and ``httpx`` is unmistakably not a
browser at the handshake. :mod:`curl_cffi` is libcurl built against the browser
handshakes, so it gets the answer a browser gets.

Nothing here forges a credential, defeats a challenge, or reaches anything a
logged-out shopper could not load in a tab — it is the same anonymous catalogue,
asked for over a connection the shop is willing to speak on. What it buys is
proportionality: a search that cost a Chrome launch, a warm-up navigation and
several seconds now costs one request in about half a second, and a host with no
display can run it.

The retailer-facing shape is deliberately identical to
:class:`~app.scraper.products.browser.BrowserJsonClient` — a context manager
exposing :meth:`json_fetch`, with the same throttle contract — so the pipeline
and the live-search runner are written once and each adapter picks the transport
its shop requires.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.scraper.ratelimit import AdaptiveThrottle

log = logging.getLogger(__name__)

#: Ceiling on a single API call. Much tighter than the browser client's, and it
#: can be: there is no page load, no WAF interstitial and no Chrome start-up
#: inside this number — just the request.
REQUEST_TIMEOUT_S = 30.0

#: Which browser handshake to present. A generic ``chrome`` tracks whatever
#: curl_cffi considers current rather than pinning a version that ages into
#: being conspicuous. Every profile tried was accepted (Chrome, Firefox and
#: Safari alike), which says the edge is rejecting *non-browsers* rather than
#: admitting one specific build.
IMPERSONATE = "chrome"


class JsonFetchError(RuntimeError):
    """A request the retailer refused, with enough of the answer to act on it.

    A ``RuntimeError`` still, because that is what the pipeline and the runner
    already catch and record against the row being fetched. What it adds is the
    status and headers, so a caller that can *fix* a particular refusal —
    Ocado's rotating CSRF token being the case in hand — can recognise its own
    and retry, instead of every failure looking alike from the outside.
    """

    def __init__(self, message: str, *, status: int | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}


class HttpJsonClient:
    """An HTTP session for a retailer's JSON API, fingerprinted as a browser.

    Subclasses set :attr:`referer` and add whatever request methods that
    retailer needs on top of :meth:`json_fetch`.
    """

    #: Sent with every request, and set to the SPA page the call would really
    #: have come from. Not required for an answer today — the endpoints are
    #: served without it — but it is true, it costs nothing, and a request that
    #: describes itself honestly is the one worth making.
    referer: str = ""

    #: Overridable per retailer, should one ever want a different handshake.
    impersonate: str = IMPERSONATE

    def __init__(self) -> None:
        self._session = None

    def __enter__(self) -> "HttpJsonClient":
        try:
            from curl_cffi import requests
        except ImportError as exc:  # pragma: no cover - exercised only without the dep
            raise RuntimeError(
                "curl_cffi is required for direct retailer JSON fetching"
            ) from exc

        self._session = requests.Session(impersonate=self.impersonate)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

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
        """One API call, with adaptive backoff.

        Mirrors :meth:`browser.BrowserJsonClient.json_fetch` exactly, including
        the rule that a non-200 *or* a non-JSON content type both count as being
        throttled: the WAF answers a caller it has lost patience with by serving
        an HTML page, sometimes with a 200 on it, so status alone would not
        notice being slowed down.
        """
        if self._session is None:
            raise RuntimeError(f"{type(self).__name__} must be used as a context manager")

        throttle.pace()

        request_headers = {"Accept": "application/json; charset=utf-8"}
        if self.referer:
            request_headers["Referer"] = self.referer
        if body is not None:
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        request_headers.update(headers or {})

        try:
            response = self._session.request(
                method,
                url,
                headers=request_headers,
                data=json.dumps(body).encode("utf-8") if body is not None else None,
                timeout=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - a transport failure is a slow-down signal
            throttle.note_throttle()
            raise JsonFetchError(f"{url} could not be fetched: {exc}") from exc

        content_type = response.headers.get("content-type") or ""
        if response.status_code != 200 or "json" not in content_type.lower():
            throttle.note_throttle()
            raise JsonFetchError(
                f"{url} returned {response.status_code} {content_type or 'unknown content-type'}",
                status=response.status_code,
                headers=dict(response.headers),
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            throttle.note_throttle()
            raise JsonFetchError(f"{url} returned non-JSON content") from exc
        throttle.note_success()
        return payload

    def fetch_text(self, url: str, *, timeout_s: float = REQUEST_TIMEOUT_S) -> str:
        """A plain page fetch, for the things that are not API calls.

        Deliberately outside the throttle: this is not a request against the
        retailer's API budget but the one-off page read that makes such requests
        possible at all (Ocado's CSRF token). Counting it as a throttled call
        would let a token refresh look like API pressure and slow the scrape for
        a reason that has nothing to do with the shop's patience.
        """
        if self._session is None:
            raise RuntimeError(f"{type(self).__name__} must be used as a context manager")
        response = self._session.get(url, timeout=timeout_s)
        if response.status_code != 200:
            raise JsonFetchError(
                f"{url} returned {response.status_code}",
                status=response.status_code,
                headers=dict(response.headers),
            )
        return response.text
