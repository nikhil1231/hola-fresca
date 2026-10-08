"""The owner's HelloFresh login kept in the env file: used when there's no working session, never for anyone else."""
from __future__ import annotations

import pytest

from app import config
from app.api import hellofresh
from app.db import retailer_accounts
from app.db.models import User
from app.hellofresh.client import HelloFreshError


class FakeClient:
    logins: list = []
    refuse = False

    def __init__(self, state="logged_out"):
        self.state = state

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
def owner(factory, monkeypatch):
    FakeClient.logins, FakeClient.refuse = [], False
    hellofresh._login_failed_at = None
    monkeypatch.setattr(config, "ACCESS_OWNER_EMAIL", "owner@x.com")
    monkeypatch.setattr(config, "HELLOFRESH_EMAIL", "box@x.com")
    monkeypatch.setattr(config, "HELLOFRESH_PASSWORD", "secret")
    clients = {}
    monkeypatch.setattr(hellofresh, "client_for", lambda account: clients.setdefault(account.key, FakeClient()))
    with factory() as session:
        user = session.query(User).order_by(User.id).first()
        user.email = "owner@x.com"
        session.commit()
        yield session, user


def test_the_owner_is_signed_in_from_the_env_file(owner):
    session, user = owner
    account = hellofresh.configured_login(session, user)
    assert account is not None and FakeClient.logins == ["box@x.com"]
    assert retailer_accounts.find(session, user.id, "hellofresh").status == "connected"
    hellofresh.configured_login(session, user)
    assert FakeClient.logins == ["box@x.com"]  # still signed in: no second login


def test_nobody_else_gets_the_owners_login(owner):
    session, _ = owner
    friend = User(email="friend@x.com", name="F")
    session.add(friend)
    session.commit()
    assert hellofresh.configured_login(session, friend) is None and FakeClient.logins == []


def test_a_refused_password_isnt_retried_on_every_request(owner):
    session, user = owner
    FakeClient.refuse = True
    assert hellofresh.configured_login(session, user) is None
    assert hellofresh.configured_login(session, user) is None
    assert FakeClient.logins == ["box@x.com"]
