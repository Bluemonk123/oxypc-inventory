"""Free-text field length limits — Device Edit + IQC Entry forms (2026-09-02).

Reported: /devices/{barcode}/edit threw a 500 when the Model field was
edited. Reproduced directly: Device.model is String(100), but neither form
had a maxlength on it (or several sibling fields with the same DB-column-vs-
form gap) — a value over the DB limit throws asyncpg's
StringDataRightTruncationError mid-flush, not a validation error. The
session-rollback boundary fix (see test_device_edit_error_boundary.py /
test_iqc_new_rollback_order.py) stops that from reaching the user as a bare
500, but the friendly message is still an unreadable DBAPIError dump — the
real fix is stopping the value at the browser before it's ever submitted.

This adds maxlength to every free-text input matching its DB column's
String(N) length, on both forms.
"""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)

# field name -> maxlength, matching models/device.py's String(N) columns.
EXPECTED_MAXLENGTHS = {
    "model": "100",
    "serial_no": "100",
    "cpu": "100",
    "cpu_make": "100",
    "generation": "50",
    "color": "30",
    "grn_number": "100",
    "total_ram_count": "50",
    "total_ram_size": "50",
    "total_hdd_count": "50",
    "total_hdd_size": "50",
    "ram_summary": "255",
    "hdd_summary": "255",
}


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"subprocess failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def _seed_device(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        db.add(Device(barcode="{barcode}", lot_id=lot.id, brand="X", model="Y"))
        await db.commit()

asyncio.run(main())
""")


def _cleanup(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.iqc_inspection import IQCInspection

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for insp in (await db.execute(
                    select(IQCInspection).where(IQCInspection.device_id == dev.id))).scalars().all():
                await db.delete(insp)
            await db.flush()
            # everything else linked to the tag (IQC copies, history, …) — same cleanup the app uses
            from utils.fk_purge import purge_references
            await purge_references(db, "devices", [dev.id])
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_device_edit_form_has_maxlength_on_every_bounded_field(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMAXLENEDIT{suffix}"
    _seed_device(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get(f"/devices/{barcode}/edit", follow_redirects=True).text
        for field, maxlen in EXPECTED_MAXLENGTHS.items():
            if field == "grn_number":      # a dropdown of GRN records on Edit too since 2026-11-24
                assert '<select name="grn_number"' in html
                continue
            assert f'name="{field}" maxlength="{maxlen}"' in html, field
    finally:
        _cleanup(barcode)


def test_iqc_new_form_has_maxlength_on_every_bounded_field(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/iqc/new", follow_redirects=True).text
    for field, maxlen in EXPECTED_MAXLENGTHS.items():
        if field == "grn_number":      # a dropdown of GRN records on IQC since 2026-11-24
            assert '<select name="grn_number"' in html
            continue
        assert f'maxlength="{maxlen}"' in html.split(f'name="{field}"', 1)[1][:40], field


def test_overlong_model_still_degrades_cleanly_not_500(app_client, make_user):  # noqa: F811
    """Belt-and-suspenders: even if maxlength is bypassed (curl, a stale
    cached page, a scanner pasting raw text), the server-side boundary from
    the earlier fix must still catch it — never a bare 500."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMAXLENSRV{suffix}"
    _seed_device(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        r = app_client.post(f"/devices/{barcode}/edit", data={
            "csrf_token": csrf, "model": "X" * 150,
        }, follow_redirects=False)

        assert r.status_code == 302, r.text[:500]
        assert r.headers["location"].startswith(f"/devices/{barcode}/edit?error=")
    finally:
        _cleanup(barcode)
