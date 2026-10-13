"""All Inventory: multi-select filters, the Lot Based Summary filter fix, and
the removal of the Movement table.
"""
import pytest

from routers.devices import _multi, _device_search_filters
from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


@pytest.mark.parametrize("raw,expected", [
    ("", []), (None, []), ("a", ["a"]), ("a,b", ["a", "b"]),
    (" a , b ,, c ", ["a", "b", "c"]), (["a", "b"], ["a", "b"]),
])
def test_multi_splits_filter_values(raw, expected):
    assert _multi(raw) == expected


def test_single_value_urls_still_work():
    """People have pasted and bookmarked ?stage=iqc — one value must behave
    exactly as it did before multi-select."""
    one = _device_search_filters("", "iqc", "", "", "", "", "", "")
    assert len(one) == 1


def test_multiple_stages_produce_one_in_clause():
    many = _device_search_filters("", "iqc,stock_in,sold", "", "", "", "", "", "")
    assert len(many) == 1
    assert "IN" in str(many[0].compile(compile_kwargs={"literal_binds": True})).upper()


def test_unknown_stage_value_is_ignored_not_fatal():
    """A stale bookmark naming a stage that no longer exists must not 500."""
    assert _device_search_filters("", "not_a_stage", "", "", "", "", "", "") == []
    mixed = _device_search_filters("", "iqc,not_a_stage", "", "", "", "", "", "")
    assert len(mixed) == 1


def test_exclude_sold_respects_a_multi_selection_containing_sold():
    """Asking for Sold among several stages must not then exclude Sold."""
    f = _device_search_filters("", "iqc,sold", "", "", "", "", "", "", exclude_sold=True)
    assert len(f) == 1          # only the stage IN clause, no != sold


def test_lot_summary_takes_the_page_filters():
    import inspect
    from routers import devices

    assert "filters" in inspect.signature(devices._build_lot_summary).parameters
    src = inspect.getsource(devices.device_search)
    assert "_build_lot_summary(db, filters)" in src


def test_page_renders_with_multiselect_and_no_movement(app_client, make_user):  # noqa: F811
    username, password = make_user("inventory_manager")
    _login(app_client, username, password)
    html = app_client.get("/devices", follow_redirects=True).text

    for name in ("stage", "lot", "grade", "device_type", "stock_type", "entity"):
        assert f'id="ms_val_{name}"' in html, f"{name} is not a multi-select"
    assert "ms-opt" in html
    # Movement table is gone, loader and all.
    assert "devicesMovementTable" not in html
    assert "devicesMovementTbody" not in html
    assert "employeeFilterSelect" not in html


def test_filtering_by_two_stages_returns_200(app_client, make_user):  # noqa: F811
    username, password = make_user("inventory_manager")
    _login(app_client, username, password)
    r = app_client.get("/devices?stage=iqc,stock_in&fs=1", follow_redirects=True)
    assert r.status_code == 200, r.text[:1500]
