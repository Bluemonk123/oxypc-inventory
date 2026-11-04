"""Customise modal Model Name (2026-11-04): full-width field on top that sets
Device.model on every selected tag. Ready to Sale Tag Wise Table opens with
OxyPC Computers + Renew Circuits ticked in the Entity filter."""
import uuid

from tests.test_customise_ram_storage import _run, ROOT
from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def test_customise_sets_model_name(app_client, make_user):  # noqa: F811
    bc = "ITCMN" + uuid.uuid4().hex[:8].upper()
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        db.add(Device(id=uuid.uuid4(), barcode="{bc}", lot_id=lot, current_stage=DeviceStage.ready_to_sale,
                      brand="HP", model="Old Model"))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, pw = make_user("admin")
        _login(app_client, u, pw)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/iqc/bulk-apply-grade-type", data={
            "csrf_token": csrf, "barcodes": bc, "return_to": "/devices",
            "model_name": "  EliteBook 840 G6  "}, headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        assert r.status_code == 302 and "error" not in r.headers["location"], r.headers.get("location")
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select model from devices where barcode = '{bc}'"))).scalar())
asyncio.run(main())
""")
        assert out == "EliteBook 840 G6", out
        m = app_client.get("/devices").text.split('id="customiseForm"', 1)[1]
        assert m.index('name="model_name"') < m.index('name="device_type"')
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode = '{bc}'"))
        await db.commit()
asyncio.run(main())
""")


def test_ready_tag_table_default_entities(app_client, make_user):  # noqa: F811
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    html = app_client.get("/sales/ready").text
    tag = html.split('id="ms_val_entity"', 1)[0].rsplit("<input", 1)[1] + html.split('id="ms_val_entity"', 1)[1][:80]
    assert "OxyPC Computers" in tag and "Renew Circuits" in tag, tag
