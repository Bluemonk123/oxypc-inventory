"""Part Master consolidated filter bar + Add Harvest Part modal reorder.

The per-tab (date range / category / part name) filters were consolidated
into one global bar above the summary tiles; each tab now shows only its own
"Added As" checkboxes. Add Harvest Part's fields were regrouped into three
explicit rows.
"""
import pytest

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

HARVEST_MODAL_FIELDS = ["hv_part_id", "hv_lot_number", "part_model",
                         "category", "part_name", "part_brand",
                         "price", "physical_qty", "min_stock_alert"]


def _part_master_html(app_client, make_user):
    username, password = make_user("spare_parts_manager")
    _login(app_client, username, password)
    return app_client.get("/spare-parts", follow_redirects=True).text


def test_global_filter_bar_present_once_above_the_tiles(app_client, make_user):  # noqa: F811
    html = _part_master_html(app_client, make_user)
    assert 'id="globalFilterBar"' in html
    assert html.index('id="globalFilterBar"') < html.index('id="partsCards"'), (
        "global filter bar must render above the summary tiles")
    # The bar ends before the summary tiles (was the old tile row's closing
    # '</div>\n</div>'; tiles are tl.double macros since 2026-11-06).
    bar = html.split('id="globalFilterBar"', 1)[1].split('</form>', 1)[0]
    assert "Added From" in bar
    assert "Added To" in bar
    assert "Category" in bar
    assert "Part Name" in bar
    assert "Added As" not in bar, "Added As stays per-tab, not in the global bar"


def test_per_tab_bars_only_have_added_as_checkboxes(app_client, make_user):  # noqa: F811
    html = _part_master_html(app_client, make_user)
    for table_id in ("partsTable", "partReqTable", "faultyReqTable"):
        bar = html.split(f'data-filterbar="{table_id}"', 1)[1].split("</div>\n", 1)[0]
        assert "Added As" in bar
        assert 'type="checkbox"' in bar
        assert "Added From" not in bar
        assert "Category" not in bar
        assert "Part Name" not in bar


def test_filter_categories_and_part_names_context_vars_are_gone():
    """The per-tab dropdowns they used to feed no longer exist — dead weight
    left in the route context would just be confusing."""
    import inspect
    from routers import spare_parts as sp

    src = inspect.getsource(sp.parts_list)
    assert "filter_categories" not in src
    assert "filter_part_names" not in src


def test_harvest_modal_fields_are_in_three_rows(app_client, make_user):  # noqa: F811
    html = _part_master_html(app_client, make_user)
    modal = html.split('id="harvestModal"', 1)[1].split("</form>", 1)[0]
    pos = [modal.index(f'"{f}"') if f.startswith("hv_") else modal.index(f'name="{f}"')
           for f in HARVEST_MODAL_FIELDS]
    assert pos == sorted(pos), (
        "Add Harvest Part fields out of order; expected "
        "(Part ID, Lot Number, Part Model) (Category, Name, Brand) (Price, Qty, Alert)")
