"""Device Type — the 12 categories (2026-10-30): Data Fixes tidy-up maps
other spellings / Tiny / Thinclient / Mobile Phone onto the 12 and fixes
Master Data; the Dashboard tiles count all 12 by Device Type."""
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


def test_canonical_device_type():
    from utils.device_types import canonical_device_type, DEVICE_TYPES_12
    assert len(DEVICE_TYPES_12) == 12 and "Workstation" not in DEVICE_TYPES_12
    assert canonical_device_type("PRINTER") == "Printer"
    assert canonical_device_type("Tiny") == "Mini PC" and canonical_device_type("thinclient") == "Mini PC"
    assert canonical_device_type("Mobile Phone") == "Mobile"
    assert canonical_device_type("laptop") == "Laptop" and canonical_device_type("Workstation") is None


def test_device_type_12_fix_and_tiles(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    tags = {f"ITDT{sfx}A": "Tiny", f"ITDT{sfx}B": "PRINTER", f"ITDT{sfx}C": "Mobile Phone"}
    master_before = _run(f"""
import asyncio, sys, uuid, json
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select, text
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        for bc, dt in {tags!r}.items():
            db.add(Device(id=uuid.uuid4(), barcode=bc, lot_id=lot.id, device_type=dt, entity="OxyPC Computers",
                          current_stage=DeviceStage.stock_in))
        await db.commit()
        rows = (await db.execute(text("select value, is_active from master_data where category = 'device_type'"))).all()
        print(json.dumps({{v: a for v, a in rows}}))
asyncio.run(main())
""")
    try:
        u, pw = make_user("admin")
        _login(app_client, u, pw)
        page = app_client.get("/admin/system/data-fixes").text
        assert "Device Type — the 12 categories" in page
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/admin/system/device-type-12-fix", data={"csrf_token": csrf},
                            headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["ok"], r.text
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in {list(tags)!r}:
            print((await db.execute(text(f"select device_type from devices where barcode = '{{bc}}'"))).scalar())
        act = dict((await db.execute(text("select value, is_active from master_data where category = 'device_type'"))).all())
        print(act.get("Projector"), act.get("Workstation", False))
asyncio.run(main())
""").splitlines()
        assert out[:3] == ["Mini PC", "Printer", "Mobile"], out
        assert out[3] in ("True False", "True None"), out
        html = app_client.get("/dashboard?entity=").text
        for name in ("Router", "Printer", "Switch", "Projector", "All-in-One", "Mobile"):
            assert f'<div class="dash-tile-lbl fw-semibold">{name}</div>' in html, name
    finally:
        _run(f"""
import asyncio, sys, json
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for bc in {list(tags)!r}:
            await db.execute(text(f"delete from devices where barcode = '{{bc}}'"))
        before = json.loads({master_before!r})
        await db.execute(text("delete from master_data where category = 'device_type' and value = 'Projector'")) \\
            if "Projector" not in before else None
        for v, a in before.items():
            await db.execute(text("update master_data set is_active = :a where category = 'device_type' and value = :v"),
                             {{"a": a, "v": v}})
        await db.commit()
asyncio.run(main())
""")
