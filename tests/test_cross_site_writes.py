"""Refusing state-changing API calls that another site caused.

Cloudflare Access assertions arrive in a cookie as well as a header, and a
cookie is attached by the browser to requests any site can make. Without this,
a page on another origin could POST to this API fully authenticated as whoever
was signed in — push a basket, disconnect a shop, or hand the login endpoint a
retailer password.

The rule is "refuse on positive evidence", not "require proof of same origin",
and both halves of that are pinned here: a request that says it is cross-site
is refused, and a request that says nothing is let through, because what sends
no such header is a script or the test suite rather than an attacker's page.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def test_a_cross_site_write_is_refused(client):
    response = client.post(
        "/api/cart/ocado/logout", headers={"Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403
    assert "Cross-site" in response.json()["detail"]


def test_a_same_origin_write_is_allowed_through(client):
    """Allowed *through the middleware*; the route may still refuse it."""
    response = client.post(
        "/api/cart/ocado/logout", headers={"Sec-Fetch-Site": "same-origin"}
    )

    assert response.status_code != 403


def test_a_direct_navigation_is_not_cross_site(client):
    """``none`` is a typed or bookmarked URL, which no page caused."""
    response = client.post("/api/cart/ocado/logout", headers={"Sec-Fetch-Site": "none"})

    assert response.status_code != 403


def test_a_cross_origin_header_is_refused_without_the_fetch_metadata(client):
    """Older browsers send no Sec-Fetch-Site, but a cross-origin post has Origin."""
    response = client.post(
        "/api/cart/ocado/logout",
        headers={"Origin": "https://evil.example", "Host": "holafresca.uk"},
    )

    assert response.status_code == 403


def test_a_matching_origin_is_allowed_through(client):
    response = client.post(
        "/api/cart/ocado/logout",
        headers={"Origin": "https://holafresca.uk", "Host": "holafresca.uk"},
    )

    assert response.status_code != 403


def test_a_request_with_no_site_headers_is_allowed(client):
    """curl, the deploy scripts, this test suite.

    None of them can be aimed at somebody else's session by a web page, which is
    the entire threat. Refusing them would break every script for no safety.
    """
    assert client.post("/api/cart/ocado/logout").status_code != 403


def test_reads_are_never_refused(client):
    """A cross-site GET is a read the browser was always going to allow."""
    response = client.get("/api/health", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 200


def test_the_frontend_is_not_subject_to_it(client):
    """Only /api/ is guarded; the SPA itself is a static asset."""
    response = client.post("/", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code != 403
