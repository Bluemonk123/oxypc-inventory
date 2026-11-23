"""Notes History (2026-11-23, Pankaj): every note is added as a new row and
never overwritten — a tag's Repair Notes changed twice keeps both, Final QC
Fail keeps its category, Pre-Sale QC Fail comes from its audit entry, a
dealer's call note goes under the dealer. Device Detail button + API,
admin page + export, Data Fixes copy of existing notes (once)."""
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


def test_notes_history(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    tag = f"NHT{sfx}"
    out = _run(f"""
import asyncio, sys, uuid, json
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text, select
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S
from models.engines import AuditLog
from models.dealers import Dealer, DealerCall
import services.notes_history  # noqa: F401
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        d = Device(id=uuid.uuid4(), barcode="{tag}", lot_id=lot, current_stage=S.l1, is_active=True,
                   repair_notes="first: no display")
        db.add(d); await db.commit()
        d.repair_notes = "second: board swap"; await db.commit()
        d.repair_notes = "second: board swap"; await db.commit()          # same text again: no new row
        d.fqc_failure_reason = "Hardware"; d.fqc_final_notes = "hinge loose"; await db.commit()
        db.add(AuditLog(id=uuid.uuid4(), action="PRE_SALE_IQC_COMPLETED", record_id=str(d.id), username="ps_user",
                        new_value=json.dumps({{"result": "fail", "category": "Cosmetic", "reason": "scratch on lid"}})))
        dl = Dealer(id=uuid.uuid4(), dealer_code="NH{sfx}", business_name="NH Dealer {sfx}")
        db.add(dl); await db.flush()
        db.add(DealerCall(dealer_id=dl.id, called_by="nh_tester", notes="call back Monday"))
        await db.commit()
        rows = (await db.execute(text("select note_type, note_text, coalesce(category,''), stage from notes_history "
                                      "where device_id = :d order by written_at, note_type"), {{"d": d.id}})).all()
        print(json.dumps([list(map(str, r)) for r in rows]))
        print((await db.execute(text("select count(*) from notes_history where record_type = 'dealer' and record_id = :i "
                                     "and note_type = 'Dealer Call Notes'"), {{"i": str(dl.id)}})).scalar())
        print(dl.id)
asyncio.run(main())
""").splitlines()
    import json
    rows, dealer_calls, dealer_id = json.loads(out[0]), out[1], out[2]
    try:
        texts = [(r[0], r[1]) for r in rows]
        assert ("L1/L2 Notes", "first: no display") in texts and ("L1/L2 Notes", "second: board swap") in texts
        assert sum(1 for t in texts if t == ("L1/L2 Notes", "second: board swap")) == 1
        fq = [r for r in rows if r[0] == "Final QC Fail Notes"]
        assert len(fq) == 1 and fq[0][1] == "hinge loose" and fq[0][2] == "Hardware"
        ps = [r for r in rows if r[0] == "Pre-Sale QC Fail Notes"]
        assert len(ps) == 1 and ps[0][1] == "scratch on lid" and ps[0][2] == "Cosmetic"
        assert rows[0][3] == "l1"
        assert dealer_calls == "1"

        u, p = make_user("admin")
        _login(app_client, u, p)
        page = app_client.get(f"/devices/{tag}").text
        assert "data-notes-history" in page and 'id="notesHistoryModal"' in page
        d = app_client.get(f"/notes-history/api?barcode={tag}").json()
        assert d["ok"] and len(d["rows"]) == 4 and d["rows"][0]["note_type"] == "Pre-Sale QC Fail Notes"
        dd = app_client.get(f"/notes-history/api?record_type=dealer&record_id={dealer_id}").json()
        assert dd["ok"] and dd["rows"][0]["note"] == "call back Monday"
        nh = app_client.get("/notes-history", params={"search": tag}).text
        assert "hinge loose" in nh and "board swap" in nh
        x = app_client.get("/notes-history/export", params={"search": tag})
        assert x.status_code == 200 and x.content[:2] == b"PK"
        df = app_client.get("/admin/system/data-fixes").text
        assert 'data-df="notes-history"' in df
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post("/admin/system/notes-history-backfill", data={"csrf_token": csrf}, headers={"x-csrf-token": csrf}).json()
        assert r["ok"]
        r2 = app_client.post("/admin/system/notes-history-backfill", data={"csrf_token": csrf}, headers={"x-csrf-token": csrf}).json()
        assert r2["ok"] and r2["done_before"] and r2["rows"] == 0          # runs once
        cnt = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select count(*) from notes_history n join devices d on d.id = n.device_id "
                                     "where d.barcode = '{tag}' and n.backfilled"))).scalar())
asyncio.run(main())
""")
        assert int(cnt) >= 2       # current Repair Notes + Final QC Fail copied as "existing"
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from notes_history where backfilled"))
        await db.execute(text("delete from notes_history where device_id in (select id from devices where barcode = '{tag}') "
                              "or record_id = '{dealer_id}'"))
        await db.execute(text("delete from audit_logs where record_id in (select id::text from devices where barcode = '{tag}')"))
        await db.execute(text("delete from dealer_calls where dealer_id = '{dealer_id}'"))
        await db.execute(text("delete from dealers where id = '{dealer_id}'"))
        await db.execute(text("delete from devices where barcode = '{tag}'"))
        await db.commit()
asyncio.run(main())
""")
