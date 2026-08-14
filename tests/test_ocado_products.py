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
    parse_shelf_life,
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


def test_parse_shelf_life_converts_units_to_days():
    assert parse_shelf_life({"quantity": 3, "unit": "DAY"}) == ("3 DAY", 3)
    assert parse_shelf_life({"quantity": 2, "unit": "WEEK"}) == ("2 WEEK", 14)
    assert parse_shelf_life({"quantity": 2, "unit": "MONTH"}) == ("2 MONTH", 60)
    assert parse_shelf_life({"quantity": 1, "unit": "YEAR"}) == ("1 YEAR", 365)


def test_parse_shelf_life_rejects_missing_and_unusable_values():
    # An absent field means "no stated life", not a zero-day life, so every one
    # of these must stay null rather than collapsing to 0.
    assert parse_shelf_life(None) == (None, None)
    assert parse_shelf_life({}) == (None, None)
    assert parse_shelf_life({"quantity": 0, "unit": "DAY"}) == (None, None)
    assert parse_shelf_life({"quantity": 2, "unit": "FORTNIGHT"}) == (None, None)
    assert parse_shelf_life({"quantity": "soon", "unit": "DAY"}) == (None, None)


def test_normalize_product_reads_guaranteed_product_life():
    product = {
        "productId": "233a6dd5-cf2a-4e0d-ae2b-9cbb1f17a7a5",
        "name": "Ocado Large Garlic",
        "guaranteedProductLife": {"quantity": 2, "unit": "WEEK"},
    }
    normalized = normalize_product(product)
    assert normalized.shelf_life_raw == "2 WEEK"
    assert normalized.shelf_life_days == 14


def test_normalize_product_reads_frozen_chip_as_a_storage_form():
    frozen = normalize_product(
        {
            "productId": "frozen-garlic",
            "name": "Chopped Garlic",
            "iconAttributes": [
                {"label": "Suitable for freezing", "file": "freezable"},
                {"label": "Frozen", "file": "frozen"},
            ],
        }
    )
    merely_freezable = normalize_product(
        {
            "productId": "fresh-garlic",
            "name": "Fresh Garlic",
            "iconAttributes": [
                {"label": "Suitable for freezing", "file": "freezable"},
            ],
        }
    )

    assert frozen.is_frozen is True
    assert merely_freezable.is_frozen is False


def test_normalize_product_uses_frozen_category_when_the_chip_is_absent():
    product = normalize_product(
        {
            "productId": "frozen-peas",
            "name": "Garden Peas",
            "categoryPath": ["Frozen Food", "Frozen Vegetables"],
        }
    )

    assert product.is_frozen is True


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


def _promoted(**overrides) -> dict:
    node = {
        "productId": "p1",
        "name": "Nishaan Minced Garlic 210g",
        "price": {"amount": "1.90", "currency": "GBP"},
        "promoPrice": {"amount": "1.60", "currency": "GBP"},
        "unitPrice": {
            "price": {"amount": "6.71", "currency": "GBP"},
            "unit": "fop.price.per.kg",
            "unitName": "PER_1KG",
        },
        "promoUnitPrice": {
            "price": {"amount": "5.65", "currency": "GBP"},
            "unit": "fop.price.per.kg",
            "unitName": "PER_1KG",
        },
    }
    node.update(overrides)
    return node


def test_a_promotional_price_is_what_the_basket_pays():
    # Ocado states promoPrice beside price rather than replacing it, so the
    # shelf price used to be the only one read and every offer counted for
    # nothing — the opposite of the Sainsbury's payload.
    product = normalize_product(_promoted())
    assert (product.price, product.base_price) == (1.6, 1.9)
    assert (product.unit_price, product.unit_price_basis) == (5.65, "kg")
    assert product.base_unit_price == 6.71


def test_a_multibuy_leaves_the_shelf_price_alone():
    # "Buy any 3 for £5" sets no promo price, and buying one of something is not
    # three of it. Nothing on offer as far as a single pack is concerned.
    product = normalize_product(
        _promoted(
            promoPrice=None,
            promoUnitPrice=None,
            promotions=[{"description": "Buy any 3 for £5", "requiredProductQuantity": 3}],
        )
    )
    assert (product.price, product.base_price) == (1.9, None)
    assert (product.unit_price, product.base_unit_price) == (6.71, None)


def test_a_promotional_unit_price_on_another_basis_is_not_taken():
    # Ocado restates the basis alongside it, and £5.65 per kg is not an
    # improvement on £6.71 per each.
    product = normalize_product(
        _promoted(
            unitPrice={
                "price": {"amount": "6.71", "currency": "GBP"},
                "unit": "fop.price.per.each",
                "unitName": "EACH",
            }
        )
    )
    assert (product.unit_price, product.unit_price_basis) == (6.71, "each")
    assert product.base_unit_price is None


def test_backfill_prices_never_invents_a_discount_off_a_stale_payload(tmp_path):
    # The payload is as old as the scrape. Adopting its promotional price would
    # fill the catalogue with offers that ended a fortnight ago, so the base is
    # written relative to the price already on the row: agreeing with it means
    # nothing is on offer, and the answer is NULL rather than a discount of zero.
    db_path = tmp_path / "products.db"
    engine = make_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)

    with factory() as session:
        session.add_all(
            [
                Product(
                    retailer=RETAILER,
                    sku="stale-promo",
                    name="Nishaan Minced Garlic 210g",
                    # The shelf price the scrape stored, from a payload whose
                    # promotion has since lapsed.
                    price=1.9,
                    unit_price=6.71,
                    raw_json=json.dumps(_promoted()),
                ),
                Product(
                    retailer=RETAILER,
                    sku="live-promo",
                    name="Nishaan Minced Garlic 210g",
                    # A row already repriced to the offer by a live refresh.
                    price=1.6,
                    unit_price=5.65,
                    raw_json=json.dumps(_promoted()),
                ),
            ]
        )
        session.commit()

    result = pipeline.backfill_prices(factory)

    assert (result.products, result.normalized, result.errors) == (2, 1, 0)
    with factory() as session:
        rows = {p.sku: p for p in session.query(Product).all()}
        assert (rows["stale-promo"].price, rows["stale-promo"].base_price) == (1.9, None)
        assert rows["stale-promo"].base_unit_price is None
        assert (rows["live-promo"].price, rows["live-promo"].base_price) == (1.6, 1.9)
        assert rows["live-promo"].base_unit_price == 6.71


def test_backfill_shelf_life_reparses_stored_raw_json(tmp_path):
    db_path = tmp_path / "products.db"
    engine = make_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)

    with factory() as session:
        session.add_all(
            [
                Product(
                    retailer=RETAILER,
                    sku="a",
                    name="Ocado Large Garlic",
                    raw_json=json.dumps({"guaranteedProductLife": {"quantity": 2, "unit": "WEEK"}}),
                ),
                Product(retailer=RETAILER, sku="b", name="Tinned Tomatoes", raw_json="{}"),
                Product(retailer=RETAILER, sku="c", name="Broken", raw_json="not json"),
            ]
        )
        session.commit()

    result = pipeline.backfill_shelf_life(factory)

    assert (result.products, result.normalized, result.errors) == (3, 1, 1)
    with factory() as session:
        by_sku = {p.sku: p for p in session.query(Product).all()}
        assert (by_sku["a"].shelf_life_raw, by_sku["a"].shelf_life_days) == ("2 WEEK", 14)
        assert by_sku["b"].shelf_life_days is None


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
