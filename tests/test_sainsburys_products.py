from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.scraper.products import sainsburys
from app.scraper.products.registry import ADAPTER_IDS, get_adapter, has_adapter

FIXTURE = Path(__file__).parent / "fixtures" / "sainsburys_search_chorizo.json"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def by_sku(payload) -> dict[str, object]:
    return {
        product.sku: product
        for product in (
            sainsburys.normalize_product(obj)
            for obj in sainsburys.extract_product_objects(payload)
        )
    }


def test_extract_ids_preserves_retailer_ranking(payload):
    assert sainsburys.extract_product_ids(payload) == [
        "7317686",
        "7995709",
        "536",
        "3300787",
        "671682",
    ]


def test_sku_is_the_id_the_bulk_endpoint_accepts(payload):
    # product_uid, not sainId: only the former is valid in `uids=`, so a stock
    # refresh keyed on sainId would silently return nothing for every product.
    objects = sainsburys.extract_product_objects(payload)
    assert sainsburys.normalize_product(objects[0]).sku == "7317686"


def test_normalizes_price_availability_and_reviews(by_sku):
    beans = by_sku["536"]
    assert beans.retailer == "sainsburys"
    assert beans.name == "Sainsbury's Baked Beans In Tomato Sauce 400g"
    assert beans.price == 0.38
    assert (beans.unit_price, beans.unit_price_basis) == (0.95, "kg")
    assert beans.in_stock is True
    assert (beans.avg_rating, beans.ratings_count) == (3.5168, 238)
    assert beans.url.endswith("/sainsburys-baked-beans-in-tomato-sauce-400g")
    assert beans.brand == "Sainsbury's"


def test_price_is_the_promotional_one_not_the_struck_through_original(by_sku):
    # retail_price already reflects the promotion; promotions[].original_price is
    # the was-price. Pricing a basket at the original would overstate every
    # promoted line.
    chorizo = by_sku["7317686"]
    assert chorizo.price == 2.25


def test_unavailable_products_are_marked_not_dropped(by_sku):
    assert by_sku["3300787"].in_stock is False


def test_pack_size_comes_from_the_name(by_sku):
    # Sainsbury's states no pack size field at all.
    assert (by_sku["536"].pack_size_value, by_sku["536"].pack_size_unit) == (400.0, "g")


def test_multipack_in_the_name_is_multiplied_out(by_sku):
    # "4 x 415g" is 1660 g. Reading only the trailing "415g" would buy a quarter
    # of the demand and call the line covered.
    heinz = by_sku["3300787"]
    assert (heinz.pack_size_value, heinz.pack_size_unit) == (1660.0, "g")


def test_count_suffix_is_used_only_when_priced_by_the_each(by_sku):
    potatoes = by_sku["671682"]
    assert (potatoes.pack_size_value, potatoes.pack_size_unit) == (4.0, "each")


def test_count_suffix_is_ignored_when_a_weight_is_stated():
    # "Chorizo Slices x34 170g" is 170 g of chorizo, not 34 packs of it.
    product = sainsburys.normalize_product(
        {
            "product_uid": "7316225",
            "name": "Sainsbury's Spanish Chorizo Slices x34 170g",
            "retail_price": {"price": 1.4, "measure": ""},
            "unit_price": {"price": 8.24, "measure": "kg", "measure_amount": 1},
            "is_available": True,
        }
    )
    assert (product.pack_size_value, product.pack_size_unit) == (170.0, "g")


def test_shelf_life_is_read_out_of_the_display_labels(by_sku):
    chorizo = by_sku["7317686"]
    assert (chorizo.shelf_life_raw, chorizo.shelf_life_days) == ("14 DAY", 14)


def test_no_life_label_means_unknown_not_zero(by_sku):
    # The waste model reads NULL as "this shop does not say" and falls back to
    # the category; a 0 would price the leftover as a total loss.
    assert by_sku["536"].shelf_life_days is None


@pytest.mark.parametrize(
    "labels,expected",
    [
        ([{"text": "Typical life 3 months"}], ("3 MONTH", 90)),
        ([{"text": "Typical life 1 week"}], ("1 WEEK", 7)),
        ([{"text": "ALDI PRICE MATCH*"}], (None, None)),
        ([], (None, None)),
        (None, (None, None)),
    ],
)
def test_shelf_life_label_forms(labels, expected):
    assert sainsburys.parse_shelf_life(labels) == expected


def test_storage_label_leads_the_category_path(by_sku):
    # "Chilled" is the single most useful fact about how long it keeps, and
    # Sainsbury's states it apart from the aisle.
    assert by_sku["7317686"].category == "Fresh & Chilled Food > Continental meats"


def test_promotional_collections_are_kept_out_of_the_category(by_sku):
    # A product shelved under "Aldi Price Match" and "Food cupboard essentials"
    # as well as its real aisle must not be filed by the offer.
    assert by_sku["536"].category == "Pulses & beans > Baked Beans"


def test_rejects_a_payload_with_no_id_or_name():
    with pytest.raises(ValueError):
        sainsburys.normalize_product({"name": "No id"})
    with pytest.raises(ValueError):
        sainsburys.normalize_product({"product_uid": "1"})


def test_search_and_bulk_urls():
    assert "filter%5Bkeyword%5D=chorizo" in sainsburys.search_url("chorizo")
    assert sainsburys.products_url(["536", "671682"]).endswith("uids=536%2C671682")


def test_registry_resolves_both_adapters():
    assert set(ADAPTER_IDS) == {"ocado", "sainsburys"}
    assert get_adapter("sainsburys") is sainsburys
    assert has_adapter("sainsburys") and not has_adapter("waitrose")
    with pytest.raises(KeyError):
        get_adapter("waitrose")


def test_every_adapter_exposes_the_same_surface():
    # The pipeline is written once against this interface; a new adapter missing
    # one of these fails here rather than halfway through a scrape.
    for retailer in ADAPTER_IDS:
        adapter = get_adapter(retailer)
        for name in (
            "RETAILER",
            "BASE_URL",
            "BrowserClient",
            "PRODUCT_BATCH_SIZE",
            "extract_product_ids",
            "extract_product_objects",
            "normalize_product",
            "product_url",
            "search_url",
        ):
            assert hasattr(adapter, name), f"{retailer} adapter has no {name}"
