import json

from app.ocado.product_details import description


def page(product):
    return '<script type="application/ld+json">' + json.dumps(product).replace("</", "<\\/") + '</script>'


def test_product_description_reads_only_the_matching_product_and_strips_html():
    html = page({"@type": "Product", "sku": "other", "description": "Wrong product"})
    html += page({"@graph": [{"@type": "Product", "sku": "123", "description": "<p>Creamy <b>butter</b> &amp; salt.</p><p>Keep chilled.</p><script>bad()</script>"}]})
    assert description(html, "123") == "Creamy butter & salt.\nKeep chilled."
    assert description(html, "missing") is None


def test_missing_and_invalid_structured_data_have_no_description():
    assert description('<script type="application/ld+json">bad json</script>', "123") is None
    assert description(page({"@type": "Product", "sku": "123", "description": None}), "123") is None


def test_ocado_browser_challenge_is_not_treated_as_an_empty_product_list():
    import httpx
    import pytest
    from app.ocado.client import OcadoClient

    class Challenged:
        def request(self, *args, **kwargs):
            return httpx.Response(202, headers={"x-amzn-waf-action": "challenge"},
                                  request=httpx.Request("GET", "https://www.ocado.com/products"))
    with pytest.raises(RuntimeError, match="browser check"):
        OcadoClient(Challenged()).search("milk")
