"""Team Productivity export (2026-11-22, Pankaj):
- sheets: Tag Summary (each tag once), All Tags (every WorkID), Stage Summary,
  Staff Summary;
- reassigned WorkIDs are listed only here, with "Reassigned" as Completed Date;
- Staff Summary "Attendance" (Yes / No) after Engineer, Stage Summary
  "Attendance" (members present per stage) — for the selected date / range."""
import io
import pathlib
import subprocess
import sys
import uuid

from openpyxl import load_workbook

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_export_reassigned_tag_summary_and_attendance(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:5].upper()
    bc = f"TPRA{sfx}"
    ua, ub, uc = f"tpa_{sfx.lower()}", f"tpb_{sfx.lower()}", f"tpc_{sfx.lower()}"
    _run(f"""
import asyncio, sys, uuid
from datetime import datetime, date
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select, text
from database import AsyncSessionLocal
from models.user import User, UserRole
from models.attendance import Attendance
from models.device import Device, DeviceStage as S
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        any_u = (await db.execute(select(User).limit(1))).scalars().first()
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        users = []
        for un, nm in (("{ua}", "TP Eng A {sfx}"), ("{ub}", "TP Eng B {sfx}"), ("{uc}", "TP Eng C {sfx}")):
            u = User(id=uuid.uuid4(), username=un, full_name=nm, role=UserRole.l1_engineer,
                     password_hash=any_u.password_hash, status=True)
            db.add(u); users.append(u)
        await db.flush()
        for u in (users[0], users[2]):      # A (has work) and C (no work) present; B absent
            db.add(Attendance(user_id=u.id, username=u.username, full_name=u.full_name,
                              date=date(2026, 7, 14), status="present"))
        d = Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot, current_stage=S.l1)
        db.add(d); await db.flush()
        at = datetime(2026, 7, 14, 10, 0)
        # A held it, B took it over with Pick This at 11:00 -> A's WorkID reassigned
        db.add(WorkOrder(work_id="WXA{sfx}", device_id=d.id, barcode="{bc}", stage="l1", status="reassigned",
                         created_at=at, assigned_at=at, completed_at=datetime(2026, 7, 14, 11, 0),
                         assigned_user_id=users[0].id, assigned_username="{ua}", assigned_name="TP Eng A {sfx}"))
        db.add(WorkOrder(work_id="WXB{sfx}", device_id=d.id, barcode="{bc}", stage="l1", status="pending",
                         created_at=datetime(2026, 7, 14, 11, 0), assigned_at=datetime(2026, 7, 14, 11, 0),
                         assigned_user_id=users[1].id, assigned_username="{ub}", assigned_name="TP Eng B {sfx}"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        q = f"tag={bc}&work_from=2026-07-14&work_to=2026-07-14&exclude_admin="
        # the page still hides the reassigned WorkID
        page = app_client.get(f"/workid-status?{q}").text
        assert f"WXB{sfx}" in page and f"WXA{sfx}" not in page
        r = app_client.get(f"/workid-status/export?{q}")
        assert r.status_code == 200
        wb = load_workbook(io.BytesIO(r.content))
        assert wb.sheetnames == ["Tag Summary", "All Tags", "Stage Summary", "Staff Summary"]

        def rows(name):
            ws = wb[name]
            head = [c.value for c in ws[1]]
            return head, [dict(zip(head, x)) for x in ws.iter_rows(min_row=2, values_only=True)]
        _h, allr = rows("All Tags")
        mine = [x for x in allr if x["Tag Number"] == bc]
        assert len(mine) == 2
        ra = next(x for x in mine if (x["Engineer Name"] or "").startswith("TP Eng A"))
        assert ra["Completed Date"] == "Reassigned" and str(ra["Status"]).startswith("Reassigned")
        _h, one = rows("Tag Summary")
        tm = [x for x in one if x["Tag Number"] == bc]
        assert len(tm) == 1 and tm[0]["Engineer Name"].startswith("TP Eng B")    # its latest, not the reassigned row
        sh, staff = rows("Staff Summary")
        assert sh[:3] == ["Engineer", "Attendance", "Stage"]
        att = {x["Engineer"]: x["Attendance"] for x in staff if x["Engineer"]}
        assert att[f"TP Eng A {sfx}"] == "Yes" and att[f"TP Eng B {sfx}"] == "No"
        assert next(x for x in staff if x["Engineer"] == f"TP Eng A {sfx}")["Reassigned WorkIDs"] == 1
        assert all((x["Engineer"] or "").strip() != "\u2014" for x in staff)      # no "—" engineer: "Not picked"
        gh, stage = rows("Stage Summary")
        assert not any(str(x["Stage"] or "").startswith("All ") for x in stage)   # no subtotal rows
        assert gh[:3] == ["Stage Group", "Stage", "Attendance"]
        l12 = next(x for x in stage if x["Stage Group"] == "L1/L2")
        assert l12["Attendance"] >= 2                  # A and C (L1/L2 engineers) were present that day
        # C was present with no WorkID: still listed, Yes, zeros
        c_row = next(x for x in staff if x["Engineer"] == f"TP Eng C {sfx}")
        assert c_row["Attendance"] == "Yes" and c_row["Total WorkIDs"] == 0
        # Stage column: the stage each present member belongs to; none for B (absent)
        assert c_row["Stage"] == "L1/L2"
        assert next(x for x in staff if x["Engineer"] == f"TP Eng A {sfx}")["Stage"] == "L1/L2"
        assert not next(x for x in staff if x["Engineer"] == f"TP Eng B {sfx}")["Stage"]
        # one members list: Stage Summary Total = Staff Summary Yes count; L1/L2 = the card's Team
        st_total = next(x for x in stage if x["Stage Group"] == "Total")["Attendance"]
        assert st_total == sum(1 for x in staff if x["Attendance"] == "Yes" and x["Engineer"] != "Total")
        assert next(x for x in staff if x["Engineer"] == "Total")["Attendance"] == st_total
        import re
        team = re.search(r'data-group="l1l2".*?Team</span> <b>(\d+)</b>', page, re.S).group(1)
        assert int(team) == l12["Attendance"]
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from work_orders where barcode = '{bc}'"))
        await db.execute(text("delete from devices where barcode = '{bc}'"))
        await db.execute(text("delete from attendance where username in ('{ua}', '{ub}', '{uc}')"))
        await db.execute(text("delete from users where username in ('{ua}', '{ub}', '{uc}')"))
        await db.commit()
asyncio.run(main())
""")
