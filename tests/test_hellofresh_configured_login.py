"""HelloFresh logins kept in the env file, one per person, and the household managing each other's."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import config
from app.api import hellofresh
from app.db import retailer_accounts
from app.db.models import User
from app.hellofresh.client import HelloFreshError


class FakeClient:
    logins: list = []
    refuse = False

    def __init__(self):
        self.state = "logged_out"

    def status(self):
        return self.state

    def refresh(self):
        raise hellofresh.NeedsLogin("expired")

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
    monkeypatch.setattr(config, "HELLOFRESH_LOGINS", {"me@x.com": ("box@x.com", "pw1"),
                                                      "partner@x.com": ("box2@x.com", "pw2")})
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


def test_logins_are_read_per_person_from_the_env(monkeypatch):
    for key, value in {"HOLAFRESCA_HELLOFRESH_EMAIL": "box@x.com", "HOLAFRESCA_HELLOFRESH_PASSWORD": "a|b,c",
                       "HOLAFRESCA_HELLOFRESH_2_FOR": "Partner@x.com", "HOLAFRESCA_HELLOFRESH_2_EMAIL": "box2@x.com",
                       "HOLAFRESCA_HELLOFRESH_2_PASSWORD": "pw2", "HOLAFRESCA_HELLOFRESH_3_EMAIL": "no-for@x.com",
                       "HOLAFRESCA_HELLOFRESH_3_PASSWORD": "x"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "ACCESS_OWNER_EMAIL", "Me@x.com")
    assert config._hellofresh_logins() == {"me@x.com": ("box@x.com", "a|b,c"), "partner@x.com": ("box2@x.com", "pw2")}
