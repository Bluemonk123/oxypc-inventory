"""Spare parts crate rows (2026-10-31).

A Part Master row = Part Name + Make + Model + New/Harvest + Crate.
- Parts GRN adds the Physical Qty to the matching New crate row (Matched) or
  creates one (Not-Matched); its price goes to every row of the part.
- Add Harvest adds to the Harvest crate row; price follows the latest GRN.
- Crate Matched / Not-Matched answer for the crate fields.
- Update Count sets a crate row's count (audited).
- Row stock mode: handover takes qty off the chosen crate row only, and is
  refused without a crate row.
"""
import json
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def _sql(stmt):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        r = await db.execute(text({stmt!r}))
        try:
            print("\\n".join("|".join("" if v is None else str(v) for v in row) for row in r.all()))
        except Exception:
            pass
        await db.commit()
asyncio.run(main())
""")


def test_crate_rows_grn_harvest_count_handover(app_client, make_user):  # noqa: F811
    name = "ITCRATE" + uuid.uuid4().hex[:6].upper()
    bc = "ITCR" + uuid.uuid4().hex[:8].upper()
    username, password = make_user("admin")
    _login(app_client, username, password)
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    H = {"X-CSRF-Token": csrf}
    try:
        items = [{"part_name": name, "part_brand": "Dell", "part_model": "E5400", "crate_number": "C-1",
                  "physical_qty": "5", "price": "100"},
                 {"part_name": name, "part_brand": "Dell", "part_model": "E5400", "crate_number": "c-1 ",
                  "physical_qty": "3", "price": "120"},
                 {"part_name": name, "part_brand": "Dell", "part_model": "E5400", "crate_number": "C-2",
                  "physical_qty": "2"}]
        r = app_client.post("/parts-grn/new", data={"csrf_token": csrf, "vendor_name": "T",
                                                     "line_items_json": json.dumps(items)},
                            headers=H, follow_redirects=False)
        assert r.status_code == 303, r.text[:500]
        rows = _sql(f"select crate_number, source, qty_in_stock, unit_price from spare_parts "
                    f"where name = '{name}' and is_trashed is not true order by crate_number")
        assert rows.splitlines() == ["C-1|new|8|120.00", "C-2|new|2|120.00"], rows

        m = app_client.get("/spare-parts/crate-match", params=dict(name=name, make="dell", model="e5400",
                                                                     source="new", crate="c-1")).json()
        assert m["matched"] and m["qty"] == 8
        assert not app_client.get("/spare-parts/crate-match", params=dict(
            name=name, make="Dell", model="E5400", source="harvest", crate="C-1")).json()["matched"]

        for q in ("4", "1"):
            j = app_client.post("/parts-grn/harvest", data={"csrf_token": csrf, "part_name": name, "part_brand": "Dell",
                                                             "part_model": "E5400", "crate_number": "C-1",
                                                             "physical_qty": q, "price": "5"}, headers=H).json()
            assert j["ok"]
        hv = _sql(f"select qty_in_stock, unit_price from spare_parts where name = '{name}' "
                  f"and source = 'harvest' and is_trashed is not true")
        assert hv == "5|120.00", hv                      # one row, price = latest GRN

        rid = _sql(f"select id from spare_parts where name = '{name}' and crate_number = 'C-1' and source = 'new'")
        j = app_client.post(f"/spare-parts/{rid}/update-count", data={"csrf_token": csrf, "count": "6",
                                                                       "reason": "test"}, headers=H).json()
        assert j == {"ok": True, "old": 8, "new": 6}, j

        # Row stock mode: handover comes off the chosen crate row.
        _sql("insert into app_settings (key, value) values ('parts_stock_mode', 'row') "
             "on conflict (key) do update set value = 'row'")
        _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.part_request import PartRequest
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        d = Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot, current_stage=DeviceStage.l1,
                   device_type="Laptop", brand="Dell", model="E5400")
        db.add(d); await db.flush()
        for _ in range(2):
            db.add(PartRequest(device_id=d.id, barcode="{bc}", stage="l1", part_name="{name}",
                               request_type="new", status="requested", qty_requested=2, requested_by="t"))
        await db.commit()
asyncio.run(main())
""")
        reqs = _sql(f"select id from part_requests where barcode = '{bc}' order by id").splitlines()
        r = app_client.post(f"/part-requests/{reqs[0]}/handover", data={"csrf_token": csrf}, headers=H,
                            follow_redirects=False)
        assert "error=" in r.headers["location"]            # no crate row -> refused
        r = app_client.post("/spare-parts/requests/bulk-action",
                            data={"csrf_token": csrf, "action": "handover", "ids": reqs,
                                  "part_row_ids": [rid, rid]}, headers=H)
        assert r.json()["processed"] == 2, r.text
        assert _sql(f"select qty_in_stock from spare_parts where id = '{rid}'") == "2"
        assert _sql(f"select count(*) from part_requests where barcode = '{bc}' and part_id = '{rid}' "
                    f"and status = 'handed_over'") == "2"
        opts = app_client.get("/part-requests/handover-options", params={"ids": reqs[0]}).json()["requests"][0]
        assert {(x["crate"], x["source"], x["qty"]) for x in opts["rows"]} >= {("C-1", "new", 2), ("C-1", "harvest", 5)}
    finally:
        _sql("delete from app_settings where key = 'parts_stock_mode'")
        _sql(f"delete from part_requests where barcode = '{bc}'")
        _sql(f"delete from devices where barcode = '{bc}'")
        _sql(f"update spare_parts set is_trashed = true where name = '{name}'")
