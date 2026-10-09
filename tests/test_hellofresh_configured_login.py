"""HelloFresh logins kept in the env file, one per person, and the household managing each other's."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import config
from app.api import hellofresh
from app.db import retailer_accounts
from app.db.models import User
from app.hellofresh.client import HelloFreshError


class FakeTokens:
    def __init__(self):
        self.refresh_token = None

    def save(self, token):
        self.refresh_token = token.get("refresh_token") or self.refresh_token


class FakeClient:
    logins: list = []
    refuse = False
    good_refresh = {"browser-token"}

    def __init__(self):
        self.state, self.tokens = "logged_out", FakeTokens()

    def status(self):
        if self.state == "ready":
            return "ready"
        return "expired" if self.tokens.refresh_token else "logged_out"

    def refresh(self):
        if self.tokens.refresh_token not in FakeClient.good_refresh:
            raise hellofresh.NeedsLogin("expired")
        self.state = "ready"

    def login(self, email, password):
        FakeClient.logins.append(email)
        if FakeClient.refuse:
            raise HelloFreshError("refused")
        self.state = "ready"


@pytest.fixture
def household(factory, monkeypatch):
    FakeClient.logins, FakeClient.refuse = [], False
    hellofresh._login_failed_at.clear()
    monkeypatch.setattr(config, "HOUSEHOLD", {"me@x.com", "partner@x.com"})
    monkeypatch.setattr(config, "HELLOFRESH_LOGINS", {"me@x.com": ("box@x.com", "pw1", None),
                                                      "partner@x.com": ("box2@x.com", "pw2", None)})
    clients = {}
    monkeypatch.setattr(hellofresh, "client_for", lambda account: clients.setdefault(account.key, FakeClient()))
    with factory() as session:
        me = session.query(User).order_by(User.id).first()
        me.email = "me@x.com"
        partner, friend = User(email="partner@x.com", name="P"), User(email="friend@x.com", name="F")
        session.add_all([partner, friend])
        session.commit()
        yield session, me, partner, friend


def test_each_person_is_signed_in_with_their_own_login(household):
    session, me, partner, _ = household
    assert hellofresh.configured_login(session, me).email == "box@x.com"
    assert hellofresh.configured_login(session, partner).email == "box2@x.com"
    assert FakeClient.logins == ["box@x.com", "box2@x.com"]
    hellofresh.configured_login(session, me)
    assert len(FakeClient.logins) == 2  # still signed in: no second login
    assert retailer_accounts.find(session, me.id, "hellofresh").status == "connected"


def test_a_household_member_can_act_on_the_others_subscription(household):
    session, me, _, friend = household
    assert [a.email for a in hellofresh.household_accounts(session, me)] == ["box@x.com", "box2@x.com"]
    assert hellofresh._account(session, me, "BOX2@x.com").email == "box2@x.com"
    assert hellofresh._account(session, me).email == "box@x.com"
    with pytest.raises(HTTPException):
        hellofresh._account(session, friend, "box2@x.com")  # outside the household
    assert hellofresh.configured_login(session, friend) is None  # and has no login of their own


def test_a_refused_password_isnt_retried_on_every_request(household):
    session, me, _, _ = household
    FakeClient.refuse = True
    assert hellofresh.configured_login(session, me) is None
    assert hellofresh.configured_login(session, me) is None
    assert FakeClient.logins == ["box@x.com"]


def test_expired_session_restores_from_password_on_an_account_request(household):
    session, me, _, _ = household
    account = hellofresh.configured_login(session, me)
    client = hellofresh.client_for(account)
    client.state = "expired"
    client.tokens.refresh_token = "revoked"
    assert hellofresh._account(session, me).key == account.key
    assert client.status() == "ready"
    assert FakeClient.logins == ["box@x.com", "box@x.com"]


def test_network_failure_during_configured_login_is_throttled(household, monkeypatch):
    import httpx
    session, me, _, _ = household
    calls = []

    def timeout(*args):
        calls.append(True)
        raise httpx.ConnectTimeout("unreachable")

    monkeypatch.setattr(FakeClient, "login", timeout)
    assert hellofresh.configured_login(session, me) is None
    assert hellofresh.configured_login(session, me) is None
    assert len(calls) == 1


def test_session_recovery_uses_the_selected_accounts_owner(household):
    session, me, _, _ = household
    client = hellofresh.get_hellofresh_client("box2@x.com", session, me)
    FakeClient.logins.clear()
    assert client.restore_session()
    assert FakeClient.logins == ["box2@x.com"]


def test_logins_are_read_per_person_from_the_env(monkeypatch):
    for key, value in {"HOLAFRESCA_HELLOFRESH_EMAIL": "box@x.com", "HOLAFRESCA_HELLOFRESH_PASSWORD": "a|b,c",
                       "HOLAFRESCA_HELLOFRESH_2_FOR": "Partner@x.com", "HOLAFRESCA_HELLOFRESH_2_EMAIL": "box2@x.com",
                       "HOLAFRESCA_HELLOFRESH_2_PASSWORD": "pw2", "HOLAFRESCA_HELLOFRESH_3_EMAIL": "no-for@x.com",
                       "HOLAFRESCA_HELLOFRESH_3_PASSWORD": "x"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "ACCESS_OWNER_EMAIL", "Me@x.com")
    monkeypatch.setenv("HOLAFRESCA_HELLOFRESH_4_FOR", "token@x.com")
    monkeypatch.setenv("HOLAFRESCA_HELLOFRESH_4_EMAIL", "box4@x.com")
    monkeypatch.setenv("HOLAFRESCA_HELLOFRESH_4_REFRESH_TOKEN", " rt ")
    assert config._hellofresh_logins() == {"me@x.com": ("box@x.com", "a|b,c", None),
                                           "partner@x.com": ("box2@x.com", "pw2", None),
                                           "token@x.com": ("box4@x.com", None, "rt")}


def test_a_refresh_token_from_the_browser_restores_the_session_without_the_login(household, monkeypatch):
    session, me, _, _ = household
    FakeClient.refuse = True  # the login is behind Cloudflare's challenge
    monkeypatch.setitem(config.HELLOFRESH_LOGINS, "me@x.com", ("box@x.com", None, "browser-token"))
    account = hellofresh.configured_login(session, me)
    assert account is not None and FakeClient.logins == []
    assert retailer_accounts.find(session, me.id, "hellofresh").status == "connected"
