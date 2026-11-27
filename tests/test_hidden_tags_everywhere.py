"""ETP tags / lots, trashed and inactive tags hidden from every page and export
(2026-11-27, Pankaj) — utils/hidden_tags.py."""
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import utils.hidden_tags as ht
from models.device import Device
from models.lot import Lot


def test_which_requests_hide():
    hide = [("GET", "/devices"), ("GET", "/devices/data"), ("GET", "/devices/export"),
            ("POST", "/devices/export"), ("GET", "/lots"), ("GET", "/repair/l1"),
            ("GET", "/sales/ready/data"), ("GET", "/devices/api/quick-search"),
            ("GET", "/dashboard"), ("GET", "/reports/daily-stock")]
    show = [("GET", "/trade-partner/partners"), ("GET", "/partner/catalog"),
            ("GET", "/partner-payments"), ("GET", "/api/v1/devices"), ("GET", "/trash"),
            ("GET", "/admin/system/data-fixes"), ("GET", "/iqc/lookup"),
            ("GET", "/iqc/api/check/ABC"), ("GET", "/lots/api/exists"),
            ("GET", "/devices/4563100726L0787"), ("GET", "/devices/X1/edit"),
            ("GET", "/devices/api/detail-cards"), ("GET", "/lots/abc"),
            ("GET", "/lots/abc/next-tag"), ("POST", "/devices/bulk-move"),
            ("POST", "/iqc/register"), ("PATCH", "/devices/X1")]
    for m, p in hide:
        assert ht.hide_for_request(m, p), (m, p)
    for m, p in show:
        assert not ht.hide_for_request(m, p), (m, p)


def _sql(stmt):
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_live_tag_sql_only_in_hide_mode():
    assert ht.live_tag_sql("d") == "TRUE"
    with ht.hidden_tags_scope():
        s = ht.live_tag_sql("d")
        assert "External Partner Test" in s and "EPT-" in s
        assert "is_trashed IS NOT TRUE" in s and "is_active IS NOT FALSE" in s
        assert "is_trashed" not in ht.live_tag_sql("d", trash=False).replace("lot_number", "")
    assert ht.live_tag_sql("d") == "TRUE"


def test_criteria_added_to_orm_selects():

    class _State:
        is_select = True
        is_column_load = False
        is_relationship_load = False
        execution_options = {}
        statement = select(Device.id).join(Lot, Device.lot_id == Lot.id)

    st = _State()
    ht._hide_hidden_rows(st)                       # hide mode off → unchanged
    assert "External Partner Test" not in _sql(st.statement)
    with ht.hidden_tags_scope():
        ht._hide_hidden_rows(st)
    sql = _sql(st.statement)
    assert "devices.is_trashed IS NOT true" in sql and "devices.is_active IS NOT false" in sql
    assert "External Partner Test" in sql and "lots.lot_number" in sql and "EPT-" in sql
    assert "lots.is_trashed IS NOT true" in sql

    st2 = _State()
    st2.execution_options = {"include_hidden_tags": True}
    st2.statement = select(Device.id)
    with ht.hidden_tags_scope():
        ht._hide_hidden_rows(st2)
    assert "External Partner Test" not in _sql(st2.statement)
