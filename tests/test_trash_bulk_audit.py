"""Trash safeguards (2026-11-04): 555 tags went to Trash on 2-4 Nov 2026 in
three bulk clicks (349 sold, with sale records) and nothing recorded who did it.
Now every trash / restore is audit-logged; a bulk trash of more than 20 tags
needs a reason; tags with a sale or return record need an explicit confirm."""
import pathlib
import subprocess
import sys
import uuid
from urllib.parse import urlencode

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def _post(client, url, pairs):
    csrf = client.cookies.get("csrf_token") or "dummy"
    return client.post(url, content=urlencode([("csrf_token", csrf)] + pairs),
                       headers={"X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded"},
                       follow_redirects=False)


def _q(sql):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text({sql!r}))).scalar())
asyncio.run(main())
""")


def test_bulk_trash_reason_sale_confirm_and_audit(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    tags = [f"ITTR{sfx}L{i:04d}" for i in range(22)]
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.sales import Sale
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        for i, t in enumerate({tags!r}):
            d = Device(id=uuid.uuid4(), barcode=t, lot_id=lot,
                       current_stage=DeviceStage.sold if i < 2 else DeviceStage.ready_to_sale,
                       device_type="Laptop", brand="Dell", model="Latitude 7490")
            db.add(d)
            await db.flush()
            if i < 2:
                db.add(Sale(sale_number=f"ITTR{sfx}S{{i}}", device_id=d.id, sale_price=1000))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, pw = make_user("admin")
        _login(app_client, u, pw)
        bc = [("barcodes", t) for t in tags]

        c = _post(app_client, "/trash/bulk/devices/check", bc).json()
        assert c["will_trash"] == 22 and c["with_sale"] == 2 and c["need_reason"] is True
        assert c["stages"] == {"sold": 2, "ready_to_sale": 20}

        # >20 tags without a reason: refused, nothing moved
        r = _post(app_client, "/trash/bulk/devices", bc + [("confirm_sale", "1")])
        assert r.status_code == 302 and "error=" in r.headers["location"]
        # reason but sale tags not confirmed: refused
        r = _post(app_client, "/trash/bulk/devices", bc + [("reason", "test cleanup")])
        assert "error=" in r.headers["location"]
        assert _q(f"select count(*) from devices where barcode like 'ITTR{sfx}%' and is_trashed") == "0"

        r = _post(app_client, "/trash/bulk/devices", bc + [("reason", "test cleanup"), ("confirm_sale", "1")])
        assert "success=22" in r.headers["location"], r.headers["location"]
        assert _q(f"select count(*) from devices where barcode like 'ITTR{sfx}%' and is_trashed") == "22"
        assert _q(f"select count(*) from audit_logs where action = 'DEVICES_TRASHED_BULK' "
                  f"and notes = 'test cleanup' and new_value like '%{tags[-1]}%'") == "1"
        # Trashed By column (2026-11-04) from the audit log, reason in the tooltip;
        # the success message is shown once (base.html), not again by the page.
        html = app_client.get("/trash?success=Hello+once").text
        row = html.split(f">{tags[-1]}</code>", 1)[1].split("</tr>", 1)[0]
        assert "Reason: test cleanup" in row and u in row, row[:600]
        assert "<th>Trashed By</th><th>Trashed On</th>" in html
        assert html.count("Hello once") == 1

        # single restore + single trash are audited too
        r = _post(app_client, f"/trash/devices/{tags[5]}/restore", [])
        assert r.status_code == 302
        r = _post(app_client, f"/trash/devices/{tags[5]}", [])
        assert r.status_code == 302
        assert _q(f"select count(*) from audit_logs where action in ('DEVICE_RESTORED','DEVICE_TRASHED') "
                  f"and record_id = '{tags[5]}'") == "2"

        # small bulk (<=20) with no sale tags needs no reason
        r = _post(app_client, f"/trash/bulk/devices/restore", [("barcodes", t) for t in tags[2:5]])
        r = _post(app_client, "/trash/bulk/devices", [("barcodes", t) for t in tags[2:5]])
        assert "success=3" in r.headers["location"], r.headers["location"]
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "(select id from devices where barcode like 'ITTR{sfx}%')"
        await db.execute(text(f"delete from sales where device_id in {{ids}}"))
        await db.execute(text("delete from devices where barcode like 'ITTR{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")


def test_trash_respects_all_inventories_delete_tick(app_client, make_user):  # noqa: F811
    """Delete unticked on All Inventories but ticked on Product IQC (Inventory
    Manager on production, 4 Nov 2026): trashing a tag from All Inventories
    must be refused, and the row Trash button hidden."""
    from models.role_permissions import set_cached_perms
    from auth.route_actions import resolve_module
    assert resolve_module('POST', '/trash/devices/{barcode}', ('devices', 'iqc'), {},
                          'http://x/devices?lot=1') == ('devices',)
    assert resolve_module('POST', '/trash/devices/{barcode}', ('devices', 'iqc'), {},
                          'http://x/iqc/') == ('iqc',)
    assert resolve_module('POST', '/trash/devices/{barcode}', ('devices', 'iqc'), {}, None) == ('devices',)

    # the real role (only admin / inventory_manager reach /trash routes at all)
    import copy
    from models.role_permissions import _PERM_CACHE
    role = "inventory_manager"
    saved = copy.deepcopy(_PERM_CACHE.get(role))
    u, pw = make_user(role)
    _login(app_client, u, pw)
    sfx = uuid.uuid4().hex[:6].upper()
    tag = f"ITTD{sfx}L0001"
    _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        db.add(Device(id=uuid.uuid4(), barcode="{tag}", lot_id=lot, current_stage=DeviceStage.ready_to_sale,
                      device_type="Laptop", brand="Dell", model="Latitude 7490"))
        await db.commit()
asyncio.run(main())
""")
    try:
        set_cached_perms(role, {"devices": {"enable": True, "delete": False},
                                "iqc": {"enable": True, "delete": True}})
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        hdr = {"X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded",
               "Referer": "http://testserver/devices"}
        r = app_client.post(f"/trash/devices/{tag}", content=urlencode([("csrf_token", csrf)]),
                            headers=hdr, follow_redirects=False)
        assert "error=" in r.headers.get("location", ""), (r.status_code, r.headers.get("location"))
        r = app_client.post("/trash/bulk/devices", content=urlencode([("csrf_token", csrf), ("barcodes", tag)]),
                            headers=hdr, follow_redirects=False)
        assert "error=" in r.headers.get("location", "")
        assert _q(f"select count(*) from devices where barcode = '{tag}' and is_trashed") == "0"
        d = app_client.get(f"/devices/data?draw=1&start=0&length=5&search[value]={tag}").text
        assert "trash-one-btn" not in d and tag in d
    finally:
        if saved is None:
            _PERM_CACHE.pop(role, None)
        else:
            set_cached_perms(role, saved)
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from devices where barcode = '{tag}'"))
        await db.commit()
asyncio.run(main())
""")
