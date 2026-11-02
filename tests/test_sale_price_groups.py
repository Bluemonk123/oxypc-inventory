"""New Sale: one invoice, several price groups (per-tag prices) + Haryana default."""
import json
import types
from decimal import Decimal

from routers.sales import _parse_price_groups


def _groups(*gs):
    return json.dumps([{"barcodes": b, "price": p} for b, p in gs])


def test_groups_map_each_tag_to_its_group_price():
    out = _parse_price_groups(_groups(("A, B", "1000"), ("C", "2500.50"), ("D,E,F", "0")))
    assert out == {"A": Decimal("1000"), "B": Decimal("1000"), "C": Decimal("2500.50"),
                   "D": Decimal("0"), "E": Decimal("0"), "F": Decimal("0")}
    assert list(out) == ["A", "B", "C", "D", "E", "F"]          # tag order preserved


def test_same_tag_in_two_groups_is_rejected():
    msg = _parse_price_groups(_groups(("A,B", "10"), ("B,C", "20")))
    assert isinstance(msg, str) and "B" in msg and "more than one group" in msg


def test_bad_input_returns_error_text():
    assert isinstance(_parse_price_groups("not json"), str)
    assert isinstance(_parse_price_groups("[]"), str)
    assert "no tag" in _parse_price_groups(_groups((" , ", "5")))
    assert "invalid price" in _parse_price_groups(_groups(("A", "abc")))
    assert "invalid price" in _parse_price_groups(_groups(("A", "")))
    assert "negative" in _parse_price_groups(_groups(("A", "-1")))
    assert isinstance(_parse_price_groups('[1, 2]'), str)


def test_new_sale_page_defaults_state_to_haryana_and_has_group_ui(monkeypatch):
    from templates_config import templates
    monkeypatch.setitem(templates.env.globals, "master_options",
                        lambda cat: ["Delhi", "Haryana", "Other State"] if cat == "customer_state" else [])
    from models.user import UserRole
    tpl = templates.env.get_template("sales/new_tag_sale.html")
    user = types.SimpleNamespace(username="admin", role=UserRole.admin, full_name="Admin",
                                 is_active=True, id=1)
    html = tpl.render({
        "request": types.SimpleNamespace(url=types.SimpleNamespace(path="/sales/new"), cookies={}, query_params={}),
        "current_user": user, "device": None, "lot": None, "error": None,
        "next_sale_number": "SALE-0001", "today": "2026-10-07", "embed": False,
        "multi_count": 2,
        "prefill_barcode": "A,B", "prefill_qty": 2, "sales_person_options": [],
    })
    assert '<option value="Haryana" selected>' in html
    assert 'Delhi (default)' not in html
    assert 'name="price_groups"' in html
    assert 'id="tagTableBox"' in html and 'id="ttApply"' in html
    assert 'id="ttTarget"' not in html            # Apply to dropdown removed (2026-11-02)
    assert 'Invoice Date' in html and 'Invoice Quantity' in html and 'Total Invoice Price' in html
    assert 'addGroupBtn' not in html
    assert 'New Tag Sale' in html and 'name="sale_page" value="tag"' in html
    assert 'max="500"' not in html            # the 500-unit Quantity cap is gone
    assert '<th>Model</th>' not in html and 'Unit Price (&#8377;)</th>' in html   # renamed 2026-11-02
    # Model Based tab (2026-11-02): Category, Model Name, Quantity, Unit Price
    assert 'id="ttModelPane"' in html and 'id="tmBody"' in html and '<th>Model Name</th>' in html
    # Reset is an icon button (no "Clear tag prices" text); Upload Tags ticks rows from a file
    assert 'Clear tag prices</button>' not in html
    assert 'id="ttClear"' in html and 'bi-arrow-counterclockwise' in html
    assert 'id="ttUploadBtn"' in html and 'bi-upload' in html and 'id="ttUploadFile"' in html


def test_tag_info_endpoint_labels_tags_with_category_and_model():
    import asyncio
    from routers.sales import sale_new_tag_info
    from models.device import DeviceStage

    class _D:
        def __init__(self, bc, dt, sub, brand, model, stage):
            self.barcode, self.device_type, self.sub_category = bc, dt, sub
            self.brand, self.model, self.current_stage = brand, model, stage

    devs = [_D("A", "Laptop", None, "HP", "840", DeviceStage.ready_to_sale),
            _D("B", None, "TFT Monitor", "Dell", None, DeviceStage.sold),
            _D("C", None, None, None, None, DeviceStage.l1)]

    class _R:
        def scalars(self): return self
        def all(self): return devs

    class _DB:
        async def execute(self, *_a, **_k): return _R()

    resp = asyncio.run(sale_new_tag_info(barcodes="A, B ,C,ZZ,A", db=_DB(), current_user=None))
    tags = json.loads(resp.body)["tags"]
    assert [t["barcode"] for t in tags] == ["A", "B", "C", "ZZ"]          # de-duplicated, input order
    assert tags[0]["category"] == "Laptop" and tags[0]["model"] == "HP 840" and tags[0]["stage"] == "ready_to_sale"
    assert tags[1]["category"] == "TFT Monitor" and tags[1]["model"] == "Dell"
    assert tags[2]["category"] == "—"                               # no type/sub-category
    assert tags[3]["found"] is False


def test_original_new_sale_page_is_untouched():
    """The existing New Sale page keeps its look; only its Quantity cap was lifted (2026-10-07)."""
    from templates_config import templates
    src = open(templates.env.get_template("sales/new.html").filename, encoding="utf-8").read()
    assert "tagTableBox" not in src and "price_groups" not in src and "Haryana" not in src
    assert 'max="500"' not in src and "Delhi (default)" in src


def test_ready_to_sale_has_new_buttons_and_old_ones_remain():
    from templates_config import templates
    src = open(templates.env.get_template("sales/ready_list.html").filename, encoding="utf-8").read()
    # The As-Is lot Sell / New Sell buttons live on Ready for Sale - Lots
    # since 2026-10-28 (sales/ready_lots.html), not on the Tags page.
    lots = open(templates.env.get_template("sales/ready_lots.html").filename, encoding="utf-8").read()
    # new
    assert 'id="newBulkSaleBtn"' in src and "New Bulk Sale (" in src
    assert 'action="/sales/new-tag-sale/prefill"' in src
    assert "as-is-lot-newsell-btn" in lots and ">New Sell</button>" in lots
    assert 'action="/sales/new-tag-sale/prefill"' in lots
    # existing, untouched
    assert 'id="multiSellBtn"' in src and 'action="/sales/new/prefill"' in src
    assert "as-is-lot-sell-btn" in lots and ">Sell</button>" in lots and 'action="/sales/new/prefill"' in lots


def test_new_tag_sale_routes_registered_and_tag_info_is_post():
    import main
    routes = [(r.path, set(r.methods)) for r in main.app.routes if hasattr(r, "methods")]
    assert any(p == "/sales/new-tag-sale" and "GET" in m for p, m in routes)
    assert any(p == "/sales/new-tag-sale/prefill" and "POST" in m for p, m in routes)
    assert any(p == "/sales/new-tag-sale/tag-info" and "POST" in m for p, m in routes)
