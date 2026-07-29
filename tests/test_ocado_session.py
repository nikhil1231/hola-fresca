from __future__ import annotations

import httpx

from app.ocado.session import OcadoSession


class FakeAuth:
    def __init__(self):
        self.calls = 0

    def ensure_authenticated(self, session):
        self.calls += 1
        session.client.cookies.set("global_sid", "fresh", domain="www.ocado.com")
        return "ready"


def test_csrf_failure_rescrapes_once_and_retries(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("x-csrf-token")))
        if request.url.path == "/basket":
            token = "new-token" if len([c for c in calls if c[1] == "/basket"]) > 1 else "old-token"
            return httpx.Response(200, text=f'{{"csrf":{{"token":"{token}"}}}}')
        if request.headers.get("x-csrf-token") == "old-token":
            return httpx.Response(403, headers={"ecom-csrf-failure": "true"})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(base_url="https://www.ocado.com", transport=httpx.MockTransport(handler))
    session = OcadoSession(client=client, jar_path=tmp_path / "session.json", auth=FakeAuth())

    response = session.request("POST", "/write", json={"x": 1})

    assert response.status_code == 200
    assert calls == [
        ("GET", "/basket", None),
        ("POST", "/write", "old-token"),
        ("GET", "/basket", None),
        ("POST", "/write", "new-token"),
    ]


def test_401_triggers_one_reauth_and_retry(tmp_path):
    auth = FakeAuth()
    seen = 0

    def handler(request):
        nonlocal seen
        seen += 1
        if seen == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(base_url="https://www.ocado.com", transport=httpx.MockTransport(handler))
    session = OcadoSession(client=client, jar_path=tmp_path / "session.json", auth=auth)

    response = session.request("GET", "/webshop/getBasket.do")

    assert response.status_code == 200
    assert auth.calls == 1
    assert seen == 2

