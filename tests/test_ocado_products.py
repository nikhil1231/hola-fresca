from __future__ import annotations

import json

from app.db.models import Product, ProductScrapeState, ProductSearchHit
from app.db.session import init_db, make_engine, make_session_factory
from app.scraper.products import pipeline, storage
from app.scraper.products.ocado import (
    RETAILER,
    extract_product_ids,
    normalize_product,
    parse_pack_size,
    parse_unit_price,
)
from app.scraper.products.worklist import load_worklist


def test_worklist_loader_takes_ranked_top_n(tmp_path):
    csv_path = tmp_path / "ingredient_frequency.csv"
    csv_path.write_text(
        "rank,ingredient_key,name,line_count\n"
        "2,name:honey,Honey,20\n"
        "1,name:garlic,Garlic Clove,30\n"
        "3,name:lime,Lime,10\n",
        encoding="utf-8",
    )

    rows = load_worklist(csv_path, limit=2)

    assert [row.name for row in rows] == ["Garlic Clove", "Honey"]
    assert rows[0].line_count == 30


def test_raw_cache_paths_are_deterministic(tmp_path):
    one = storage.write_raw(RETAILER, "search", "Garlic Clove", {"ok": True}, tmp_path)
    two = storage.raw_path(RETAILER, "search", "Garlic Clove", tmp_path)

    assert one == two
    assert storage.read_raw(RETAILER, "search", "Garlic Clove", tmp_path) == {"ok": True}


def test_ocado_fixture_parser_extracts_product_fields():
    product = json.loads(open("tests/fixtures/ocado_product_potato.json", encoding="utf-8").read())
    normalized = normalize_product(product)

    assert normalized.sku == "552ecfc0-e064-4916-8968-4ed4c64c58de"
    assert normalized.name == "Ocado White Potatoes 2kg"
    assert normalized.brand == "Ocado"
    assert normalized.pack_size_raw == "2kg"
    assert normalized.pack_size_value == 2000
    assert normalized.pack_size_unit == "g"
    assert normalized.price == 1.8
    assert normalized.unit_price == 0.9
    assert normalized.unit_price_basis == "kg"
    assert normalized.category == "Fresh & Chilled Food > Vegetables"
    assert normalized.in_stock is None
    assert normalized.image_url == "https://images.ocado.com/products/552ecfc0.jpg"
    assert normalized.url == "https://www.ocado.com/products/ocado-white-potatoes-552ecfc0"


def test_product_url_falls_back_to_retailer_product_id():
    # Real Ocado payloads carry no url field; the UUID productId is not a valid
    # path (404), but /products/<retailerProductId> 301s to the canonical page.
    product = {
        "productId": "233a6dd5-cf2a-4e0d-ae2b-9cbb1f17a7a5",
        "retailerProductId": "628808011",
        "name": "Eat Real Lentil Creamy Dill Chips",
        "price": {"amount": "2.00", "currency": "GBP"},
    }
    normalized = normalize_product(product)
    assert normalized.url == "https://www.ocado.com/products/628808011"


def test_ocado_search_fixture_extracts_candidate_ids():
    search = json.loads(open("tests/fixtures/ocado_search_potatoes.json", encoding="utf-8").read())

    assert extract_product_ids(search) == [
        "552ecfc0-e064-4916-8968-4ed4c64c58de",
        "baac2960-65d0-4eab-91cc-944bdad56996",
    ]


def test_pack_size_parser_common_forms():
    assert parse_pack_size("400g") == (400, "g")
    assert parse_pack_size("1L") == (1000, "ml")
    assert parse_pack_size("2 x 400g") == (800, "g")
    assert parse_pack_size("6 per pack") == (6, "each")
    assert parse_pack_size("500ml") == (500, "ml")


def test_unit_price_parser_common_forms():
    assert parse_unit_price("£1.50 per kg") == (1.5, "kg")
    assert parse_unit_price("95p/100g") == (0.95, "100g")
    assert parse_unit_price("£2.20 per litre") == (2.2, "l")


def test_product_upsert_and_search_hit_linking_are_restart_safe(tmp_path, monkeypatch):
    db_path = tmp_path / "products.db"
    engine = make_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)
    raw_dir = tmp_path / "raw"
    csv_path = tmp_path / "ingredient_frequency.csv"
    csv_path.write_text(
        "rank,ingredient_key,name,line_count\n1,name:potatoes,Potatoes,3314\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.RAW_DIR", raw_dir)

    product = json.loads(open("tests/fixtures/ocado_product_potato.json", encoding="utf-8").read())
    search = json.loads(open("tests/fixtures/ocado_search_potatoes.json", encoding="utf-8").read())
    search_key = "1:name:potatoes"
    sku = product["id"]
    storage.write_raw(RETAILER, "product", sku, {"sku": sku, "response": product})
    storage.write_raw(
        RETAILER,
        "search",
        search_key,
        {
            "search_term": "Potatoes",
            "ingredient_key": "name:potatoes",
            "term_rank": 1,
            "line_count": 3314,
            "product_ids": [sku],
            "response": search,
        },
    )
    with factory() as session:
        session.add_all(
            [
                ProductScrapeState(
                    retailer=RETAILER, kind="product", key=sku, status="fetched"
                ),
                ProductScrapeState(
                    retailer=RETAILER,
                    kind="search",
                    key=search_key,
                    label=json.dumps(
                        {
                            "rank": 1,
                            "ingredient_key": "name:potatoes",
                            "name": "Potatoes",
                            "line_count": 3314,
                        }
                    ),
                    status="fetched",
                ),
            ]
        )
        session.commit()

    pipeline.normalize(factory, limit=1, csv_path=csv_path)
    pipeline.normalize(factory, limit=1, csv_path=csv_path, force=True)

    with factory() as session:
        assert session.query(Product).count() == 1
        assert session.query(ProductSearchHit).count() == 1
        hit = session.query(ProductSearchHit).one()
        assert hit.search_term == "Potatoes"
        assert hit.sku == sku
