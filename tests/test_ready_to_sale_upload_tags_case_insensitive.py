"""Ready to Sale — Bulk Upload Tags (POST /sales/ready/upload-tags).

2026-10-19: tags of ANY stage are selectable for sale; the only two checks
are "not found" and "Incorrect Entity" (tag entity != the user's entity; a
user with no entity gets every tag as Incorrect Entity).

2026-09-11 fix:

The stage lookup was a plain `Device.barcode.in_(tags)` — an exact,
case-sensitive match. Tag numbers get typed into spreadsheets by hand and
come back a different case than the stored barcode (this codebase's
barcodes are inconsistently cased — some lots are upper, some lower), so a
tag sitting in Ready to Sale right now was reported "not found" purely
because of casing. The identical bug was already found and fixed on
/devices' own bulk tag upload (routers/devices.py, func.upper().in_()) —
this pins the same fix here.
"""
import io
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


def _seed_device(barcode, stage="ready_to_sale", entity="ITestEntity"):
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
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     entity={entity!r}, current_stage=DeviceStage.{stage})
        db.add(dev)
        await db.commit()

asyncio.run(main())
""")


def _set_user_entity(username, entity):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import update
from database import AsyncSessionLocal
from models.user import User

async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(update(User).where(User.username == "{username}").values(entity={entity!r}))
        await db.commit()

asyncio.run(main())
""")


def _upload(app_client, make_user, tags, user_entity="ITestEntity"):
    username, password = make_user("admin")
    _set_user_entity(username, user_entity)
    _login(app_client, username, password)
    csrf = app_client.cookies.get("csrf_token") or ""
    csv_body = "tag_number\n" + "".join(t + "\n" for t in tags)
    r = app_client.post(
        "/sales/ready/upload-tags",
        data={"csrf_token": csrf},
        files={"file": ("tags.csv", io.BytesIO(csv_body.encode()), "text/csv")},
    )
    assert r.status_code == 200, r.text[:400]
    return r.json()


def _cleanup(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_upload_tags_matches_regardless_of_case(app_client, make_user):  # noqa: F811
    """A tag typed in different case than the stored barcode still resolves
    to 'found', echoed back in the casing the database stores."""
    barcode = f"ITRTSCASE{uuid.uuid4().hex[:6].upper()}"
    _seed_device(barcode)
    try:
        payload = _upload(app_client, make_user, [barcode.lower()])
        assert payload["not_found"] == [] and payload["incorrect_entity"] == [], payload
        assert barcode in payload["found"], payload
    finally:
        _cleanup(barcode)


def test_upload_tags_selects_any_stage_of_own_entity(app_client, make_user):  # noqa: F811
    """A tag sitting in IQC (or Sold, or any stage) is selectable now."""
    a = f"ITRTSIQC{uuid.uuid4().hex[:6].upper()}"
    b = f"ITRTSSOLD{uuid.uuid4().hex[:6].upper()}"
    _seed_device(a, stage="iqc")
    _seed_device(b, stage="sold")
    try:
        payload = _upload(app_client, make_user, [a, b])
        assert set(payload["found"]) == {a, b}, payload
    finally:
        _cleanup(a); _cleanup(b)


def test_upload_tags_reports_not_found_and_incorrect_entity(app_client, make_user):  # noqa: F811
    other = f"ITRTSENT{uuid.uuid4().hex[:6].upper()}"
    missing = f"ITRTSNOPE{uuid.uuid4().hex[:6].upper()}"
    _seed_device(other, stage="l1", entity="ITestOtherEntity")
    try:
        payload = _upload(app_client, make_user, [other, missing])
        assert payload["found"] == [], payload
        assert payload["not_found"] == [missing], payload
        assert payload["incorrect_entity"] == [other], payload
        assert payload["header"] == ["Barcode", "Entity", "Serial", "Make", "Model", "RAM",
                                     "Storage", "Grade", "Current Stage"]
        row = payload["file_rows"]["incorrect_entity"][0]
        assert row[0] == other and row[1] == "ITestOtherEntity" and row[3] == "ITestBrand", row
        assert payload["file_rows"]["not_found"][0][0] == missing
    finally:
        _cleanup(other)


def test_upload_tags_user_without_entity_gets_every_tag_incorrect(app_client, make_user):  # noqa: F811
    barcode = f"ITRTSNOENT{uuid.uuid4().hex[:6].upper()}"
    _seed_device(barcode)
    try:
        payload = _upload(app_client, make_user, [barcode], user_entity=None)
        assert payload["found"] == [] and payload["incorrect_entity"] == [barcode], payload
    finally:
        _cleanup(barcode)
