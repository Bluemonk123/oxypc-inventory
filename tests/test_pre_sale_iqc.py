"""Pre-Sale IQC (2026-10-29): the Ready for Sale Tags page with Start / Mark
Complete / Request Part / Cancel and a Parts Requested column. Start opens a
PSQC- WorkID (stage "psiqc") that WorkID Status shows as "Pre-Sale IQC";
Mark Complete follows L1/L2 (PNA blocks; handed-over parts verified,
never-handed-over cancelled); Cancel undoes Start and cancels requests not yet
handed over. The module is OFF by default for every role."""
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


def _wo(bc):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(WorkOrder).where(WorkOrder.barcode == "{bc}", WorkOrder.stage == "psiqc")
                .order_by(WorkOrder.created_at))).scalars().all()
        print(";".join(f"{{w.work_id}}|{{w.status}}" for w in rows))
asyncio.run(main())
""")


def _prs(bc):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.part_request import PartRequest
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(PartRequest).where(PartRequest.barcode == "{bc}")
                .order_by(PartRequest.part_name))).scalars().all()
        print(";".join(f"{{p.part_name}}|{{p.status}}" for p in rows))
asyncio.run(main())
""")


def _add_requests(bc, items):
    """items: [(part_name, status, qty_handed_over)] on the tag's open PSQC WorkID."""
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.part_request import PartRequest
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        wo = (await db.execute(select(WorkOrder).where(WorkOrder.barcode == "{bc}", WorkOrder.stage == "psiqc",
              WorkOrder.status == "in_progress"))).scalars().first()
        for name, st, ho in {items!r}:
            db.add(PartRequest(work_order_id=wo.id, work_id=wo.work_id, device_id=wo.device_id, barcode="{bc}",
                               stage="psiqc", part_name=name, request_type="replace", status=st,
                               qty_requested=1, qty_handed_over=ho, requested_by="t", engineer_name="IT Eng"))
        await db.commit()
asyncio.run(main())
""")


def test_pre_sale_iqc_flow(app_client, make_user):  # noqa: F811
    bc = "ITPSQ" + uuid.uuid4().hex[:8].upper()
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        db.add(Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot, current_stage=DeviceStage.ready_to_sale,
                      device_type="Laptop", brand="Dell", model="Latitude 7490"))
        await db.commit()
asyncio.run(main())
""")
    try:
        # OFF by default: a non-admin role gets no page and no sidebar link
        u, pw = make_user("inventory_manager")
        _login(app_client, u, pw)
        assert app_client.get("/pre-sale-iqc").status_code == 403
        assert 'href="/pre-sale-iqc"' not in app_client.get("/sales/ready").text

        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        html = app_client.get("/pre-sale-iqc").text
        assert "Pre-Sale IQC" in html and "Parts Requested</th>" in html and "/pre-sale-iqc/data" in html
        assert 'href="/pre-sale-iqc"' in html and 'id="psiqcPartsModal"' in html

        def feed(**extra):
            return app_client.get("/pre-sale-iqc/data", params={"search[value]": bc, "length": 5, **extra}).json()

        def row():
            d = feed()
            assert d["recordsFiltered"] == 1, d
            return d["data"][0]

        def post(path):
            r = app_client.post(path, data={"csrf_token": csrf, "barcode": bc}, headers={"X-CSRF-Token": csrf})
            assert r.status_code in (200, 400), r.text[:300]
            return r.json()

        # Queue (2026-10-29): only tags assigned from Ready for Sale show here
        assert feed()["recordsFiltered"] == 0
        ready = app_client.get("/sales/ready").text
        assert 'id="assignPsiqcBtn"' in ready and 'id="bulkSetPriceBtn" class="d-none" hidden' in ready
        assert "tag-set-price-btn" not in app_client.get("/sales/ready/data", params={"search[value]": bc}).text
        r = app_client.post("/pre-sale-iqc/assign", data={"csrf_token": csrf, "barcodes": f"{bc},NOPE{bc}"},
                            headers={"X-CSRF-Token": csrf}).json()
        assert r["ok"] and r["assigned"] == 1 and r["not_found"] == 1
        r = app_client.post("/pre-sale-iqc/assign", data={"csrf_token": csrf, "barcodes": bc},
                            headers={"X-CSRF-Token": csrf}).json()
        assert r["assigned"] == 0 and r["already"] == 1                    # already queued
        assert _wo(bc).endswith("|pending")
        d = feed()
        assert d["psiqc_tiles"] == {"assigned": 1, "completed": 0, "requested": 0, "verified": 0}
        r = row()
        assert "psiqc-start-btn" in r[-1] and r[-2].endswith("—</span>")
        assert post("/pre-sale-iqc/start")["ok"]
        assert post("/pre-sale-iqc/start")["ok"]                          # double click: still one WorkID
        wos = _wo(bc).split(";")
        assert len(wos) == 1 and wos[0].startswith("PSQC-") and wos[0].endswith("|in_progress")
        r = row()
        assert "psiqc-complete-btn" in r[-1] and "psiqc-cancel-btn" in r[-1] and f'/devices/{bc}#parts' in r[-1]

        # Parts Requested: count + Pending, listed in the modal feed
        _add_requests(bc, [("RAM", "requested", 0), ("Battery", "handed_over", 1)])
        r = row()
        assert 'psiqc-parts-btn' in r[-2] and ">2</button>" in r[-2] and "Pending" in r[-2]
        parts = app_client.get("/pre-sale-iqc/parts", params={"barcode": bc}).json()["parts"]
        assert sorted(p["part_name"] for p in parts) == ["Battery", "RAM"]
        assert feed()["psiqc_tiles"]["requested"] == 1
        assert feed(tile="requested")["recordsFiltered"] == 1 and feed(tile="verified")["recordsFiltered"] == 0
        # Parts Manager (2026-10-30): PSQC- requests sit in their own
        # "Pre-Sale IQC Requests" tab, not in Part Requests
        sp = app_client.get("/spare-parts").text
        assert 'id="psiqcReqTab"' in sp
        psq = sp.split('id="psiqcReqTableData">', 1)[1].split("</script>", 1)[0]
        prq = sp.split('id="partReqTableData">', 1)[1].split("</script>", 1)[0]
        assert bc in psq and bc not in prq

        # WorkID Status shows it as stage Pre-Sale IQC (and the Stage filter finds it)
        ws = app_client.get("/workid-status", params={"tag": bc, "work_from": "", "work_to": ""}).text
        rows = json.loads(ws.split('id="workidRows">', 1)[1].split("</script>", 1)[0])
        assert [x["s"] for x in rows] == ["Pre-Sale IQC"]
        ws = app_client.get("/workid-status", params={"tag": bc, "work_from": "", "work_to": "",
                                                     "cosmetic_stage": "pre_sale_iqc"}).text
        assert len(json.loads(ws.split('id="workidRows">', 1)[1].split("</script>", 1)[0])) == 1

        # Cancel: back to the queue (Start); never-handed-over request cancelled,
        # handed-over one kept
        assert post("/pre-sale-iqc/cancel")["ok"]
        assert _wo(bc).endswith("|pending")
        assert _prs(bc) == "Battery|handed_over;RAM|cancelled"
        r = row()     # only a handed-over part left: the middle state
        assert ">1</button>" in r[-2] and "Handed Over" in r[-2] and "Pending" not in r[-2]
        assert "psiqc-start-btn" in row()[-1]

        # Start again + Mark Complete: handed-over part verified, WorkID completed
        assert post("/pre-sale-iqc/start")["ok"]
        _add_requests(bc, [("Screen", "handed_over", 1)])
        pre = app_client.get("/pre-sale-iqc/complete-precheck", params={"barcode": bc}).json()
        assert {p["part_name"] for p in pre["to_verify"]} == {"Battery", "Screen"} and pre["pna"] == []
        d = post("/pre-sale-iqc/complete")
        assert d["ok"] and d["verified"] == 2
        assert _wo(bc).split(";")[-1].endswith("|completed")
        assert _prs(bc) == "Battery|received;RAM|cancelled;Screen|received"
        # Part colours (2026-10-29): the Pre-Sale IQC Mark Complete counts as the
        # "pass" — the parts it verified turn green (passed), not orange.
        st = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from services.parts_changed import changed_parts_by_device
async def main():
    async with AsyncSessionLocal() as db:
        did = (await db.execute(select(Device.id).where(Device.barcode == "{bc}"))).scalar()
        items = (await changed_parts_by_device(db, [did]))[str(did)]["items"]
        print(";".join(sorted(f"{{i['part_name']}}|{{i['state']}}|{{i['pass_kind']}}" for i in items)))
asyncio.run(main())
""")
        assert st == "Battery|passed|Pre-Sale IQC;Screen|passed|Pre-Sale IQC", st
        r = row()
        assert "Completed" in r[-1] and ">2</button>" in r[-2] and "Verified" in r[-2]
        # Tabs (2026-10-29): Assigned = not completed, Completed = completed
        # part tiles count the Assigned tab only, so a completed tag is in neither
        assert feed()["psiqc_tiles"] == {"assigned": 0, "completed": 1, "requested": 0, "verified": 0}
        assert feed(view="completed")["recordsFiltered"] == 1 and feed(view="assigned")["recordsFiltered"] == 0
        sel = app_client.get("/sales/ready/barcodes", params={"psiqc": "1", "view": "completed"}).json()["barcodes"]
        assert bc in sel
        # Assigned again from Ready for Sale after completion: a fresh start —
        # new WorkID, back in the Assigned tab, no old parts in the table
        r = app_client.post("/pre-sale-iqc/assign", data={"csrf_token": csrf, "barcodes": bc},
                            headers={"X-CSRF-Token": csrf}).json()
        assert r["assigned"] == 1
        wos = _wo(bc).split(";")
        assert len(wos) == 2 and wos[0].endswith("|completed") and wos[1].endswith("|pending")
        assert feed(view="assigned")["recordsFiltered"] == 1 and feed(view="completed")["recordsFiltered"] == 0
        r = row()
        assert "psiqc-start-btn" in r[-1] and "Completed" not in r[-1] and r[-2].endswith("—</span>")
        assert feed()["psiqc_tiles"] == {"assigned": 1, "completed": 0, "requested": 0, "verified": 0}
        # still Ready for Sale
        assert app_client.get("/sales/ready/data", params={"search[value]": bc}).json()["recordsFiltered"] == 1
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from part_requests where barcode = '{bc}'"))
        await db.execute(text("delete from work_orders where barcode = '{bc}'"))
        await db.execute(text("delete from devices where barcode = '{bc}'"))
        await db.commit()
asyncio.run(main())
""")


def test_pre_sale_iqc_bulk_complete(app_client, make_user):  # noqa: F811
    """Bulk Complete (2026-10-29): the ticked tags, same rules as the row's
    Mark Complete — tags not started are skipped and listed."""
    a, b = ("ITPSB" + uuid.uuid4().hex[:8].upper() for _ in range(2))
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        for bc in ("{a}", "{b}"):
            db.add(Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=DeviceStage.ready_to_sale,
                          device_type="Laptop", brand="HP", model="EliteBook 840 G5"))
        await db.commit()
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        assert 'id="psiqcBulkCompleteBtn"' in app_client.get("/pre-sale-iqc").text
        app_client.post("/pre-sale-iqc/assign", data={"csrf_token": csrf, "barcodes": f"{a},{b}"},
                        headers={"X-CSRF-Token": csrf})
        r = app_client.post("/pre-sale-iqc/start", data={"csrf_token": csrf, "barcode": a},
                            headers={"X-CSRF-Token": csrf})
        assert r.json()["ok"]
        _add_requests(a, [("Keyboard", "handed_over", 1)])
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.part_request import PartRequest
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        wo = (await db.execute(select(WorkOrder).where(WorkOrder.barcode == "{a}", WorkOrder.stage == "psiqc"))).scalars().first()
        db.add(PartRequest(work_order_id=wo.id, work_id=wo.work_id, device_id=wo.device_id, barcode="{a}", stage="psiqc",
                           part_name="RAM", part_capacity="16GB", part_type="DDR4", request_type="replace",
                           status="handed_over", qty_requested=1, qty_handed_over=1, requested_by="t"))
        await db.commit()
asyncio.run(main())
""")
        both = f"{a},{b},NOSUCHTAG{a}"
        pre = app_client.post("/pre-sale-iqc/bulk-complete-precheck", data={"csrf_token": csrf, "barcodes": both},
                              headers={"X-CSRF-Token": csrf}).json()
        assert [t["barcode"] for t in pre["ready"]] == [a]
        assert sorted(pre["ready"][0]["to_verify"]) == ["Keyboard ×1", "RAM ×1"]
        assert pre["not_started"] == [b] and pre["not_found"] == [f"NOSUCHTAG{a}"] and pre["pna"] == []
        d = app_client.post("/pre-sale-iqc/bulk-complete", data={"csrf_token": csrf, "barcodes": both},
                            headers={"X-CSRF-Token": csrf}).json()
        assert d["ok"] and d["completed"] == 1 and d["verified"] == 2 and d["not_started"] == 1
        assert _wo(a).endswith("|completed") and _wo(b).endswith("|pending")
        assert _prs(a) == "Keyboard|received;RAM|received"
        # verified RAM written onto the tag's spec (no Final QC pass is coming)
        ram = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(select(Device.ram_gb).where(Device.barcode == "{a}"))).scalar())
asyncio.run(main())
""")
        assert ram == "16", ram
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in ("{a}", "{b}"):
            await db.execute(text(f"delete from part_requests where barcode = '{{bc}}'"))
            await db.execute(text(f"delete from work_orders where barcode = '{{bc}}'"))
            await db.execute(text(f"delete from devices where barcode = '{{bc}}'"))
        await db.commit()
asyncio.run(main())
""")


def test_pre_sale_iqc_user_gets_floor_device_detail(app_client, make_user):  # noqa: F811
    """Device Detail (2026-10-30): a custom role with Pre-Sale IQC enabled and
    no All Inventories Edit sees the L1/L2 floor view (Parts Consumption only,
    full width); the same role WITH All Inventories Edit keeps the full page."""
    from models.role_permissions import get_cached_perms, set_cached_perms
    role = "itest_psiqc_" + uuid.uuid4().hex[:6]
    bc = "ITPSF" + uuid.uuid4().hex[:8].upper()
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        db.add(Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot, current_stage=DeviceStage.ready_to_sale,
                      device_type="Laptop", brand="Dell", model="Latitude 7490"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, pw = make_user(role)
        _login(app_client, u, pw)
        set_cached_perms(role, {"pre_sale_iqc": {"enable": True}, "devices": {"enable": True, "edit": False}})
        html = app_client.get(f"/devices/{bc}").text
        assert 'class="col-12"' in html and 'class="col-lg-7"' not in html
        set_cached_perms(role, {"pre_sale_iqc": {"enable": True}, "devices": {"enable": True, "edit": True}})
        html = app_client.get(f"/devices/{bc}").text
        assert 'class="col-lg-7"' in html
    finally:
        set_cached_perms(role, {})
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode = '{bc}'"))
        await db.commit()
asyncio.run(main())
""")
