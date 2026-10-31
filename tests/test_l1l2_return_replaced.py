"""L1/L2 Mark Complete for customer returns (2026-10-31):
- Return for Repair (customer gets this laptop back) -> straight to Sold;
- Replaced (Replace Now, Tag Return Status "Replaced by <tag>") or Credit
  Note ("Return for Credit Note") -> the customer has another laptop / was
  refunded, this one is our stock: usual process, Stress Test (qc_check)."""
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


def test_replaced_return_follows_usual_process(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:7].upper()
    tags = {"ITRR" + sfx: "Return for Repair", "ITRP" + sfx: "Replaced by ITNEW" + sfx,
            "ITRC" + sfx: "Return for Credit Note"}
    ids = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        out = []
        for bc, st in {tags!r}.items():
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=DeviceStage.l1,
                       device_type="Laptop", brand="Dell", model="E5400",
                       return_status=True, tag_return_status=st)
            db.add(d); out.append(f"{{bc}}={{d.id}}")
        await db.commit()
        print(" ".join(out))
asyncio.run(main())
""")
    dev = dict(x.split("=") for x in ids.split())
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        for bc in tags:
            r = app_client.post("/repair/l1l2-complete-to-stress", data={"csrf_token": csrf, "device_id": dev[bc]},
                                headers={"X-CSRF-Token": csrf}, follow_redirects=False)
            assert r.status_code == 302, r.text[:500]
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("select barcode, current_stage from devices where barcode like 'ITR_{sfx}' order by barcode"))).all()
        print(" ".join(f"{{b}}={{s}}" for b, s in rows))
asyncio.run(main())
""")
        assert out == f"ITRC{sfx}=qc_check ITRP{sfx}=qc_check ITRR{sfx}=sold", out
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "(select id from devices where barcode like 'ITR_{sfx}')"
        for t in ("stage_movements", "work_orders", "repair_jobs", "part_requests"):
            await db.execute(text(f"delete from {{t}} where device_id in {{ids}}"))
        await db.execute(text("delete from devices where barcode like 'ITR_{sfx}'"))
        await db.commit()
asyncio.run(main())
""")
