"""Device Edit save — never a bare 500 (2026-09-02).

Reported: /devices/{barcode}/edit threw a 500 on save. Root cause of that
specific report was never pinned down (not reproducible locally with either
a minimal or full-field submission), but the underlying gap is real: unlike
/iqc/new (routers/iqc.py's _iqc_form_boundary, in place for the same class
of bug), device_edit_save had no exception boundary at all — any unhandled
error reached the user as an opaque 500 with their edits lost and nothing
to report. This adds the same pattern: roll back, expunge current_user, log
a full traceback + submitted form server-side, and redirect back to the
edit form with a readable ?error=... (the same query param the barcode-
clash branch already redirects with, already rendered by
templates/devices/edit.html).
"""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


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
            # device_edit_save creates an IQCInspection row for any device
            # edited through this form — delete it first (FK on device_id).
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


def test_edit_save_with_a_genuine_db_error_redirects_with_error_not_500(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITEDITBOUND{suffix}"
    _seed_device(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        # A lot_id that doesn't exist violates devices.lot_id's FK constraint
        # at commit — a real, unpredictable-in-advance failure, same class as
        # what an unhandled exception in this handler used to turn into a 500.
        r = app_client.post(f"/devices/{barcode}/edit", data={
            "csrf_token": csrf, "model": "ChangedModel",
            "lot_id": str(uuid.uuid4()),
        }, follow_redirects=False)

        assert r.status_code == 302, r.text[:500]
        assert r.headers["location"].startswith(f"/devices/{barcode}/edit?error=")

        # The edit page still loads afterward (session/current_user usable,
        # not left in a DetachedInstanceError-triggering state).
        follow = app_client.get(r.headers["location"], follow_redirects=True)
        assert follow.status_code == 200
        assert "Could not save changes" in follow.text
    finally:
        _cleanup(barcode)


def test_edit_save_still_works_normally_after_the_boundary_wrap(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITEDITOK{suffix}"
    _seed_device(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        r = app_client.post(f"/devices/{barcode}/edit", data={
            "csrf_token": csrf, "model": "GenuinelyChangedModel",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == f"/devices/{barcode}?success=Device+updated+successfully"
    finally:
        _cleanup(barcode)
