"""IQC Compare (2026-11-22, Pankaj): NEW IQC (as the tag came in), TRC IQC
(the live record) and SOLD IQC (one per sale).
- NEW is written when the tag gets its IQC in intake, follows edits while still
  in intake, and is frozen once the tag moves on.
- An old tag that gets an IQC record later (not in intake) gets no NEW.
- Every Sale writes a SOLD copy linked to that sale.
- Device Detail has the IQC Compare button; /devices/api/iqc-compare returns all three."""
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


def test_iqc_compare(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    tag, old = f"IQCA{sfx}", f"IQCB{sfx}"
    sale_no = f"TS{sfx}"
    out = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select, text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S
from models.iqc_inspection import IQCInspection
from models.iqc_snapshot import IQCSnapshot
from models.sales import Sale
import services.iqc_snapshot  # noqa: F401  (the flush hook)
async def snaps(db, did):
    return (await db.execute(select(IQCSnapshot).where(IQCSnapshot.device_id == did)
                             .order_by(IQCSnapshot.kind))).scalars().all()
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        d = Device(id=uuid.uuid4(), barcode="{tag}", lot_id=lot, current_stage=S.iqc, is_active=True, ram_gb=8)
        o = Device(id=uuid.uuid4(), barcode="{old}", lot_id=lot, current_stage=S.l1, is_active=True)
        db.add_all([d, o]); await db.commit()
        insp = IQCInspection(device_id=d.id, power_on="Yes", screen_dot="No")
        db.add(insp); await db.commit()
        s = await snaps(db, d.id)
        r = [len(s), s[0].kind, s[0].iqc["inspection"]["screen_dot"], s[0].iqc["device"]["ram_gb"]]
        insp.screen_dot = "Yes"; await db.commit()                 # still in intake -> NEW follows
        await db.refresh(s[0]); r.append(s[0].iqc["inspection"]["screen_dot"])
        d.current_stage = S.l1; await db.commit()
        insp.screen_dot = "No"; d.ram_gb = 16; await db.commit()   # repair line -> NEW frozen
        s = await snaps(db, d.id); r += [len(s), s[0].iqc["inspection"]["screen_dot"], s[0].iqc["device"]["ram_gb"]]
        db.add(IQCInspection(device_id=o.id, power_on="Yes")); await db.commit()
        r.append(len(await snaps(db, o.id)))                       # old tag -> no NEW
        db.add(Sale(sale_number="{sale_no}", device_id=d.id, sale_price=1000)); await db.commit()
        s = [x for x in await snaps(db, d.id) if x.kind == "SOLD"]
        sid = (await db.execute(text("select id from sales where sale_number = '{sale_no}'"))).scalar()
        r += [len(s), s[0].sale_id == sid, s[0].iqc["inspection"]["screen_dot"], s[0].iqc["device"]["ram_gb"]]
        print("|".join(map(str, r)))
asyncio.run(main())
""")
    try:
        assert out == "1|NEW|No|8|Yes|1|Yes|8|0|1|True|No|16", out
        u, p = make_user("admin")
        _login(app_client, u, p)
        page = app_client.get(f"/devices/{tag}").text
        assert 'id="iqcCompareBtn"' in page and 'id="iqcCompareModal"' in page
        d = app_client.get(f"/devices/api/iqc-compare?barcode={tag}").json()
        assert d["ok"] and d["new"] and d["trc"] and len(d["sold"]) == 1
        assert d["new"]["iqc"]["inspection"]["screen_dot"] == "Yes"
        assert d["trc"]["iqc"]["inspection"]["screen_dot"] == "No"
        assert d["sold"][0]["sale_number"] == sale_no
        assert "screen_dot" in d["inspection_keys"] and "ram_gb" in d["spec_keys"]
        o = app_client.get(f"/devices/api/iqc-compare?barcode={old}").json()
        assert o["ok"] and o["new"] is None and o["sold"] == []
        json.dumps(d)
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode in ('{tag}', '{old}')"
        for t in ("iqc_snapshots", "sales", "iqc_inspections", "stage_movements", "work_orders", "device_location_logs"):
            await db.execute(text(f"delete from {{t}} where device_id in ({{ids}})"))
        await db.execute(text("delete from devices where barcode in ('{tag}', '{old}')"))
        await db.commit()
asyncio.run(main())
""")
