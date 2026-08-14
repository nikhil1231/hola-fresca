"""The browser-free JSON transport.

Both shops are fetched over plain HTTP now rather than by driving Chrome. These
pin the two things that made the browser client safe to run unattended for a few
hundred requests, because the new transport has to keep them: a request that is
refused must *slow the next one down*, and a WAF serving an HTML page must count
as a refusal even when it does so with a 200.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.scraper.products
from app.scraper.products import ocado, sainsburys
from app.scraper.products.http_session import HttpJsonClient, JsonFetchError
from app.scraper.products.registry import ADAPTER_IDS, get_adapter
from app.scraper.ratelimit import AdaptiveThrottle


class FakeResponse:
    def __init__(self, status_code=200, content_type="application/json", body=None,
                 headers=None):
        self.status_code = status_code
        self.headers = {"content-type": content_type, **(headers or {})}
        self._body = body if body is not None else {"products": []}

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)  # raises, as curl_cffi's would
        return self._body


class FakePage:
    """A plain HTML answer, as ``fetch_text`` reads it."""

    def __init__(self, text, status_code=200):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": "text/html"}


class FakeSession:
    """Stands in for a ``curl_cffi`` session and records what it was asked."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def request(self, method, url, *, headers, data, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "data": data, "timeout": timeout}
        )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, *, timeout=None):
        self.calls.append({"method": "GET", "url": url, "headers": {}, "data": None,
                           "timeout": timeout})
        return self._responses.pop(0)

    def close(self):
        self.closed = True


class _Client(HttpJsonClient):
    referer = "https://shop.example/spa"


def _client(*responses) -> tuple[_Client, FakeSession]:
    client = _Client()
    session = FakeSession(*responses)
    client._session = session
    return client, session


def _throttle() -> AdaptiveThrottle:
    return AdaptiveThrottle(workers=1, delay=0.0, max_delay=20.0)


def test_a_good_answer_is_returned_and_relaxes_the_throttle():
    client, _ = _client(FakeResponse(body={"products": [{"product_uid": "1"}]}))
    throttle = _throttle()
    throttle.delay = 4.0

    payload = client.json_fetch("GET", "https://shop.example/api", None, throttle)

    assert payload == {"products": [{"product_uid": "1"}]}
    assert throttle.delay < 4.0


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(status_code=403, content_type="text/html"),
        # The one status alone would miss: Akamai's interstitial is a 200.
        FakeResponse(status_code=200, content_type="text/html"),
        FakeResponse(status_code=200, content_type="application/json", body="not json"),
        RuntimeError("connection reset"),
    ],
    ids=["denied", "html-with-a-200", "malformed-json", "transport-failure"],
)
def test_every_kind_of_refusal_raises_and_backs_off(response):
    client, _ = _client(response)
    throttle = _throttle()

    with pytest.raises(RuntimeError):
        client.json_fetch("GET", "https://shop.example/api", None, throttle)

    assert throttle.delay > 0, "a refused request must slow the next one down"


def test_the_request_says_where_it_came_from():
    client, session = _client(FakeResponse())

    client.json_fetch("GET", "https://shop.example/api", None, _throttle())

    headers = session.calls[0]["headers"]
    assert headers["Referer"] == "https://shop.example/spa"
    assert "json" in headers["Accept"]
    assert session.calls[0]["data"] is None


def test_a_body_is_sent_as_json():
    client, session = _client(FakeResponse())

    client.json_fetch("PUT", "https://shop.example/api", ["a", "b"], _throttle())

    call = session.calls[0]
    assert call["method"] == "PUT"
    assert json.loads(call["data"]) == ["a", "b"]
    assert "json" in call["headers"]["Content-Type"]


def test_using_the_client_unopened_is_an_error_not_a_crash():
    # The runner and the pipeline both enter the client as a context manager;
    # anything that forgets should say so rather than fail on a None attribute.
    with pytest.raises(RuntimeError, match="context manager"):
        _Client().json_fetch("GET", "https://shop.example/api", None, _throttle())


def test_closing_releases_the_session():
    client, session = _client()
    client.__exit__(None, None, None)
    assert session.closed


def test_no_adapter_opens_a_browser_to_scrape():
    # The point of the exercise. Both shops are reachable over HTTP, so a
    # regression that reintroduces Playwright into the scrape fails here rather
    # than on whichever machine has no display.
    for retailer in ADAPTER_IDS:
        adapter = get_adapter(retailer)
        assert issubclass(adapter.Client, HttpJsonClient), retailer
        assert not hasattr(adapter, "BrowserClient"), retailer


def test_the_scraper_package_imports_no_playwright():
    # Playwright survives only for the Ocado *login* (app/ocado/auth.py), which
    # faces a reCAPTCHA. Nothing under the scraper may reach for it.
    root = Path(app.scraper.products.__file__).parent
    offenders = [
        path.name
        for path in sorted(root.glob("*.py"))
        if "playwright" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_sainsburys_asks_the_gol_endpoints():
    client = sainsburys.SainsburysClient()
    session = FakeSession(FakeResponse(), FakeResponse())
    client._session = session
    throttle = _throttle()

    client.search("chorizo", throttle)
    client.products(["7317686", "536"], throttle)

    search_url, products_url = (call["url"] for call in session.calls)
    assert "/groceries-api/gol-services/product/v1/product" in search_url
    assert "filter%5Bkeyword%5D=chorizo" in search_url
    assert "uids=7317686%2C536" in products_url


def test_ocado_sends_the_csrf_token_it_read_from_a_page():
    # The decorate endpoint is the only guarded one at Ocado, and what it wants
    # is the token any page carries — not a login.
    client = ocado.OcadoClient()
    session = FakeSession(
        FakePage('window.x = {"csrf":{"token":"tok-123"},"other":1}'),
        FakeResponse(body=[{"id": "a"}]),
    )
    client._session = session
    client._csrf = None

    client.products(["a", "b"], _throttle())

    page, put = session.calls
    assert page["method"] == "GET" and page["url"] == ocado.BASE_URL + "/"
    assert put["headers"]["x-csrf-token"] == "tok-123"


def test_ocado_rereads_a_rejected_csrf_token_and_retries_once():
    # A scrape runs for minutes off a token read at the start, so an expiry
    # mid-run is ordinary. Ocado names this refusal in a header.
    client = ocado.OcadoClient()
    session = FakeSession(
        FakeResponse(status_code=403, content_type="text/html",
                     headers={"ecom-csrf-failure": "true"}),
        FakePage('{"csrf":{"token":"fresh"}}'),
        FakeResponse(body=[{"id": "a"}]),
    )
    client._session = session
    client._csrf = "stale"

    result = client.products(["a"], _throttle())

    assert result == [{"id": "a"}]
    assert client._csrf == "fresh"


def test_ocado_does_not_retry_a_refusal_that_is_not_about_the_csrf():
    # Re-sending a batch Ocado disliked for its own reasons just asks twice.
    client = ocado.OcadoClient()
    session = FakeSession(FakeResponse(status_code=500, content_type="text/html"))
    client._session = session
    client._csrf = "tok"

    with pytest.raises(JsonFetchError):
        client.products(["a"], _throttle())
    assert len(session.calls) == 1


def test_ocado_says_so_when_no_token_is_on_the_page():
    client = ocado.OcadoClient()
    client._session = FakeSession(FakePage("<html>no token here</html>"))
    client._csrf = None

    with pytest.raises(RuntimeError, match="CSRF token"):
        client.products(["a"], _throttle())
