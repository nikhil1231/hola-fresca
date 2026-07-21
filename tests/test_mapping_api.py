"""Mapping review API: list, detail overlay, save decision, bulk approve."""
from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from tests.conftest import seed_candidates

KEY = "name:chicken breast"
KEY_Q = quote(KEY, safe="")
PRODUCTS = [
    {"sku": "p1", "name": "Ocado Chicken Breast", "price": 3.5, "pack_value": 600, "pack_unit": "g"},
    {"sku": "p2", "name": "Mini Fillets", "price": 2.5, "pack_value": 300, "pack_unit": "g"},
]


@pytest.fixture
def client(factory, tmp_path, monkeypatch):
    import main
    from app.api import mapping as mapping_api
    from app.api.deps import get_session

    (tmp_path / "ingredient_frequency.csv").write_text(
        "rank,ingredient_key,source_ingredient_ids,name,line_count,metric_unit,"
        "median_metric_amount,p25_metric_amount,p75_metric_amount,common_native_amounts,name_variants\n"
        "1,name:chicken breast,sid1,Chicken Breast,500,g,450,400,500,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path)
    mapping_api._usage_stats.cache_clear()

    def override():
        with factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = override
    with factory() as s:
        seed_candidates(s, KEY, "Chicken Breast", PRODUCTS, line_count=500)
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    mapping_api._usage_stats.cache_clear()


def test_detail_before_any_decision_lists_candidates(client):
    r = client.get(f"/api/mapping/ingredients/{KEY_Q}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] is None
    assert len(body["candidates"]) == 2
    assert body["usage"]["median"] == 450


def test_unknown_ingredient_returns_404(client):
    r = client.get("/api/mapping/ingredients/name:does-not-exist")
    assert r.status_code == 404


def test_save_decision_then_list_and_detail(client):
    save = client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={
            "status": "approved",
            "accepted": [{"sku": "p2", "rank": 1, "match_type": "exact", "reason": "value"}],
            "each_to_grams": None,
            "needs_substitution": False,
            "reviewer_notes": "ok",
        },
    )
    assert save.status_code == 200
    assert save.json()["status"] == "approved"
    accepted = [c for c in save.json()["candidates"] if c["accepted"]]
    assert [c["sku"] for c in accepted] == ["p2"]

    listing = client.get("/api/mapping/ingredients").json()
    assert listing["counts"] == {"approved": 1}
    assert listing["items"][0]["ingredient_key"] == KEY
    assert listing["items"][0]["num_accepted"] == 1


def test_invalid_status_rejected(client):
    r = client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "banana", "accepted": []},
    )
    assert r.status_code == 400


def test_bulk_approve_endpoint(client):
    client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "proposed", "accepted": [{"sku": "p1", "rank": 1, "match_type": "exact"}]},
    )
    r = client.post("/api/mapping/bulk-approve", json={"keys": [KEY]})
    assert r.status_code == 200 and r.json()["approved"] == 1
    assert client.get("/api/mapping/ingredients").json()["counts"] == {"approved": 1}
