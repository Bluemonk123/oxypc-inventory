"""Rental Return (2026-11-05, Pankaj): optional Return Reason (new column
rental_returns.return_reason, same list as Internal / External returns) and
Complaint Description (the existing Notes field, renamed on the form)."""
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


def test_rental_return_saves_reason_and_complaint(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    a, b = f"ITRR{sfx}A", f"ITRR{sfx}B"
    _run(f"""
import asyncio, sys, uuid
from datetime import date, timedelta
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.rental import RentalRecord, RentalItem
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        for bc, num in (("{a}", "RNT-IT{sfx}A"), ("{b}", "RNT-IT{sfx}B")):
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot.id, current_stage=DeviceStage.on_rent)
            db.add(d); await db.flush()
            r = RentalRecord(rental_number=num, status="active", bill_type="individual", person_name="IT Rent",
                             duration_value=10, duration_unit="days", start_date=date.today() - timedelta(days=5),
                             end_date=date.today() + timedelta(days=5), total_quantity=1, total_price=1000)
            db.add(r); await db.flush()
            db.add(RentalItem(rental_id=r.id, device_id=d.id, barcode=bc))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, pw = make_user("admin")
        _login(app_client, u, pw)
        html = app_client.get("/returns/new?active_tab=rental").text
        tab = html.split('id="rentalReturnTab"', 1)[1]
        assert 'name="return_reason"' in tab and "Complaint Description" in tab and ">Notes<" not in tab
        assert 'name="return_reason" class="form-select" required' not in tab           # optional
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        today = __import__("datetime").date.today().isoformat()
        for bc, extra in ((a, {"return_reason": "Not working", "notes": "Fan noise"}), (b, {})):
            r = app_client.post("/rentals/return/submit", follow_redirects=False,
                                data={"csrf_token": csrf, "barcodes": bc, "received_date": today,
                                      "decision": "waive_off", **extra}, headers={"X-CSRF-Token": csrf})
            assert r.status_code == 302 and "error=" not in r.headers["location"], r.headers["location"]
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for n in ("RNT-IT{sfx}A", "RNT-IT{sfx}B"):
            row = (await db.execute(text("select rr.return_reason, rr.notes from rental_returns rr join rental_records r "
                                         "on r.id = rr.rental_id where r.rental_number = :n"), {{"n": n}})).first()
            print(repr(tuple(row)))
asyncio.run(main())
""").splitlines()
        assert out == ["('Not working', 'Fan noise')", "(None, None)"], out          # both optional
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "(select id from rental_records where rental_number like 'RNT-IT{sfx}%')"
        await db.execute(text(f"delete from rental_return_items where rental_return_id in (select id from rental_returns where rental_id in {{ids}})"))
        await db.execute(text(f"delete from rental_returns where rental_id in {{ids}}"))
        await db.execute(text(f"delete from rental_items where rental_id in {{ids}}"))
        await db.execute(text("delete from rental_records where rental_number like 'RNT-IT{sfx}%'"))
        dids = "(select id from devices where barcode like 'ITRR{sfx}%')"
        for t in ("stage_movements", "device_location_logs"):
            await db.execute(text(f"delete from {{t}} where device_id in {{dids}}"))
        await db.execute(text("delete from devices where barcode like 'ITRR{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")
