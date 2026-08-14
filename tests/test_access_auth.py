"""Cloudflare Access identity: what is trusted, what is refused, whose account.

The signing key is generated here and the key lookup is stubbed, so these run
without touching the network. Everything else — the audience, issuer and expiry
checks, the header/cookie handling, the account resolution — is the real code.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

import main
from app.api import access
from app.api.deps import get_session
from app.db.models import User
from app.db.session import init_db, make_engine, make_session_factory

TEAM = "example.cloudflareaccess.com"
AUD = "aud-tag-for-the-holafresca-app"
HOSTNAME = "hola.example.com"
OWNER = "owner@example.com"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(tmp_path / "access.db")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def configured(monkeypatch, signing_key, factory):
    """Access switched on, its key lookup stubbed, the API on a temp database."""
    monkeypatch.setattr("app.config.ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setattr("app.config.ACCESS_AUD", AUD)
    monkeypatch.setattr("app.config.ACCESS_HOSTNAME", HOSTNAME)
    monkeypatch.setattr("app.config.ACCESS_OWNER_EMAIL", OWNER)

    class _Key:
        key = signing_key.public_key()

    class _Client:
        def get_signing_key_from_jwt(self, token):  # noqa: ARG002 - one key here
            return _Key()

    monkeypatch.setattr(access, "_jwk_client", lambda certs_url: _Client())

    def override():
        with factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = override
    yield factory
    main.app.dependency_overrides.clear()


def make_token(
    signing_key,
    *,
    email: str = OWNER,
    aud: str = AUD,
    issuer: str = f"https://{TEAM}",
    expires_in: timedelta = timedelta(hours=1),
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "email": email,
            "aud": aud,
            "iss": issuer,
            "iat": now,
            "exp": now + expires_in,
            "sub": "some-access-user-id",
        },
        signing_key,
        algorithm="RS256",
    )


def get(client: TestClient, *, host: str | None = None, token: str | None = None):
    headers = {}
    if host is not None:
        headers["Host"] = host
    if token is not None:
        headers["Cf-Access-Jwt-Assertion"] = token
    return client.get("/api/schedule", headers=headers)


def test_lan_request_without_an_assertion_is_the_bootstrap_user(configured):
    """The laptop still answers on its own address, as it did before Access."""
    client = TestClient(main.app)
    assert get(client, host="192.168.1.50:8100").status_code == 200


def test_tunnel_request_without_an_assertion_is_refused(configured):
    """The case that matters if Access is ever off: no silent fallback."""
    client = TestClient(main.app)
    response = get(client, host=HOSTNAME)

    assert response.status_code == 403


def test_spoofed_identity_header_is_ignored(configured):
    """The header Cloudflare sets is a claim; anyone on the LAN can set it too."""
    client = TestClient(main.app)
    response = client.get(
        "/api/schedule",
        headers={
            "Host": HOSTNAME,
            "Cf-Access-Authenticated-User-Email": "attacker@example.com",
        },
    )

    assert response.status_code == 403


def test_valid_assertion_is_accepted(configured, signing_key):
    client = TestClient(main.app)
    response = get(client, host=HOSTNAME, token=make_token(signing_key))

    assert response.status_code == 200


def test_assertion_may_arrive_as_a_cookie(configured, signing_key):
    """Which is how it arrives on the page load, rather than on an API call."""
    client = TestClient(main.app)
    client.cookies.set("CF_Authorization", make_token(signing_key))
    response = client.get("/api/schedule", headers={"Host": HOSTNAME})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"aud": "someone-elses-app"}, id="another app's audience"),
        pytest.param({"issuer": "https://evil.cloudflareaccess.com"}, id="another team"),
        pytest.param({"expires_in": timedelta(hours=-1)}, id="expired"),
        pytest.param({"email": ""}, id="no email, e.g. a service token"),
    ],
)
def test_bad_assertions_are_refused(configured, signing_key, kwargs):
    client = TestClient(main.app)
    response = get(client, host=HOSTNAME, token=make_token(signing_key, **kwargs))

    assert response.status_code == 403


def test_assertion_signed_by_the_wrong_key_is_refused(configured):
    """A well-formed token is still only worth its signature."""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = TestClient(main.app)
    response = get(client, host=HOSTNAME, token=make_token(other))

    assert response.status_code == 403


def test_owner_claims_the_bootstrap_account(configured, signing_key):
    """Rather than starting a second, empty account beside their own data."""
    factory = configured
    with factory() as session:
        before = session.scalars(select(User).order_by(User.id)).all()
        assert [u.email for u in before] == [None]
        bootstrap_id = before[0].id

    client = TestClient(main.app)
    assert get(client, host=HOSTNAME, token=make_token(signing_key)).status_code == 200

    with factory() as session:
        users = session.scalars(select(User).order_by(User.id)).all()
        assert [(u.id, u.email, bool(u.is_admin)) for u in users] == [
            (bootstrap_id, OWNER, True)
        ]


def test_another_allowed_address_gets_its_own_non_admin_account(configured, signing_key):
    """Access vetted them at the edge; they are not the owner, though."""
    factory = configured
    client = TestClient(main.app)
    token = make_token(signing_key, email="flatmate@example.com")

    assert get(client, host=HOSTNAME, token=token).status_code == 200
    assert get(client, host=HOSTNAME, token=token).status_code == 200

    with factory() as session:
        users = session.scalars(select(User).order_by(User.id)).all()
        # Twice through, one account: the second request found the first's row.
        assert [(u.email, bool(u.is_admin)) for u in users] == [
            (None, True),
            ("flatmate@example.com", False),
        ]


def test_email_match_is_case_insensitive(configured, signing_key):
    factory = configured
    client = TestClient(main.app)

    assert get(client, host=HOSTNAME, token=make_token(signing_key)).status_code == 200
    upper = make_token(signing_key, email=OWNER.upper())
    assert get(client, host=HOSTNAME, token=upper).status_code == 200

    with factory() as session:
        assert len(session.scalars(select(User)).all()) == 1


def test_access_unconfigured_changes_nothing(monkeypatch, factory):
    """Local dev and the test suite: no team domain, no enforcement anywhere."""
    monkeypatch.setattr("app.config.ACCESS_TEAM_DOMAIN", None)
    monkeypatch.setattr("app.config.ACCESS_AUD", None)

    def override():
        with factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = override
    try:
        client = TestClient(main.app)
        # Even addressed to the public name, and even with a junk assertion
        # attached, because nothing is being checked.
        assert get(client, host=HOSTNAME).status_code == 200
        assert get(client, host=HOSTNAME, token="not-a-jwt").status_code == 200
    finally:
        main.app.dependency_overrides.clear()
