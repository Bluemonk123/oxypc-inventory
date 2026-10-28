"""Daily Stock: Opening excludes tags sold on/before the previous day, Closing =
Opening - tags sold that day, GRN count excluded, sales timed by Sale Date.

Reported 2026-10-07: for 6 Oct the report showed far more stock than expected
(a "Sold" row of ~34k and a "GRN Receipt" row counted as stock, and 772 sales
recorded on 6 Oct but dated 1-5 Oct counted as sold on 6 Oct).
"""
import json
import pathlib
import subprocess
import sys
import uuid

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"subprocess failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip().splitlines()[-1]


# ── pure: the SQL building blocks ────────────────────────────────────────────
def test_reconstruction_times_sold_by_sale_date_without_reordering():
    from routers.reports import _stock_reconstruction_sql
    sql = _stock_reconstruction_sql(False)
    assert "sl.sale_number = substring(sm.notes from 'SALE-[0-9]+')" in sql
    assert "GREATEST(sl.sold_at" in sql and "LAG(sm.moved_at)" in sql     # never earlier than the previous move
    assert "ORDER BY ev.device_id, ev.ev_at DESC, ev.moved_at DESC" in sql
    assert "ev.ev_at <= :as_of" in _stock_reconstruction_sql(False)
    assert "ev.ev_at <= :open_as_of" in _stock_reconstruction_sql(False, "open_as_of")


def test_scope_drops_sold_and_grn_and_restricts_closing_to_the_opening_set():
    from routers.reports import _stock_scope_sql
    opening = _stock_scope_sql(False, None)
    assert "NOT IN ('sold', 'grn')" in opening and "open_as_of" not in opening
    closing = _stock_scope_sql(False, "open_as_of")
    assert "NOT IN ('sold', 'grn')" in closing
    assert "latest.device_id IN (" in closing and ":open_as_of" in closing


# ── DB-backed, isolated by a unique Entity value ─────────────────────────────
SCRIPT = r'''
import asyncio, json, sys
from datetime import datetime
sys.path.insert(0, r"{root}")
from sqlalchemy import select, text
from database import AsyncSessionLocal, engine
from models.lot import Lot
from models.device import Device, DeviceStage, StageMovement
from models.sales import Sale
import routers.reports as rp

ENT = "{ent}"
D = lambda s: datetime.fromisoformat(s)
DAY0, DAY1 = D("2026-10-06T00:00:00"), D("2026-10-07T00:00:00")

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        def dev(code, stage):
            d = Device(barcode=f"{{ENT}}-{{code}}", lot_id=lot.id, brand="Lenovo", model="T490", entity=ENT, current_stage=stage,
                       device_type="Laptop", cpu="i5-8350U", ram_gb=8, storage_gb=256, storage_type="SSD")
            db.add(d); return d
        A, B, C, E, N = dev("A", DeviceStage.sold), dev("B", DeviceStage.sold), dev("C", DeviceStage.ready_to_sale), \
                        dev("E", DeviceStage.grn), dev("N", DeviceStage.stock_in)
        T, X = dev("T", DeviceStage.ready_to_sale), dev("X", DeviceStage.ready_to_sale)
        T.is_active = False          # deactivated tag
        X.is_trashed = True          # trashed tag
        await db.flush()
        def mv(d, frm, to, at, note=None):
            db.add(StageMovement(device_id=d.id, from_stage=frm, to_stage=to, moved_by="itest", moved_at=D(at), notes=note))
        R = DeviceStage.ready_to_sale
        # A: ready since 1 Oct; sale DATED 3 Oct but keyed in on 6 Oct  -> sold before the day
        mv(A, DeviceStage.l1, R, "2026-10-01T10:00:00"); mv(A, R, DeviceStage.sold, "2026-10-06T11:00:00", "Sold — SALE-{n}1")
        db.add(Sale(sale_number="SALE-{n}1", device_id=A.id, sale_price=1, sold_by="itest", sold_at=D("2026-10-03T11:00:00")))
        # B: ready since 1 Oct; sold on 6 Oct (sale date 6 Oct)       -> the day's sale
        mv(B, DeviceStage.l1, R, "2026-10-01T10:00:00"); mv(B, R, DeviceStage.sold, "2026-10-06T12:00:00", "Sold — SALE-{n}2")
        db.add(Sale(sale_number="SALE-{n}2", device_id=B.id, sale_price=1, sold_by="itest", sold_at=D("2026-10-06T12:00:00")))
        # C: ready since 1 Oct, never sold                              -> in both
        mv(C, DeviceStage.l1, R, "2026-10-01T10:00:00")
        # E: GRN Receipt stage                                          -> not stock
        mv(E, None, DeviceStage.grn, "2026-10-02T10:00:00")
        # N: first received during the day (GRN)                        -> not in Closing (not in Opening)
        mv(N, None, DeviceStage.stock_in, "2026-10-06T15:00:00")
        # T deactivated, X trashed: in stock per the movement log, but not stock anywhere else -> excluded
        mv(T, DeviceStage.l1, R, "2026-10-01T10:00:00"); mv(X, DeviceStage.l1, R, "2026-10-01T10:00:00")
        await db.commit()
        try:
            o = await rp._stock_as_of(db, DAY0, False, [ENT])
            c = await rp._stock_as_of(db, DAY1, False, [ENT], open_as_of=DAY0)
            sold = await rp._sold_in_day(db, DAY0, DAY1, False, [ENT])
            tags_o = [r[1] for r in await rp._stock_tags_as_of(db, DAY0, False, [ENT])]
            tags_c = [r[1] for r in await rp._stock_tags_as_of(db, DAY1, False, [ENT], open_as_of=DAY0)]
            csv_text = await rp.build_tags_csv(db, DAY0.date(), False, [ENT])
            print(json.dumps({{"csv": csv_text, "open": o, "close": c, "sold": sold,
                              "tags_open": sorted(t.split("-")[-1] for t in tags_o),
                              "tags_close": sorted(t.split("-")[-1] for t in tags_c)}}))
        finally:
            ids = [d.id for d in (A, B, C, E, N, T, X)]
            await db.execute(text("delete from sales where device_id = any(:i)"), {{"i": ids}})
            await db.execute(text("delete from stage_movements where device_id = any(:i)"), {{"i": ids}})
            await db.execute(text("delete from devices where id = any(:i)"), {{"i": ids}})
            await db.commit()
    await engine.dispose()

asyncio.run(main())
'''


def test_opening_closing_sold_and_grn_rules_on_real_rows():
    ent = "ITDS" + uuid.uuid4().hex[:6]
    n = str(uuid.uuid4().int)[:8]
    out = json.loads(_run(SCRIPT.format(root=ROOT, ent=ent, n=n)))
    # Opening: A (sale dated 3 Oct), E (GRN stage), T (deactivated), X (trashed) are not stock; B and C are.
    assert out["tags_open"] == ["B", "C"]
    assert out["open"] == {"ready_to_sale": 2}
    # Closing = Opening - the day's sale (B). N (first received that day) is not added.
    assert out["tags_close"] == ["C"]
    assert out["close"] == {"ready_to_sale": 1}
    assert out["sold"] == 1
    # Tag Based export: both snapshots carry the tag details
    import csv, io
    rows = list(csv.reader(io.StringIO(out["csv"])))
    assert rows[0] == ["Report Date", "Snapshot", "Stage", "Tag Number", "Category", "Make", "Model",
                       "CPU", "RAM", "Storage", "Grade", "Entity"]
    body = {(r[1], r[3].split("-")[-1]): r for r in rows[1:]}
    assert set(body) == {("Opening", "B"), ("Opening", "C"), ("Closing", "C")}
    for r in body.values():
        assert r[4:8] == ["Laptop", "Lenovo", "T490", "i5-8350U"] and r[8:10] == ["8 GB", "256 GB SSD"]
        assert r[10] == "" and r[11] == r[3].rsplit("-", 1)[0]       # grade not set; entity = the test entity


def test_page_shows_opening_minus_sold_equals_closing(monkeypatch):
    import types
    from templates_config import templates
    from models.user import UserRole
    monkeypatch.setitem(templates.env.globals, "master_options", lambda cat: ["OxyPC Computers"])
    tpl = templates.env.get_template("reports/daily_stock.html")
    user = types.SimpleNamespace(username="admin", role=UserRole.admin, full_name="Admin", is_active=True, id=1)
    html = tpl.render({
        "request": types.SimpleNamespace(url=types.SimpleNamespace(path="/reports/daily-stock"),
                                         query_params={}, session={}, cookies={}),
        "current_user": user, "selected_date": "2026-10-06", "exclude_admin": False, "selected_entity": "",
        "entity_options": ["OxyPC Computers"],
        "rows": [{"stage": "ready_to_sale", "label": "Ready to Sale", "opening": 13888, "closing": 12324, "net": -1564}],
        "total_open": 13888, "total_close": 12324, "total_net": -1564, "sold_today": 1561, "is_today": False,
    })
    assert "Sold on 2026-10-06" in html and "1561" in html
    assert "Other" in html            # 13888 - 1561 = 12327, closing 12324 -> 3 "other" (e.g. trashed)
    assert "excluding tags sold on or before the previous day" in html
