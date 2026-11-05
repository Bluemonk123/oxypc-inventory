"""Return Stock clean-up (2026-11-05, Pankaj):
  * CN Complete clears the tag's open-return flag;
  * a tag sold again after its return shows "Return Complete" (Completed tab);
  * Data Fixes "Open returns with no activity" lists a Sold, never-repaired
    open return and Close clears its flag (Credit Note returns not listed)."""
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


_SETUP = """
import asyncio, sys, uuid
from datetime import timedelta
sys.path.insert(0, r"{root}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.sales import Sale, Return
from utils.timezone import app_now
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        now = app_now()
        out = []
        for bc, cn in (("{cn}", "Restock Pending"), ("{rs}", None), ("{st}", None), ("{cnst}", "Return Received")):
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot.id, brand="Dell", model="Latitude 7490",
                       current_stage=DeviceStage.sold, return_status=True, is_active=True)
            db.add(d)
            await db.flush()
            db.add(Sale(sale_number=("T" + bc)[-20:], device_id=d.id, sale_price=0,
                        sold_at=now - timedelta(days=30)))
            r = Return(device_id=d.id, reason="Not working", complaint_text="x", cn_stage=cn,
                       return_date=now - timedelta(days=10))
            db.add(r)
            await db.flush()
            out.append(str(r.id))
            if bc == "{rs}":
                db.add(Sale(sale_number=("U" + bc)[-20:], device_id=d.id, sale_price=0,
                            sold_at=now - timedelta(days=2)))
        await db.commit()
        print(out[0])
asyncio.run(main())
"""

_CLEAN = """
import asyncio, sys
sys.path.insert(0, r"{root}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in {tags!r}:
            ids = f"(select id from devices where barcode = '{{bc}}')"
            for t in ("returns", "stage_movements", "device_location_logs", "sales"):
                await db.execute(text(f"delete from {{t}} where device_id in {{ids}}"))
            await db.execute(text(f"delete from devices where barcode = '{{bc}}'"))
        await db.commit()
asyncio.run(main())
"""


def _flags(tags):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in {tags!r}:
            print((await db.execute(text("select return_status from devices where barcode = :b"), {{"b": bc}})).scalar())
asyncio.run(main())
""").split()


def test_cn_complete_resold_and_stale_returns(app_client, make_user, monkeypatch):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    cn, rs, st, cnst = f"ITRC{sfx}C", f"ITRC{sfx}R", f"ITRC{sfx}S", f"ITRC{sfx}N"
    tags = [cn, rs, st, cnst]
    cn_rid = _run(_SETUP.format(root=ROOT, cn=cn, rs=rs, st=st, cnst=cnst))
    try:
        u, pw = make_user("admin")
        _login(app_client, u, pw)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        # Sold again after the return -> Return Complete row on Return Stock
        html = app_client.get("/stock", follow_redirects=True).text
        i = html.find(rs)
        assert i > 0
        row = html[html.rfind("<tr", 0, i):html.find("</tr>", i)]
        assert "Return Complete" in row and 'data-rs-tab="completed"' in row and "Sold again" in row

        # Data Fixes: stale list has the no-activity tag and the resold tag,
        # never the Credit Note ones
        page = app_client.get("/admin/system/data-fixes").text
        assert "Open returns with no activity" in page
        assert st in page and rs in page and cnst not in page and cn not in page

        # CN Complete clears the flag
        r = app_client.post(f"/credit-note/{cn_rid}/advance", data={"csrf_token": csrf},
                            headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        assert r.status_code == 302 and "error" not in r.headers["location"], r.headers.get("location")
        assert _flags([cn]) == ["False"]

        # Close the stale returns — limited to this test's tags so the local
        # database's own open returns are left for the admin to review.
        import routers.system_health as sh
        _orig = sh._stale_returns

        async def _only_ours(db):
            return [r for r in await _orig(db) if r["tag"] in tags]
        monkeypatch.setattr(sh, "_stale_returns", _only_ours)
        r = app_client.post("/admin/system/stale-returns-fix", data={"csrf_token": csrf},
                            headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["updated"] == 2
        assert _flags([rs, st, cnst]) == ["False", "False", "True"]
    finally:
        _run(_CLEAN.format(root=ROOT, tags=tags))
