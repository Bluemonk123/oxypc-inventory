"""Parts GRN Detail page edits (2026-11-04): GRN details and part lines; stock
follows a part line's change (qty difference, or a move to another crate row)."""
import asyncio
import json
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def _q(sql, **kw):
    from database import AsyncSessionLocal
    from sqlalchemy import text

    async def go():
        async with AsyncSessionLocal() as s:
            r = await s.execute(text(sql), kw)
            try:
                rows = r.all()
            except Exception:
                rows = None
            await s.commit()
            return rows
    return asyncio.run(go())


def test_parts_grn_edit_header_and_item(app_client, make_user):  # noqa: F811
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    csrf = app_client.cookies.get("csrf_token", "")
    tag = uuid.uuid4().hex[:6].upper()
    name, crate, crate2 = f"ITEST PART {tag}", f"CR{tag}", f"CX{tag}"
    items = [{"part_name": name, "part_brand": "HP", "part_model": "M1", "crate_number": crate,
              "invoice_qty": 5, "physical_qty": 5, "price": "100"}]
    r = app_client.post("/parts-grn/new", data={
        "csrf_token": csrf, "vendor_name": "ITest Vendor", "invoice_number": f"INV{tag}",
        "line_items_json": json.dumps(items)}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    gid = r.headers["location"].rsplit("/", 1)[-1]
    try:
        page = app_client.get(f"/parts-grn/{gid}").text
        assert 'id="pgEditModal"' in page and "pg-item-edit" in page

        # GRN details
        r = app_client.post(f"/parts-grn/{gid}/edit", data={
            "csrf_token": csrf, "date_received": "2026-11-04", "vendor_name": "ITest Vendor 2",
            "vendor_state": "Delhi", "po_number": "PO1", "invoice_number": f"INV{tag}",
            "vehicle_number": "DL1", "invoice_value": "1000", "total_physical_qty": "5"}, follow_redirects=False)
        assert r.status_code == 303 and "success=" in r.headers["location"], r.headers.get("location")
        assert _q("select vendor_name, invoice_value from parts_grn where id = :i", i=gid)[0][0] == "ITest Vendor 2"
        # a required field missing is refused with the field named
        r = app_client.post(f"/parts-grn/{gid}/edit", data={"csrf_token": csrf, "vendor_name": "x"}, follow_redirects=False)
        assert "error=" in r.headers["location"] and "GRN+Date" in r.headers["location"]

        li_id, code = _q("select id, part_id from parts_grn_line_items where grn_id = :i", i=gid)[0]
        stock = lambda c: _q("select qty_in_stock from spare_parts where part_code = :c", c=c)[0][0]
        assert stock(code) == 5

        # same crate, qty 5 -> 8: +3 in stock
        base = {"csrf_token": csrf, "part_name": name, "part_brand": "HP", "part_model": "M1",
                "crate_number": crate, "invoice_qty": "8", "physical_qty": "8", "price": "100"}
        r = app_client.post(f"/parts-grn/{gid}/items/{li_id}/edit", data=base, follow_redirects=False)
        assert "success=" in r.headers["location"], r.headers["location"]
        assert stock(code) == 8

        # another crate: the 8 leave the old row, go into a new row
        r = app_client.post(f"/parts-grn/{gid}/items/{li_id}/edit", data={**base, "crate_number": crate2},
                            follow_redirects=False)
        assert "success=" in r.headers["location"], r.headers["location"]
        new_code = _q("select part_id from parts_grn_line_items where id = :i", i=li_id)[0][0]
        assert new_code != code and stock(code) == 0 and stock(new_code) == 8
    finally:
        codes = [c for (c,) in _q("select part_id from parts_grn_line_items where grn_id = :i", i=gid)]
        _q("delete from parts_grn_line_items where grn_id = :i", i=gid)
        _q("delete from parts_grn where id = :i", i=gid)
        _q("delete from spare_parts where name = :n", n=name)
