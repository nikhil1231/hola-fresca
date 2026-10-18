"""Exercise authorization at the HTTP boundary, not router source spelling."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import main
from app.api import access
from app.api.deps import get_current_user
from app.db.models import User


def _writes():
    for path, operations in main.app.openapi()["paths"].items():
        if not path.startswith("/api/"):
            continue
        path = re.sub(r"\{([^}:]+)(?::[^}]+)?\}",
                      lambda match: "ocado" if match[1] == "retailer" else "1", path)
        for method in sorted(operations.keys() & {"post", "put", "patch", "delete"}):
            yield method.upper(), path


WRITES = list(_writes())


@pytest.mark.parametrize("method,path", WRITES)
def test_invalid_access_identity_cannot_change_anything(method, path, monkeypatch):
    monkeypatch.setattr("app.config.ACCESS_TEAM_DOMAIN", "test.cloudflareaccess.com")
    monkeypatch.setattr("app.config.ACCESS_AUD", "planner-audience")
    monkeypatch.setattr("app.config.OCADO_ACCESS_AUD", "shop-audience")
    monkeypatch.setattr("app.config.ACCESS_HOSTNAME", "planner.example.test")
    monkeypatch.setattr("app.config.OCADO_APP_HOSTNAME", "shop.example.test")

    def reject_key(_url):
        raise ValueError("Signature cannot be verified")

    monkeypatch.setattr(access, "_jwk_client", reject_key)
    # Local Host avoids the public-host middleware shortcut: the endpoint's
    # actual dependency chain must reject the invalid signed identity.
    response = TestClient(main.app).request(method, path, json={}, headers={
        "Cf-Access-Jwt-Assertion": "invalid-access-identity",
    })
    assert response.status_code == 403, (method, path, response.text)


@pytest.mark.parametrize("method,path", [(method, path) for method, path in WRITES
                                         if path.startswith("/api/mapping")])
def test_non_admin_cannot_change_the_shared_catalogue(method, path):
    main.app.dependency_overrides[get_current_user] = lambda: User(id=1, is_admin=0)
    try:
        response = TestClient(main.app).request(method, path, json={})
        assert response.status_code == 403, (method, path, response.text)
    finally:
        main.app.dependency_overrides.pop(get_current_user, None)
