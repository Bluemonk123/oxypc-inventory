"""2026-09-22: test-data intake for External Partner (Trade Partner) testing.

/trade-partner/manage-lots is a 2-tab page ("GRN & Lot", "Asset IQC") that
EMBEDS the real GRN CRUD (Add/Edit/Delete GRN, Add/Edit Lot) and the real
Bulk Upload IQC form -- not a hub linking out to the live pages. Records
created here must remain fully separate from live GRN/Lot/Device data:

1. A GRN created via this page's Add GRN modal is tagged
   GRNImport.source = EXTERNAL_PARTNER_TEST_GRN_SOURCE ("ext_partner_test")
   -- reusing the existing `source` column the live GRN pages already
   filter on, rather than inferring test-ness from lot-number prefix (which
   would leave a freshly-created, not-yet-lot-mapped GRN unflagged). It must
   never appear on /grn ("GRN with Invoice") or /grn/post-iqc ("GRN post
   IQC"), and Edit/Delete on it must redirect back to Manage Lots, not those
   live pages.
2. An EPT- lot auto-restricts itself the moment routers/grn.py's REAL
   grn_add_lot endpoint creates it -- never open to the live dealer catalog
   by default (Lot.is_restricted otherwise defaults to False = visible to
   every dealer) -- and is excluded from the live /lots (Lot Overview)
   default view, though an explicit search for it still finds it.
3. Devices on the External Partner Test entity are excluded from
   routers/devices.py's default (unfiltered) All Inventory view, but still
   show up when explicitly entity-filtered -- proving the exclusion is a
   default, not a hard block.
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


def _seed_bare_device(barcode):
    """A device with NO entity set (the common 'Unassigned' case) -- guards
    the NULL-safety bug the exclusion clauses had before it was fixed:
    `Device.entity != X` alone evaluates to SQL NULL (not TRUE) for a NULL
    entity, silently excluding every entity-less device from default views,
    not just the External Partner test entity."""
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        db.add(Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.iqc))
        await db.commit()

asyncio.run(main())
""")


def _cleanup_bare_device(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device, StageMovement

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for m in (await db.execute(select(StageMovement).where(StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(m)
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_entity_less_device_still_shows_in_default_views(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    barcode = f"ITNOENT{suffix}"
    username, password = make_user("admin")
    _login(app_client, username, password)
    _seed_bare_device(barcode)
    try:
        assert barcode in app_client.get("/devices", follow_redirects=True).text
    finally:
        _cleanup_bare_device(barcode)


def _seed_test_device(barcode, lot_id):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.master import EXTERNAL_PARTNER_TEST_ENTITY

async def main():
    async with AsyncSessionLocal() as db:
        db.add(Device(barcode="{barcode}", lot_id="{lot_id}", brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.iqc, entity=EXTERNAL_PARTNER_TEST_ENTITY))
        await db.commit()

asyncio.run(main())
""")


def _lot_field(lot_number, field):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).where(Lot.lot_number == "{lot_number}"))).scalar_one()
        print(getattr(lot, "{field}"))

asyncio.run(main())
""")


def _cleanup(barcode, lot_number, grn_number):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device, StageMovement
from models.lot import Lot
from models.grn_import import GRNImport

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for m in (await db.execute(select(StageMovement).where(StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(m)
            await db.delete(dev)
        lot = (await db.execute(select(Lot).where(Lot.lot_number == "{lot_number}"))).scalar_one_or_none()
        if lot:
            await db.delete(lot)
        if "{grn_number}":
            g = (await db.execute(select(GRNImport).where(GRNImport.grn_number == "{grn_number}"))).scalar_one_or_none()
            if g:
                await db.delete(g)
        await db.commit()

asyncio.run(main())
""")


def test_grn_lot_iqc_stay_off_live_pages_and_show_on_manage_lots(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    lot_number = f"EPT-{suffix}"
    barcode = f"ITEPT{suffix}"
    inv_no = f"ITEPTINV{suffix}"
    username, password = make_user("admin")
    _login(app_client, username, password)
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    grn_number = ""
    try:
        # 1. Add GRN through the Manage Lots page's own modal (source =
        # ext_partner_test) — must redirect back to Manage Lots, not the
        # live GRN post-IQC page.
        r = app_client.post("/grn/create-manual", data={
            "csrf_token": csrf, "sender_name": "ITest External Partner Supplier",
            "invoice_number": inv_no, "quantity": "1", "amount": "0",
            "source": "ext_partner_test",
        }, follow_redirects=False)
        assert r.status_code == 302, r.text[:300]
        assert r.headers.get("location", "").startswith("/trade-partner/manage-lots"), r.headers.get("location")

        grn_number = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.grn_import import GRNImport

async def main():
    async with AsyncSessionLocal() as db:
        g = (await db.execute(select(GRNImport).where(GRNImport.invoice_number == "{inv_no}"))).scalar_one()
        print(g.grn_number)

asyncio.run(main())
""")
        grn_id = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.grn_import import GRNImport

async def main():
    async with AsyncSessionLocal() as db:
        g = (await db.execute(select(GRNImport).where(GRNImport.invoice_number == "{inv_no}"))).scalar_one()
        print(g.id)

asyncio.run(main())
""")

        # 2. Never on the live GRN pages.
        assert inv_no not in app_client.get("/grn/post-iqc").text
        assert inv_no not in app_client.get("/grn").text

        # 3. Manage Lots no longer has a GRN step (2026-11-23, Pankaj): it shows
        # Lots / Models / Tags from the uploaded test tags only.
        ml_html = app_client.get("/trade-partner/manage-lots").text
        assert 'data-bs-target="#lotsTab"' in ml_html and 'data-bs-target="#modelsTab"' in ml_html
        assert "grn/_post_iqc" not in ml_html and inv_no not in ml_html

        # 4. Add Lot via the same real endpoint used elsewhere — must
        # auto-restrict immediately.
        r2 = app_client.post(f"/grn/{grn_id}/add-lot", data={
            "csrf_token": csrf, "lot_number": [lot_number],
        }, follow_redirects=False)
        assert r2.status_code == 200, r2.text[:300]
        assert r2.json().get("ok") is True, r2.text[:300]
        assert _lot_field(lot_number, "is_restricted") == "True"

        # 5. Excluded from the live Lot Overview default view...
        default_lots_html = app_client.get("/lots", follow_redirects=True).text
        assert lot_number not in default_lots_html
        # ...but an explicit search still finds it.
        searched_lots_html = app_client.get(f"/lots?q={lot_number}", follow_redirects=True).text
        assert lot_number in searched_lots_html

        # 6. Editing the GRN also redirects back to Manage Lots, not the
        # live GRN post-IQC page.
        r3 = app_client.post(f"/grn/{grn_id}/edit", data={
            "csrf_token": csrf, "sender_name": "ITest External Partner Supplier Updated",
            "invoice_number": inv_no,
        }, follow_redirects=False)
        assert r3.status_code == 302, r3.text[:300]
        assert r3.headers.get("location", "").startswith("/trade-partner/manage-lots"), r3.headers.get("location")

        # 7. Seed a device on the test entity against this lot (stands in
        # for the real Bulk Upload IQC step, which is unrelated pre-existing
        # logic) and confirm it shows on the Asset IQC tab and is excluded
        # from All Inventory's default view but visible when explicitly
        # entity-filtered.
        lot_id = _lot_field(lot_number, "id")
        _seed_test_device(barcode, lot_id)

        ml_html2 = app_client.get("/trade-partner/manage-lots").text
        assert barcode in ml_html2

        default_html = app_client.get("/devices", follow_redirects=True).text
        assert barcode not in default_html
        filtered_html = app_client.get(
            "/devices?entity=External+Partner+Test", follow_redirects=True).text
        assert barcode in filtered_html
    finally:
        _cleanup(barcode, lot_number, grn_number)
