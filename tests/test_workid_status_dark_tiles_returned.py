"""WorkID Status (2026-11-22, Pankaj): the four dark tiles are the totals of
the stage cards — Total Pending = Pending + Started of every card, Total PNA =
L1/L2 / L3/L4 tags holding a PNA part on the last date, Total Repaired = Final
QC Pass, Total Scrap = L1/L2 Scrap + L3/L4 Scrap — and the Returned card
(Pending / Started / Pass / Replace). A single date means that one day."""
import pathlib
import re
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def _dark(html):
    return {lbl: int(n.replace(",", "")) for n, lbl in re.findall(
        r'oxt-count"[^>]*>([\d,]+)</div>\s*<div class="oxt-label"[^>]*>(Total [\w ]+?)<', html)}


def _card(html, grp):
    card = html.split('data-group="%s"' % grp, 1)[1].split('data-group=', 1)[0]
    return dict((k, int(v)) for k, v in re.findall(r'data-k="(\w+)" data-server="(\d+)"', card))


def test_dark_tiles_and_returned_card(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    day = "2026-09-15"
    _run(f"""
import asyncio, sys, uuid
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage, StageMovement
from models.pna_part import DevicePNAPart
from models.sales import Return
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        D = lambda d, h: datetime(2026, 9, d, h, 0)
        def dev(bc, now_stage, trs=None):
            d = Device(id=uuid.uuid4(), barcode=bc + "{sfx}", lot_id=lot, current_stage=now_stage,
                       tag_return_status=trs)
            db.add(d)
            return d
        def mv(d, frm, to, at, notes=None):
            db.add(StageMovement(device_id=d.id, from_stage=frm, to_stage=to, moved_at=at, moved_by="t", notes=notes))
        # Repaired twice on the 15th (failed in between) -> counts once; ready today
        r = dev("DKR", DeviceStage.ready_to_sale); await db.flush()
        mv(r, DeviceStage.final_qc, DeviceStage.final_qc_pass_hold, D(15, 10))
        mv(r, DeviceStage.final_qc_pass_hold, DeviceStage.final_qc, D(15, 11))
        mv(r, DeviceStage.final_qc, DeviceStage.ready_to_sale, D(15, 12))
        # In L1/L2 on the 15th (pending), sold today
        p = dev("DKP", DeviceStage.sold); await db.flush()
        mv(p, DeviceStage.stock_in, DeviceStage.l1, D(15, 9))
        mv(p, DeviceStage.l1, DeviceStage.sold, D(20, 9))
        db.add(WorkOrder(work_id="DKW{sfx}", device_id=p.id, barcode="DKP{sfx}", stage="l1", status="pending",
                         created_at=D(15, 9), assigned_at=D(15, 9),
                         assigned_username="dk_eng", assigned_name="dk_eng"))
        db.add(DevicePNAPart(device_id=p.id, barcode="DKP{sfx}", part_name="RAM", source="l1",
                             marked_by="t", marked_at=D(14, 9), cleared_at=D(18, 9), is_active=False))
        # Scrap: L3/L4 decision and L1/L2 Back to Inventory the same day -> once
        s = dev("DKS", DeviceStage.scrap_for_sale); await db.flush()
        mv(s, DeviceStage.l3, DeviceStage.l1, D(15, 13), "L3/L4 Normal Scrap")
        mv(s, DeviceStage.l1, DeviceStage.scrap_for_sale, D(15, 14), "Back to Inventory \\u2014 Normal Scrap")
        for wid, code, at in (("L3L4-K{sfx}", "l3", D(15, 13)), ("DKSL{sfx}", "l1", D(15, 14))):
            db.add(WorkOrder(work_id=wid, device_id=s.id, barcode="DKS{sfx}", stage=code, status="completed",
                             created_at=D(15, 8), assigned_at=D(15, 8), completed_at=at,
                             assigned_username="dk_eng", assigned_name="dk_eng"))
        # Customer Return card (2026-11-27): from RMA Devices on the 15th — one
        # still there (Pending), one picked and made Ready (Started, Pass), one
        # replaced: customer tag to IQC, replacement tag onto RMA (Replace).
        uid = (await db.execute(text("select id from users limit 1"))).scalar()
        a = dev("RMP", DeviceStage.rma, "Return for Repair"); await db.flush()
        mv(a, DeviceStage.stock_in, DeviceStage.rma, D(15, 9), "Moved to RMA Devices")
        b = dev("RMS", DeviceStage.sold, "Return for Repair"); await db.flush()
        mv(b, DeviceStage.stock_in, DeviceStage.rma, D(15, 9), "Moved to RMA Devices")
        mv(b, DeviceStage.rma, DeviceStage.sold, D(15, 12), "RMA Ready \u2014 customer return, moved to Sold (no new sale)")
        db.add(WorkOrder(work_id="RMA-S{sfx}"[:12], device_id=b.id, barcode="RMS{sfx}", stage="rma", status="completed",
                         created_at=D(15, 9), assigned_at=D(15, 10), completed_at=D(15, 12), assigned_user_id=uid,
                         assigned_username="dk_eng", assigned_name="dk_eng"))
        c = dev("RMR", DeviceStage.iqc, "Replaced by RMY{sfx}"); await db.flush()
        mv(c, DeviceStage.stock_in, DeviceStage.rma, D(15, 9), "Moved to RMA Devices")
        mv(c, DeviceStage.rma, DeviceStage.iqc, D(15, 13), "RMA Replace \u2014 customer given RMY{sfx}; tag back to IQC")
        y = dev("RMY", DeviceStage.rma, "Replacement for RMR{sfx}"); await db.flush()
        mv(y, DeviceStage.ready_to_sale, DeviceStage.rma, D(15, 13), "RMA Replace \u2014 replacement for customer tag RMR{sfx}, Ready")
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        html = app_client.get(f"/workid-status?work_from={day}&work_to={day}").text
        dk = _dark(html)
        assert set(dk) == {"Total Repair Line Pending", "Total PNA", "Total Repaired", "Total Scrap"}, dk
        # 2026-11-22: the dark tiles are the totals of the cards below
        cards = {g: _card(html, g) for g in ("l1l2", "l3l4", "stress", "cosmetic", "final_qc")}
        assert dk["Total Repair Line Pending"] == sum(c["pending"] for c in cards.values())   # tags sitting on each stage
        assert dk["Total Repaired"] == cards["final_qc"]["pass"]
        assert dk["Total Scrap"] <= cards["l1l2"]["scrap"] + cards["l3l4"]["scrap"]   # each tag once
        assert cards["l1l2"]["pending"] >= 1 and dk["Total PNA"] >= 1     # DKP: L1/L2 WorkID + PNA on the 15th
        # Scrapped by L3/L4 and then L1/L2: on both cards, once in Total Scrap
        one = app_client.get(f"/workid-status?tag=DKS{sfx}&work_from={day}&work_to={day}").text
        assert _card(one, "l1l2")["scrap"] == 1 and _card(one, "l3l4")["scrap"] == 1
        assert _dark(one)["Total Scrap"] == 1
        ret = _card(html, "returned")
        assert ret == {"pending": 1, "pass": 1, "replace": 1}, ret   # from RMA Devices (2026-11-27), no Started line
        assert 'data-group="pre_sale_iqc"' not in html
        # More than one day: no Pending / Started lines and no Moved In count
        multi = app_client.get("/workid-status?work_from=2026-09-14&work_to=2026-09-15").text
        for g in ("l1l2", "l3l4", "stress", "cosmetic", "final_qc", "returned"):
            v = _card(multi, g)
            assert "pending" not in v and "started" not in v and "moved" not in v, (g, v)
        assert "Pending</span>" in html     # single day still shows them
        # Returned card: Team (present Return Engineers) beside the title, no footer (2026-11-27)
        ret_card = html.split('data-group="returned"', 1)[1][:4000]
        assert "Return Engineers (Designation)" in ret_card and "bi-person-fill" in ret_card
        assert "oxt-mfoot" not in ret_card
        # A single date (only From filled in) = that one day, not "until now"
        single = app_client.get("/workid-status?work_from=2026-09-15").text
        assert _dark(single) == dk and _card(single, "returned") == ret
        assert "On 15-Sep-2026" in html
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode like '%{sfx}'"
        for t in ("returns", "stage_movements", "device_pna_parts", "work_orders"):
            await db.execute(text(f"delete from {{t}} where device_id in ({{ids}})"))
        await db.execute(text(f"delete from devices where barcode like '%{sfx}'"))
        await db.commit()
asyncio.run(main())
""")
