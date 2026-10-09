"""HelloFresh: the client against a fake gateway, the box summary, and the API's guards.

Nothing reaches HelloFresh. The fake gateway answers the paths the site's own
JavaScript calls, so these pin the request shapes (methods, bodies, the refresh
on a rejected token) rather than HelloFresh's behaviour.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from app.api import hellofresh as api
from app.hellofresh import boxes
from app.hellofresh.client import HelloFreshClient, HelloFreshError, NeedsLogin, TokenStore


class Gateway:
    def __init__(self):
        self.calls = []
        self.access = "a1"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, dict(request.url.params), body))
        path = request.url.path
        if path == "/gw/login":
            if body["password"] != "right":
                return httpx.Response(401, json={"message": "Invalid credentials"})
            return httpx.Response(200, json={"access_token": self.access, "refresh_token": "r1", "expires_in": 3600})
        if path == "/gw/refresh":
            self.access = "a2"
            return httpx.Response(200, json={"access_token": "a2", "expires_in": 3600})
        if request.headers.get("Authorization") != f"Bearer {self.access}":
            return httpx.Response(401, json={"message": "Provided token is invalid"})
        if path == "/gw/api/subscriptions/7/delivery_dates/2030-W42" and request.method == "GET":
            return httpx.Response(200, json={"delivery": {"id": "2030-W42", "cutoffDate": "2030-10-15T23:59:00Z",
                                                          "deliveryDate": "2030-10-20", "status": "RUNNING"}})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def gw(tmp_path):
    gateway = Gateway()
    http = httpx.Client(base_url="https://www.hellofresh.co.uk/gw", transport=httpx.MockTransport(gateway))
    client = HelloFreshClient(TokenStore(tmp_path / "s.json"), http=http)
    return gateway, client


def test_login_saves_tokens_and_never_the_password(gw, tmp_path):
    gateway, client = gw
    client.login("me@x.com", "right")
    saved = (tmp_path / "s.json").read_text()
    assert "a1" in saved and "right" not in saved
    assert gateway.calls[0][2] == {"country": "GB", "locale": "en-GB"}
    assert client.status() == "ready"
    with pytest.raises(Exception, match="refused"):
        client.login("me@x.com", "wrong")


def test_a_rejected_token_is_refreshed_once(gw):
    gateway, client = gw
    client.login("me@x.com", "right")
    gateway.access = "a2"  # the server has moved on: a1 is now rejected
    client.tokens.data["access_token"] = "stale"
    assert client.subscriptions() == {"ok": True}
    assert [c[1] for c in gateway.calls][-3:] == ["/gw/api/customers/me/subscriptions", "/gw/refresh",
                                                  "/gw/api/customers/me/subscriptions"]


@pytest.mark.parametrize("status,headers,escalates", [
    (403, {"cf-mitigated": "challenge"}, True),
    (403, {"content-type": "text/html"}, True),
    (403, {}, False),
    (401, {}, False),
])
def test_only_a_challenged_login_uses_the_browser(tmp_path, monkeypatch, status, headers, escalates):
    from app.hellofresh import auth
    calls = []

    def browser(email, password):
        calls.append((email, password))
        return {"access_token": "browser-access", "refresh_token": "browser-refresh", "expires_in": 3600}

    monkeypatch.setattr(auth, "browser_login", browser)
    http = httpx.Client(base_url="https://www.hellofresh.co.uk/gw", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, headers=headers, json={"message": "refused"})))
    client = HelloFreshClient(TokenStore(tmp_path / "session.json"), http=http)
    if escalates:
        client.login("me@example.com", "secret-password")
        assert calls == [("me@example.com", "secret-password")]
        assert client.status() == "ready"
        assert "secret-password" not in client.tokens.path.read_text()
    else:
        with pytest.raises(HelloFreshError):
            client.login("me@example.com", "secret-password")
        assert calls == []


def test_refresh_without_an_access_token_preserves_existing_session(tmp_path):
    tokens = TokenStore(tmp_path / "session.json")
    tokens.save({"access_token": "old", "refresh_token": "refresh"})
    http = httpx.Client(base_url="https://www.hellofresh.co.uk/gw", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"message": "unexpected response"})))
    with pytest.raises(NeedsLogin, match="no access token"):
        HelloFreshClient(tokens, http=http).refresh()
    assert tokens.access_token == "old" and tokens.refresh_token == "refresh"


def test_revoked_tokens_restore_configured_session_once(gw):
    gateway, client = gw
    client.login("me@x.com", "right")
    gateway.access = "new"
    original = gateway.__call__

    def revoked(request):
        if request.url.path == "/gw/refresh":
            return httpx.Response(401, json={"message": "revoked"})
        return original(request)

    client.http = httpx.Client(base_url="https://www.hellofresh.co.uk/gw", transport=httpx.MockTransport(revoked))
    restores = []

    def restore():
        restores.append(True)
        # The API restores with a separate client sharing the account token file.
        HelloFreshClient(TokenStore(client.tokens.path), http=client.http).login("me@x.com", "right")
        return True

    client.restore_session = restore
    assert client.subscriptions() == {"ok": True}
    assert restores == [True]


def test_a_network_error_never_replays_a_subscription_write(gw):
    gateway, client = gw
    client.login("me@x.com", "right")
    calls = []

    def unavailable(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("unknown result")

    client.http = httpx.Client(base_url="https://www.hellofresh.co.uk/gw", transport=httpx.MockTransport(unavailable))
    client.restore_session = lambda: pytest.fail("Must not restore or retry after a network error")
    with pytest.raises(httpx.ReadTimeout):
        client.cancel_plan("p9")
    assert calls == ["POST"]


def test_no_session_means_sign_in_again(tmp_path):
    client = HelloFreshClient(TokenStore(tmp_path / "none.json"))
    assert client.status() == "logged_out"
    with pytest.raises(NeedsLogin):
        client.subscriptions()


def test_skip_sends_the_sites_patch(gw):
    gateway, client = gw
    client.login("me@x.com", "right")
    client.skip_week("7", "2030-W42")
    method, path, params, body = gateway.calls[-1]
    assert (method, path) == ("PATCH", "/gw/api/subscriptions/7/delivery_dates/2030-W42")
    assert params == {"country": "GB", "locale": "en-GB"}
    assert body == {"delivery": {"cutoffDate": "2030-10-15T23:59:00Z", "deliveryDate": "2030-10-20",
                                 "status": "PAUSED", "subscriptionId": "7", "id": "2030-W42"}}


def test_cancel_posts_the_cancel_then_the_reason(gw):
    gateway, client = gw
    client.login("me@x.com", "right")
    client.cancel_plan("p9", reason="Too expensive")
    assert [(c[0], c[1], c[3]) for c in gateway.calls[-2:]] == [
        ("POST", "/gw/api/plans/p9/cancel", {}),
        ("POST", "/gw/api/plans/p9/cancellation/reason", {"reason": "Too expensive"})]


FIX = __import__("pathlib").Path(__file__).parent / "fixtures" / "hellofresh"


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text())


def test_boxes_read_a_real_deliveries_answer():
    """Captured from a real account: one box delivered, one being packed, three skipped."""
    got = boxes.summarise(fixture("deliveries"), {"2026-W42": ["Chicken Katsu"]})
    assert [(b.week, b.status, b.skipped, b.changeable) for b in got] == [
        ("2026-W41", "DELIVERED", False, False), ("2026-W42", "RUNNING", False, False),
        ("2026-W43", "PAUSED", True, True), ("2026-W44", "PAUSED", True, True), ("2026-W45", "PAUSED", True, True)]
    assert got[1].meals == ("Chicken Katsu",) and got[1].cutoff == "2026-10-07T23:59:59+0100"
    assert got[1].subscription_id == "1234567"
    assert boxes.week_range(__import__("datetime").date(2030, 12, 30), 2) == ("2031-W01", "2031-W02")


def test_the_week_menu_is_asked_for_as_the_site_does_and_read_for_chosen_meals():
    sub = fixture("subscriptions")["items"][0]
    params = boxes.menu_params(sub, fixture("delivery_week"), "2026-W44")
    assert {k: params[k] for k in (
        "customerPlanId", "delivery-option", "product-sku", "servings", "subscription", "preference", "postcode")} == {
        "customerPlanId": "00000000-0000-4000-8000-000000000001", "delivery-option": "GB-1-0800-2100",
        "product-sku": "GB-CBU-3-4-0", "servings": "4", "subscription": "1234567", "preference": "chefschoice",
        "postcode": "SW1A1AA"}
    assert boxes.chosen_meals(fixture("menu")) == ["Crispy Chicken Tenders and Cheesy Chips",
                                                   "Beef and Pork Rogan Josh Style Curry",
                                                   "Mexican Style Cheesy Pork Nachos Rapidos"]


def test_real_subscriptions_and_plans_are_live():
    assert boxes.live_ids(fixture("subscriptions")) == ["1234567"]
    assert boxes.live_ids(fixture("plans")) == ["00000000-0000-4000-8000-000000000001"]
    assert boxes.live_ids([{"id": "x", "finalDeliveryWeek": "2026-W50"}, {"id": "y", "isActive": False}]) == []


def test_changes_need_confirming():
    class Fake:
        def skip_week(self, sub, week):
            return {"skipped": week}

    main.app.dependency_overrides[api.get_hellofresh_client] = lambda: Fake()
    try:
        with TestClient(main.app) as client:
            url = "/api/hellofresh/subscriptions/7/weeks/2030-W42/skip"
            assert client.post(url, json={}).status_code == 400
            assert client.post(url, json={"confirm": True}).json() == {"skipped": "2030-W42"}
            assert client.post("/api/hellofresh/plans/p9/cancel", json={"reason": "x"}).status_code == 400
    finally:
        main.app.dependency_overrides.clear()


def test_status_without_an_account_is_logged_out():
    with TestClient(main.app) as client:
        assert client.get("/api/hellofresh/status").json() == {"status": "logged_out", "email": None}
        assert client.get("/api/hellofresh/boxes").status_code == 404


def test_a_week_is_an_iso_week_or_any_date_in_it():
    assert boxes.week_of("2030-W42") == boxes.week_of("2030-10-20") == boxes.week_of("2030-10-14") == "2030-W42"
    with pytest.raises(ValueError):
        boxes.week_of("next week")


def test_cutoff_and_live_ids():
    from datetime import datetime, timezone
    now = datetime(2030, 10, 15, 12, tzinfo=timezone.utc)
    assert not boxes.past_cutoff({"cutoffDate": "2030-10-15T23:59:00Z"}, now)
    assert boxes.past_cutoff({"cutoffDate": "2030-10-15T11:00:00Z"}, now)
    assert not boxes.past_cutoff({"cutoffDate": "2030-10-15"}, now)  # a bare date runs to its end
    assert not boxes.past_cutoff({}, now)
    assert boxes.changeable({"cutoffDate": "2000-01-01T00:00:00Z", "allowedActions": {"pause": True}})
    assert not boxes.changeable({"cutoffDate": "2099-01-01T00:00:00Z", "allowedActions": {"pause": False}})


class FakeAccount:
    """The account as the site would answer: one subscription, one plan, weeks by cutoff."""

    def __init__(self, subscriptions=({"id": 7, "status": "ACTIVE"},)):
        self._subscriptions = {"items": list(subscriptions)}
        self.changes = []
        self.cancelled = []

    def subscriptions(self):
        return self._subscriptions

    def plans(self):
        return [{"id": "p9", "status": "ACTIVE"}]

    def delivery(self, sub, week):
        cutoff = "2000-01-01T00:00:00Z" if week == "2000-W01" else "2099-01-01T00:00:00Z"
        status = "PAUSED" if week == "2099-W02" else "RUNNING"
        return {"delivery": {"id": week, "cutoffDate": cutoff, "deliveryDate": "x", "status": status}}

    def set_delivery_status(self, sub, week, status, *, cutoff_date, delivery_date):
        self.changes.append((sub, week, status))

    def cancel_plan(self, plan_id, reason=None):
        self.cancelled.append((plan_id, reason))
        return {"ok": True}


@pytest.fixture
def account():
    fake = FakeAccount()
    main.app.dependency_overrides[api.get_hellofresh_client] = lambda: fake
    try:
        with TestClient(main.app) as client:
            yield fake, client
    finally:
        main.app.dependency_overrides.clear()


def test_skip_a_week_by_date_without_naming_the_subscription(account):
    fake, client = account
    assert client.post("/api/hellofresh/weeks/2099-01-01/skip", json={}).status_code == 400
    out = client.post("/api/hellofresh/weeks/2099-01-01/skip", json={"confirm": True}).json()
    assert out == {"week": "2099-W01", "subscription_id": "7", "status": "PAUSED", "changed": True}
    assert fake.changes == [("7", "2099-W01", "PAUSED")]
    # already skipped: nothing sent
    assert client.post("/api/hellofresh/weeks/2099-W02/skip", json={"confirm": True}).json()["changed"] is False
    assert client.post("/api/hellofresh/weeks/2099-W02/unskip", json={"confirm": True}).json()["changed"] is True
    # past its cutoff: refused, not sent
    assert client.post("/api/hellofresh/weeks/2000-W01/skip", json={"confirm": True}).status_code == 409
    assert len(fake.changes) == 2


def test_two_subscriptions_must_be_told_apart(account):
    fake, client = account
    fake._subscriptions = {"items": [{"id": 7}, {"id": 8}]}
    r = client.post("/api/hellofresh/weeks/2099-W03/skip", json={"confirm": True})
    assert r.status_code == 409 and "7, 8" in r.json()["detail"]
    assert client.post("/api/hellofresh/weeks/2099-W03/skip",
                       json={"confirm": True, "subscription_id": "8"}).json()["subscription_id"] == "8"


def test_cancel_the_whole_subscription(account):
    fake, client = account
    assert client.post("/api/hellofresh/cancel", json={"reason": "Moving"}).status_code == 400
    assert client.post("/api/hellofresh/cancel", json={"confirm": True, "reason": "Moving"}).json()["plan_id"] == "p9"
    assert fake.cancelled == [("p9", "Moving")]


def test_coming_boxes_fetch_menus_only_for_boxes_still_coming():
    class Real:
        asked = []

        def deliveries(self, start, end):
            return fixture("deliveries")

        def subscriptions(self):
            return fixture("subscriptions")

        def week_menu(self, params):
            self.asked.append(params["week"])
            return fixture("menu")

    got = api.coming_boxes(Real(), "2026-W41", "2026-W45")
    assert Real.asked == ["2026-W42"]  # delivered and skipped weeks have no menu to read
    assert len(next(b for b in got if b.week == "2026-W42").meals) == 3
