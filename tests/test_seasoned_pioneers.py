"""Seasoned Pioneers: snapshot parsing, catalogue sync, and name matching.

The committed snapshot is treated as a fixture in its own right — it is real
captured data and the thing the feature actually runs on — so a few tests assert
against it directly. Those are the ones that would catch a refresh that silently
dropped the pack sizes.
"""
from __future__ import annotations

import json

import pytest

from app.db.models import Product, ProductScrapeState, ProductSearchHit
from app.mapping import external
from app.scraper.products import catalogue
from app.scraper.products import seasoned_pioneers as sp
from tests.conftest import seed_candidates


def _payload(woo_id, name, price="350", *, categories=(("moroccan", "Moroccan Spices"),),
             type_="simple", stock=True, sku="", rating="4.5", reviews=12):
    return {
        "id": woo_id,
        "name": name,
        "sku": sku,
        "type": type_,
        "permalink": f"https://www.seasonedpioneers.com/x/{woo_id}/",
        "prices": {"price": price, "currency_minor_unit": 2, "currency_code": "GBP"},
        "categories": [{"slug": s, "name": n} for s, n in categories],
        "images": [{"src": "https://www.seasonedpioneers.com/img.jpeg", "name": "img"}],
        "average_rating": rating,
        "review_count": reviews,
        "is_in_stock": stock,
    }


def _snapshot(tmp_path, products):
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps({"products": products}), encoding="utf-8")
    return path


# --- adapter ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("35g", (35.0, "g")),
        ("1.5kg", (1500.0, "g")),
        ("250 ml", (250.0, "ml")),
        ("1 litre", (1000.0, "ml")),
        (None, (None, None)),
        ("a pinch", (None, None)),
    ],
)
def test_pack_size_parses_to_metric(raw, expected):
    assert sp.parse_pack_size(raw) == expected


def test_size_is_read_from_the_product_page_meta_block():
    html = '<div class="meta"> <span><strong>SIZE</strong> 42g</span> <span>CUISINE</span> </div>'
    assert sp.parse_size_from_html(html) == "42g"
    assert sp.parse_size_from_html("<div>no meta here</div>") is None


def test_price_converts_from_minor_units():
    entry = sp.CatalogueEntry(woo_id=1, payload=_payload(1, "Chermoula"), size_raw="35g")
    assert sp.normalize_product(entry).price == 3.50


def test_normalize_derives_unit_price_and_states_a_shelf_life():
    entry = sp.CatalogueEntry(woo_id=1, payload=_payload(1, "Chermoula"), size_raw="35g")
    product = sp.normalize_product(entry)

    assert (product.unit_price, product.unit_price_basis) == (100.0, "kg")
    # The store publishes no shelf life; dried spice keeping is stated, not inferred.
    assert product.shelf_life_days == sp.DEFAULT_SHELF_LIFE_DAYS
    assert product.shelf_life_raw is None


def test_sku_is_the_woo_id_not_the_stores_own_sku_field():
    """67 catalogue rows share an empty ``sku``; keying on it would collide."""
    a = sp.normalize_product(sp.CatalogueEntry(1, _payload(1, "Bundle A", sku=""), "35g"))
    b = sp.normalize_product(sp.CatalogueEntry(2, _payload(2, "Bundle B", sku=""), "35g"))

    assert (a.sku, b.sku) == ("sp:1", "sp:2")
    assert a.sku != b.sku


def test_html_entities_in_names_are_decoded():
    entry = sp.CatalogueEntry(9, _payload(9, "Cumin White, Roast &#038; Ground"), "34g")
    assert sp.normalize_product(entry).name == "Cumin White, Roast & Ground"


def test_unrated_products_report_no_rating_rather_than_zero_stars():
    entry = sp.CatalogueEntry(9, _payload(9, "New Spice", rating="0.00", reviews=0), "34g")
    product = sp.normalize_product(entry)

    assert product.avg_rating is None
    assert product.ratings_count is None


@pytest.mark.parametrize(
    "name,categories,size,saleable",
    [
        ("Chermoula Spice Mix", (("moroccan", "Moroccan"),), "35g", True),
        ("Zanzibar Spice Mix TIN", (("spice-tins", "Spice Tins"),), "60g", True),
        # No size: a bundle, whatever category it sits in.
        ("Hello Fresh Spices", (("seasoning-collections", "Collections"),), None, False),
        # Has a combined size, but is a gift box: category is what catches it.
        ("Gin & Tonic Garnish Box", (("spice-gifts-boxes", "Gifts"),), "155g", False),
    ],
)
def test_bundles_and_gifts_are_not_saleable_ingredients(name, categories, size, saleable):
    payload = _payload(1, name, categories=categories)
    assert sp.is_saleable_ingredient(payload, size) is saleable


def test_variable_products_are_excluded():
    """A gift card prices as a range; there is nothing for the planner to cost."""
    payload = _payload(1, "Online Gift Card", type_="variable")
    assert sp.is_saleable_ingredient(payload, "10g") is False


# --- snapshot --------------------------------------------------------------

def test_load_snapshot_rejects_a_file_it_cannot_use(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(ValueError, match="no catalogue snapshot"):
        sp.load_snapshot(missing)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        sp.load_snapshot(broken)

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"products": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="no products"):
        sp.load_snapshot(empty)


def test_write_snapshot_refuses_a_capture_that_lost_the_pack_sizes(tmp_path):
    """The snapshot is the only copy; a sizeless one makes nothing buyable."""
    target = tmp_path / "out.json"
    with pytest.raises(ValueError, match="size_raw"):
        sp.write_snapshot({"products": [_payload(1, "Chermoula")]}, target)
    assert not target.exists()


def test_write_snapshot_round_trips(tmp_path):
    target = tmp_path / "out.json"
    captured = {"products": [dict(_payload(2, "B"), size_raw="30g"),
                             dict(_payload(1, "A"), size_raw="35g")]}

    assert sp.write_snapshot(captured, target) == 2
    entries = sp.load_snapshot(target)
    assert [e.woo_id for e in entries] == [1, 2]  # sorted, for a readable diff
    assert entries[0].size_raw == "35g"


def test_committed_snapshot_is_usable():
    """Guards a refresh that lands a file the pipeline cannot actually consume."""
    entries = sp.load_snapshot()
    saleable = [e for e in entries if sp.is_saleable_ingredient(e.payload, e.size_raw)]

    assert len(entries) > 300
    assert len(saleable) > 250
    # Every saleable product must price and size, or the planner cannot cost it.
    for entry in saleable:
        product = sp.normalize_product(entry)
        assert product.price is not None, product.name
        assert product.pack_size_value, product.name
        assert product.pack_size_unit in ("g", "ml"), product.name


def test_committed_snapshot_has_the_hellofresh_blends():
    """The whole reason for this source: blends Ocado has no equivalent for."""
    names = {sp.normalize_product(e).name.lower() for e in sp.load_snapshot()
             if sp.is_saleable_ingredient(e.payload, e.size_raw)}
    for blend in ("chermoula spice mix", "central american spice mix", "ras el hanout"):
        assert blend in names


# --- catalogue sync --------------------------------------------------------

def test_sync_loads_saleable_products_and_sets_the_rest_aside(factory, tmp_path):
    path = _snapshot(tmp_path, [
        dict(_payload(1, "Chermoula Spice Mix"), size_raw="35g"),
        dict(_payload(2, "Hello Fresh Spices",
                      categories=(("seasoning-collections", "Collections"),)), size_raw=None),
    ])

    result = catalogue.sync(factory, path=path, cache_raw=False)

    assert (result.normalized, result.skipped, result.errors) == (1, 1, 0)
    with factory() as s:
        products = s.query(Product).all()
        assert [(p.retailer, p.sku, p.pack_size_value) for p in products] == [
            (sp.RETAILER, "sp:1", 35.0)
        ]
        states = {st.key: st.status for st in s.query(ProductScrapeState).all()}
        assert states == {"sp:1": "normalized", "sp:2": "skipped"}


def test_sync_is_idempotent_and_moves_prices_on_a_refresh(factory, tmp_path):
    first = _snapshot(tmp_path, [dict(_payload(1, "Chermoula Spice Mix"), size_raw="35g")])
    catalogue.sync(factory, path=first, cache_raw=False)
    catalogue.sync(factory, path=first, cache_raw=False)

    with factory() as s:
        assert s.query(Product).count() == 1
        assert s.query(Product).one().price == 3.50

    dearer = tmp_path / "dearer.json"
    dearer.write_text(
        json.dumps({"products": [dict(_payload(1, "Chermoula Spice Mix", price="399"),
                                      size_raw="35g")]}),
        encoding="utf-8",
    )
    catalogue.sync(factory, path=dearer, cache_raw=False)

    with factory() as s:
        # Upserted in place: an accepted mapping pointing at sp:1 still resolves.
        assert s.query(Product).count() == 1
        assert s.query(Product).one().price == 3.99


def test_sync_records_a_bad_row_without_abandoning_the_rest(factory, tmp_path):
    path = _snapshot(tmp_path, [
        dict(_payload(1, "Chermoula Spice Mix"), size_raw="35g"),
        dict(_payload(2, ""), size_raw="30g"),  # no name
        dict(_payload(3, "Ras el Hanout"), size_raw="33g"),
    ])

    result = catalogue.sync(factory, path=path, cache_raw=False)

    assert result.errors == 1
    assert result.normalized == 2
    with factory() as s:
        bad = s.query(ProductScrapeState).filter_by(key="sp:2").one()
        assert bad.status == "error"
        assert "no name" in bad.error_message


# --- name matching ---------------------------------------------------------

@pytest.mark.parametrize(
    "ingredient,product",
    [
        # Packaging words differ; the ingredient does not.
        ("Cajun Spice Mix", "Cajun Seasoning Spice Blend"),
        ("Central American Style Spice Mix", "Central American Spice Mix"),
        ("Caribbean Style Jerk", "Caribbean Jerk Seasoning Spice Rub"),
        # Spelling variants, without a hand-maintained table.
        ("Za'atar", "Zahtar Spice Blend"),
        # Preparation synonyms.
        ("Ground Turmeric", "Turmeric Powder"),
        ("Chilli Flakes", "Red Chillies Crushed"),
        # Every herb in the catalogue is dried; saying so distinguishes nothing.
        ("Dried Oregano", "Oregano Leaves, Wild-Grown"),
        # The alias is in the parentheses, not outside them.
        ("Smoked Paprika", "Pimenton Dulce, Smoked (Smoked Paprika)"),
        # ...and here it is outside them.
        ("North Indian Style Spice Mix", "North Indian Style Spice Mix (Curry Powder)"),
    ],
)
def test_similar_names_match(ingredient, product):
    assert external.similarity(ingredient, product) >= external.MIN_SCORE


@pytest.mark.parametrize(
    "ingredient,product",
    [
        ("Chilli Flakes", "Chilli con Carne Spices"),
        ("Peas", "Rose Petals"),
        ("Garlic Clove", "Garlic Salt Roasted"),
        ("Chermoula Spice Mix", "Ras el Hanout"),
    ],
)
def test_unrelated_names_do_not_match(ingredient, product):
    assert external.similarity(ingredient, product) < external.MIN_SCORE


def test_a_prepared_mix_outranks_a_raw_spice_of_the_same_score(factory, tmp_path):
    """Both share exactly one token with "Mexican Style Spice Mix"; kind breaks the tie."""
    path = _snapshot(tmp_path, [
        dict(_payload(1, "Oregano Mexican, Leaves", price="195"), size_raw="11g"),
        dict(_payload(2, "Mexican Adobo Spice Rub", price="350"), size_raw="28g"),
    ])
    catalogue.sync(factory, path=path, cache_raw=False)

    with factory() as s:
        matches = external.match_products(s, "Mexican Style Spice Mix")

    assert [m.product_name for m in matches][0] == "Mexican Adobo Spice Rub"


def test_cheaper_pack_wins_an_otherwise_equal_match(factory, tmp_path):
    path = _snapshot(tmp_path, [
        dict(_payload(1, "Chermoula Spice Mix", price="399"), size_raw="35g"),
        dict(_payload(2, "Chermoula Spice Blend", price="350"), size_raw="35g"),
    ])
    catalogue.sync(factory, path=path, cache_raw=False)

    with factory() as s:
        matches = external.match_products(s, "Chermoula Spice Mix")

    assert [m.price for m in matches] == [3.50, 3.99]


@pytest.mark.parametrize(
    "native,expected",
    [
        ("1 sachet(s) (244) | 1 pot(s) (48)", True),
        ("1 pinch (422) | 1 sachet(s) (72)", True),
        ("0.5 unit(s) (316) | 1 unit(s) (190)", False),
        ("1 bunch(es) (1171)", False),
        (None, False),
    ],
)
def test_seasoning_guard_reads_how_the_ingredient_arrives(native, expected):
    assert external.arrives_as_seasoning(native) is expected


# --- attaching to the review queue ----------------------------------------

def test_attach_files_hits_under_the_host_retailer(factory, tmp_path):
    """Products stay Seasoned Pioneers; the hit joins the Ocado review queue."""
    path = _snapshot(tmp_path, [dict(_payload(1, "Chermoula Spice Mix"), size_raw="35g")])
    catalogue.sync(factory, path=path, cache_raw=False)

    with factory() as s:
        external.attach_matches(s, "name:chermoula spice mix", name="Chermoula Spice Mix")
        s.commit()

    with factory() as s:
        hit = s.query(ProductSearchHit).one()
        assert hit.retailer == external.HOST_RETAILER == "ocado"
        assert hit.sku == "sp:1"
        assert s.get(Product, hit.product_id).retailer == sp.RETAILER


def test_attach_keeps_existing_candidates_and_ranks_after_them(factory, tmp_path):
    path = _snapshot(tmp_path, [dict(_payload(1, "Chermoula Spice Mix"), size_raw="35g")])
    catalogue.sync(factory, path=path, cache_raw=False)
    with factory() as s:
        seed_candidates(s, "name:chermoula spice mix", "Chermoula Spice Mix",
                        [{"sku": "oc1", "name": "Some Ocado Spice"}])

    with factory() as s:
        external.attach_matches(s, "name:chermoula spice mix", name="Chermoula Spice Mix")
        s.commit()

    with factory() as s:
        hits = s.query(ProductSearchHit).order_by(ProductSearchHit.result_rank).all()
        assert [h.sku for h in hits] == ["oc1", "sp:1"]


def test_attach_is_idempotent(factory, tmp_path):
    path = _snapshot(tmp_path, [dict(_payload(1, "Chermoula Spice Mix"), size_raw="35g")])
    catalogue.sync(factory, path=path, cache_raw=False)

    for _ in range(3):
        with factory() as s:
            external.attach_matches(s, "name:chermoula spice mix", name="Chermoula Spice Mix")
            s.commit()

    with factory() as s:
        assert s.query(ProductSearchHit).count() == 1


def test_attach_adds_nothing_when_nothing_scores(factory, tmp_path):
    path = _snapshot(tmp_path, [dict(_payload(1, "Chermoula Spice Mix"), size_raw="35g")])
    catalogue.sync(factory, path=path, cache_raw=False)

    with factory() as s:
        assert external.attach_matches(s, "name:potatoes", name="Potatoes") == []
        s.commit()

    with factory() as s:
        assert s.query(ProductSearchHit).count() == 0


def test_resolve_name_does_not_score_against_the_raw_key(factory, tmp_path):
    """An ingredient with no candidates has no display name to borrow."""
    path = _snapshot(tmp_path, [dict(_payload(1, "Chermoula Spice Mix"), size_raw="35g")])
    catalogue.sync(factory, path=path, cache_raw=False)

    with factory() as s:
        # No mapping row and no hits: the key's "name:" prefix must not leak into
        # the score, or an exact match reads as 0.67.
        assert external.resolve_name(s, "name:chermoula spice mix") == "chermoula spice mix"
        matches = external.match_products(
            s, external.resolve_name(s, "name:chermoula spice mix")
        )
    assert matches[0].score == 1.0


# --- reaching the planner --------------------------------------------------

def test_accepted_catalogue_product_reaches_the_planner_as_an_external_pack(
    factory, tmp_path
):
    """The whole point: costed by the planner, but off the Ocado order."""
    from app.db.models import IngredientMapping, IngredientMappingProduct
    from app.planner import index as planner_index

    key = "name:chermoula spice mix"
    path = _snapshot(tmp_path, [dict(_payload(1, "Chermoula Spice Mix"), size_raw="35g")])
    catalogue.sync(factory, path=path, cache_raw=False)

    with factory() as s:
        mapping = IngredientMapping(
            retailer="ocado", ingredient_key=key, name="Chermoula Spice Mix",
            line_count=318, status="approved", unit_kind="mass",
        )
        s.add(mapping)
        s.flush()
        external.attach_matches(s, key, name="Chermoula Spice Mix")
        product = s.query(Product).filter_by(sku="sp:1").one()
        s.add(IngredientMappingProduct(
            mapping_id=mapping.id, product_id=product.id, sku="sp:1",
            rank=1, match_type="exact", accepted=1, source="human",
        ))
        s.commit()

    freq = tmp_path / "freq.csv"
    freq.write_text(
        "rank,ingredient_key,source_ingredient_ids,name,line_count,metric_unit,"
        "median_metric_amount,p25_metric_amount,p75_metric_amount,"
        "common_native_amounts,name_variants\n"
        f"1,{key},sid1,Chermoula Spice Mix,318,g,8,8,8,1 sachet(s) (244),\n",
        encoding="utf-8",
    )
    index = planner_index.load_index(factory, csv_path=freq)
    pack = index.ingredients[key].packs[0]

    assert pack.retailer == sp.RETAILER
    assert pack.external is True
    assert (pack.capacity_g, pack.price) == (35.0, 3.50)
    # The stated 365-day life is what earns a near-full salvage on leftovers.
    assert pack.salvage > 0.5
