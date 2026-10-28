"""Stress Test page (2026-10-28): the Edit Tag Details window has a Grade box
there, saved through POST /devices/{barcode}/specs. Grade is only touched when
the form sends it; an unreadable grade is refused. Table shows CPU / CPU Gen /
RAM / Storage, hides Lot, and has Actions last."""
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


def _seed(barcode):
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage, DeviceGrade
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        db.add(Device(id=uuid.uuid4(), barcode="{barcode}", lot_id=lot, current_stage=DeviceStage.qc_check,
                      grade=DeviceGrade.B, brand="Dell", model="Latitude 5490", cpu="I5", generation="8TH",
                      ram_gb=16, storage_gb=256))
        await db.commit()
asyncio.run(main())
""")


def _grade(barcode):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select coalesce(grade::text, 'NONE') from devices where barcode = :b"), {{"b": "{barcode}"}})).scalar())
asyncio.run(main())
""")


def _cleanup(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode = :b"), {{"b": "{barcode}"}})
        await db.commit()
asyncio.run(main())
""")


def test_grade_saved_from_spec_edit_and_stress_table_layout(app_client, make_user):  # noqa: F811
    barcode = "ITGRD" + uuid.uuid4().hex[:8]
    _seed(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        base = {"csrf_token": csrf, "brand": "Dell", "model": "Latitude 5490", "cpu": "I5",
                "generation": "8TH", "ram": "16 GB", "hard_drive": "256 GB"}

        # No grade in the form (L1/L2, Final QC): grade untouched
        r = app_client.post(f"/devices/{barcode}/specs", data=base, headers={"X-CSRF-Token": csrf})
        assert r.json()["ok"] and _grade(barcode) == "B"
        # Grade sent: saved
        r = app_client.post(f"/devices/{barcode}/specs", data={**base, "grade": "A"}, headers={"X-CSRF-Token": csrf})
        assert r.json() == {"ok": True, "changed": 1} and _grade(barcode) == "A"
        # Unreadable grade refused, nothing changed
        r = app_client.post(f"/devices/{barcode}/specs", data={**base, "grade": "zz"}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 400 and _grade(barcode) == "A"
        # Blank = no grade
        r = app_client.post(f"/devices/{barcode}/specs", data={**base, "grade": ""}, headers={"X-CSRF-Token": csrf})
        assert r.json()["ok"] and _grade(barcode) == "NONE"

        html = app_client.get("/qc").text
        head = html.split('id="qcTable"', 1)[1].split("</thead>", 1)[0]
        import re
        cols = re.findall(r"<th[^>]*>([^<]*)</th>", head)
        assert cols == ["Tag Number", "Lot", "L1/L2 Engineer", "Brand", "Model", "CPU", "CPU Gen",
                        "RAM", "Storage", "Grade", "Location", "Report", "Actions"]
        assert '<th class="d-none">Lot</th>' in head
        assert 'name="grade"' in html               # Grade box in the Edit window
        row = html.split(f'id="row-{barcode}"', 1)[1].split("</tr>", 1)[0]
        assert "8TH" in row and "16 GB" in row and "256 GB" in row
    finally:
        _cleanup(barcode)
