"""Part Sales: New parts only (2026-11-09, Pankaj). The Ready to Sale Parts
list already shows only New parts; the server now refuses a Sale Request or a
New Parts Sale for a Harvest part too."""
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from tests.test_return_charge_waiver import _run, ROOT


def _part(code, source):
    return _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        pid = uuid.uuid4()
        await db.execute(text("insert into spare_parts (id, part_code, name, category, unit_price, qty_in_stock, "
                              "min_stock_alert, sold_qty, is_trashed, source) values "
                              "(:i, :c, 'IT New-only part', 'RAM', 100, 5, 1, 0, false, :s)"),
                         {{"i": pid, "c": "{code}", "s": "{source}"}})
        await db.commit()
        print(pid)
asyncio.run(main())
""")


def _requests(pid):
    return int(_run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text("select count(*) from part_sale_requests where part_id = '{pid}'"))).scalar())
asyncio.run(main())
"""))


def _cleanup(codes):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for c in {list(codes)!r}:
            ids = f"(select id from spare_parts where part_code = '{{c}}')"
            await db.execute(text(f"delete from part_sale_requests where part_id in {{ids}}"))
            await db.execute(text(f"delete from spare_parts where part_code = '{{c}}'"))
        await db.commit()
asyncio.run(main())
""")


def test_harvest_part_cannot_be_requested_or_sold(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    new_code, harv_code = f"ITNO{sfx}N", f"ITNO{sfx}H"
    try:
        new_id, harv_id = _part(new_code, "new"), _part(harv_code, "harvest")
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        h = {"X-CSRF-Token": csrf}
        # Harvest: refused, nothing created
        r = app_client.post(f"/ready-to-sale-parts/{harv_id}/request", data={"csrf_token": csrf, "qty": "1"},
                            headers=h, follow_redirects=False)
        assert "Only+New+parts" in r.headers["location"] and _requests(harv_id) == 0
        # New: request goes through as before
        r = app_client.post(f"/ready-to-sale-parts/{new_id}/request", data={"csrf_token": csrf, "qty": "1"},
                            headers=h, follow_redirects=False)
        assert r.status_code == 302 and "error" not in r.headers["location"] and _requests(new_id) == 1
        # Harvest sale: refused before anything is checked or saved
        r = app_client.post("/part-sales/new", data={"csrf_token": csrf, "part_id": harv_id, "qty": "1",
                            "sale_price": "200", "customer_name": "IT"}, headers=h, follow_redirects=False)
        assert "Only+New+parts" in r.headers["location"]
    finally:
        _cleanup([new_code, harv_code])
