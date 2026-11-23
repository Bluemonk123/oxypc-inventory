"""Rebuild old IQC history (2026-11-22, Pankaj): NEW / SOLD copies for tags
and sales from before IQC Compare, worked back from the live IQC:
- quick spec edit (old value logged) and part-change RAM (old text logged)
  are undone — all for NEW, only those after the sale for SOLD;
- a Revalidate IQC field (no old value) shows as not recorded;
- tags that already have copies are skipped; a second run adds nothing."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=180)
    assert r.returncode == 0, r.stderr[-2500:]
    return r.stdout.strip()


def test_iqc_history_rebuild(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    tag, sale_no = f"IQRB{sfx}", f"TR{sfx}"
    _run(f"""
import asyncio, sys, uuid, json
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S, StageMovement
from models.iqc_inspection import IQCInspection
from models.sales import Sale
from models.engines import AuditLog
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        # today's (TRC) values: RAM 16 after a part change, CPU fixed by quick edit, screen_dot revalidated
        d = Device(id=uuid.uuid4(), barcode="{tag}", lot_id=lot, current_stage=S.ready_to_sale, is_active=True,
                   ram_gb=16, cpu="i7", brand="Dell")
        db.add(d); await db.flush()
        insp = IQCInspection(id=uuid.uuid4(), device_id=d.id, power_on="Yes", screen_dot="No",
                             inspected_at=datetime(2026, 1, 5, 10))
        db.add(insp); await db.flush()
        db.add(StageMovement(device_id=d.id, from_stage=S.iqc, to_stage=S.l1, moved_at=datetime(2026, 1, 6, 10)))
        A = lambda **k: db.add(AuditLog(id=uuid.uuid4(), **k))
        A(action="DEVICE_SPECS_QUICK_EDIT", record_id=str(d.id), timestamp=datetime(2026, 1, 7),
          old_value=json.dumps({{"barcode": "{tag}", "cpu": "i5"}}), new_value=json.dumps({{"cpu": "i7"}}))
        A(action="IQC_REVALIDATED", record_id=str(insp.id), timestamp=datetime(2026, 1, 8),
          new_value=json.dumps({{"screen_dot": "No"}}))
        db.add(Sale(sale_number="{sale_no}", device_id=d.id, sale_price=1000, sold_at=datetime(2026, 1, 9)))
        A(action="DEVICE_SPEC_FROM_PARTS", record_id=str(d.id), timestamp=datetime(2026, 1, 12),
          old_value=json.dumps({{"barcode": "{tag}", "values": {{"RAM": "8 GB"}}}}), new_value="{{}}")
        await db.commit()
        # the go-live hook wrote copies for this test's inserts; drop them so it looks like an old tag
        await db.execute(text("delete from iqc_snapshots where device_id = :d"), {{"d": d.id}}); await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        page = app_client.get("/admin/system/data-fixes").text
        assert 'data-df="iqc-history"' in page and "/admin/system/iqc-history-rebuild" in page
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post("/admin/system/iqc-history-rebuild", data={"csrf_token": csrf},
                            headers={"x-csrf-token": csrf})
        assert r.status_code == 200 and r.json()["ok"], r.text
        d = app_client.get(f"/devices/api/iqc-compare?barcode={tag}").json()
        new, sold, trc = d["new"], d["sold"], d["trc"]
        assert new["backfilled"] and new["iqc"]["device"]["cpu"] == "i5" and new["iqc"]["device"]["ram_gb"] == 8
        assert new["context"]["unknown_fields"]["screen_dot"] == "changed 08-01-2026"
        assert new["iqc"]["inspection"]["power_on"] == "Yes"
        assert len(sold) == 1 and sold[0]["sale_number"] == sale_no and sold[0]["backfilled"]
        s = sold[0]["iqc"]["device"]
        assert s["cpu"] == "i7" and s["ram_gb"] == 8           # quick edit before the sale kept; RAM change after it undone
        assert "unknown_fields" not in sold[0]["context"]
        assert trc["iqc"]["device"]["ram_gb"] == 16
        again = app_client.post("/admin/system/iqc-history-rebuild", data={"csrf_token": csrf},
                                headers={"x-csrf-token": csrf}).json()
        assert again["ok"]
        cnt = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select count(*) from iqc_snapshots s join devices d on d.id = s.device_id where d.barcode = '{tag}'"))).scalar())
asyncio.run(main())
""")
        assert cnt == "2"
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode = '{tag}'"
        await db.execute(text(f"delete from audit_logs where record_id in (select id::text from devices where barcode = '{tag}') "
                              f"or record_id in (select i.id::text from iqc_inspections i join devices d on d.id = i.device_id where d.barcode = '{tag}')"))
        for t in ("iqc_snapshots", "sales", "iqc_inspections", "stage_movements", "work_orders", "device_location_logs"):
            await db.execute(text(f"delete from {{t}} where device_id in ({{ids}})"))
        await db.execute(text("delete from devices where barcode = '{tag}'"))
        await db.commit()
asyncio.run(main())
""")
