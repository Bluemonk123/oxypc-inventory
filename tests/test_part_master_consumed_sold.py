"""Part Master's Consumed / Sold / In Stock sources, and the Edit Part layout.

Consumed and Sold both feed In Stock, so a wrong source here understates or
overstates availability on the page Stores buys from.
"""
import re

import pytest

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

EDIT_FIELDS = ["crate_number", "part_lot", "model", "category", "name", "make",
               "unit_price", "qty_in_stock", "min_stock_alert"]


def _first_edit_id(app_client):
    listing = app_client.get("/spare-parts", follow_redirects=True).text
    m = re.search(r'/spare-parts/([0-9a-f-]{36})/edit', listing)
    assert m, "no part on Part Master to edit"
    return m.group(1)


def test_consumed_counts_every_handover_not_just_handed_over_status():
    """A row that moves on to 'received' still shows its Qty Handover, so it
    must still count as Consumed — the status filter dropped it and inflated
    In Stock by the same amount."""
    # 2026-10-31: the netting lives in services/parts_stock (legacy mode).
    import inspect
    from services import parts_stock as ps

    consumed_block = inspect.getsource(ps.legacy_consumed_by_group)
    assert 'PartRequest.status == "handed_over"' not in consumed_block
    assert "PartRequest.qty_handed_over" in consumed_block


def test_sold_comes_from_approved_sale_requests():
    import inspect
    from services import parts_stock as ps

    sold_block = inspect.getsource(ps.sold_by_part)
    assert "PartSaleRequest.qty_requested" in sold_block
    assert '"approved"' in sold_block


def test_in_stock_deducts_both_consumed_and_sold():
    import io as _io
    html = _io.open("templates/spare_parts/list.html", encoding="utf-8").read()
    # 2026-10-31: In Stock = services/parts_stock.live_map (qty - consumed - sold
    # in legacy mode; crate row count - sold after the Parts Data Fix).
    assert "{% set live_stock = live_by_part.get(part.id|string, 0) %}" in html
    import inspect
    from services import parts_stock as ps
    src = inspect.getsource(ps.live_map)
    assert "cons.get" in src and "sold.get" in src
    # Sold must read the new map, not the stale column.
    assert "sold_by_part.get(part.id|string, 0)" in html
    assert "part.sold_qty" not in html


def test_part_master_renders(app_client, make_user):  # noqa: F811
    username, password = make_user("spare_parts_manager")
    _login(app_client, username, password)
    r = app_client.get("/spare-parts", follow_redirects=True)
    assert r.status_code == 200, r.text[:1500]
    for header in ("In Stock", "Consumed", "Sold"):
        assert header in r.text


def test_edit_part_shows_code_readonly_and_adds_crate(app_client, make_user):  # noqa: F811
    """Part Code is back in the form (grouped with Part Lot / Part Model per a
    later request) but stays disabled with no name attr, so it still can
    never be submitted or edited — only ever displayed."""
    username, password = make_user("spare_parts_manager")
    _login(app_client, username, password)
    html = app_client.get(f"/spare-parts/{_first_edit_id(app_client)}/edit",
                          follow_redirects=True).text

    # Pick the edit form by its action — base.html renders a logout form first.
    form = html.split('action="/spare-parts/', 1)[1].split("</form>", 1)[0]
    assert "Part Code" in form
    assert 'name="part_code"' not in form, "Part Code must stay non-submittable"
    assert 'name="crate_number"' in form
    assert "Crate Number" in form

    pos = [form.index(f'name="{f}"') for f in EDIT_FIELDS]
    assert pos == sorted(pos), (
        "Edit Part fields out of order; expected "
        "(Crate) (Code, Lot, Model) (Category, Name, Make) (Price, Stock, Alert)")
    assert form.index("Part Code") < form.index('name="part_lot"')


def test_tiles_use_one_live_definition_deducting_both():
    """Total Quantities, Out of Stock, the group rollups and QTY Available all
    read _live(), so none of them can disagree with the table's In Stock."""
    import inspect
    from routers import spare_parts as sp

    src = inspect.getsource(sp.parts_list)
    live = src.split("def _live(p):", 1)[1].split("\n\n", 1)[0]
    assert "live_by_part" in live          # services/parts_stock.live_map: consumed + sold
    # Out of Stock must not carry its own private subtraction any more.
    assert "out_of_stock_count = sum(1 for p in parts if _live(p) <= 0)" in src


def test_ready_to_sale_keeps_its_own_stock_rule():
    """Deliberate: Ready to Sale Parts must NOT switch to counting approved
    requests. `available` there gates sale creation, and an approval is
    unconsumed while the sale it authorises is being raised — deducting it
    would make an approval block its own sale."""
    import inspect
    from routers import part_sales as ps

    src = inspect.getsource(ps._stock_of)
    assert "sold_qty" in src
    assert "PartSaleRequest" not in src
