"""Cosmetic bulk Assign closes the tag's WorkID for its stage (2026-10-27).

- Cosmetic Received -> Cleaning: the Cosmetic Received ("recv") WorkID is
  completed and a Cleaning ("clean") WorkID opens for the chosen person.
- Reassigning at the same stage: the previous person's WorkID is closed as
  "reassigned"; picking the person who already holds it changes nothing.
Runs in its own process against the local database and removes its rows.
"""
import json
import pathlib
import subprocess
import sys
import uuid

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def test_bulk_assign_closes_previous_cosmetic_workids():
    tag = uuid.uuid4().hex[:6].upper()
    r = subprocess.run([sys.executable, "-c", f"""
import asyncio, json, sys, uuid
sys.path.insert(0, r"{ROOT}")
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text
from main import app
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.work_order import WorkOrder
from models.user import User, UserRole
from auth.dependencies import get_current_user, verify_csrf

RECV, CLEAN = "ZZBA-R{tag}", "ZZBA-C{tag}"

async def wos(db, bc):
    return [(w.stage, w.status, w.assigned_username) for w in (await db.execute(
        select(WorkOrder).where(WorkOrder.barcode == bc).order_by(WorkOrder.created_at))).scalars()]

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        admin = (await db.execute(select(User).where(User.role == UserRole.admin, User.status == True))).scalars().first()
        users = (await db.execute(select(User).where(User.status == True, User.id != admin.id).limit(2))).scalars().all()
        a, b = users[0], users[1]
        d1 = Device(id=uuid.uuid4(), barcode=RECV, lot_id=lot, current_stage=DeviceStage.cosmetic_received)
        d2 = Device(id=uuid.uuid4(), barcode=CLEAN, lot_id=lot, current_stage=DeviceStage.cleaning)
        db.add_all([d1, d2]); await db.flush()
        db.add(WorkOrder(work_id="ZR" + "{tag}", device_id=d1.id, barcode=RECV, stage="recv",
                         assigned_role="x", status="pending", created_by="t"))
        db.add(WorkOrder(work_id="ZC" + "{tag}", device_id=d2.id, barcode=CLEAN, stage="clean",
                         assigned_role="x", assigned_user_id=a.id, assigned_username=a.username,
                         status="pending", created_by="t"))
        await db.commit()
        ids = (d1.id, d2.id)
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[verify_csrf] = lambda: None
    out = {{}}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r1 = await c.post("/cosmetic/bulk-assign", data={{"barcodes": RECV + "," + CLEAN, "engineer_user_id": str(b.id)}})
            out["r1"] = r1.json()
            async with AsyncSessionLocal() as db:
                out["recv"] = await wos(db, RECV); out["clean"] = await wos(db, CLEAN)
            r2 = await c.post("/cosmetic/bulk-assign", data={{"barcodes": CLEAN, "engineer_user_id": str(b.id)}})
            out["r2"] = r2.json()
            async with AsyncSessionLocal() as db:
                out["clean2"] = await wos(db, CLEAN)
        out["a"], out["b"] = a.username, b.username
    finally:
        async with AsyncSessionLocal() as db:
            for bc in (RECV, CLEAN):
                await db.execute(text("delete from notifications where barcode = :b"), {{"b": bc}})
                await db.execute(text("delete from work_orders where barcode = :b"), {{"b": bc}})
            for i in ids:
                await db.execute(text("delete from stage_movements where device_id = :d"), {{"d": i}})
                await db.execute(text("delete from devices where id = :d"), {{"d": i}})
            await db.commit()
    print(json.dumps(out))

asyncio.run(main())
"""], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    a, b = out["a"], out["b"]
    assert out["r1"]["assigned"] == 2 and out["r1"]["moved_to_cleaning"] == 1
    # Cosmetic Received WorkID completed; a Cleaning WorkID opened for b.
    assert out["recv"] == [["recv", "completed", None], ["clean", "pending", b]]
    # a's Cleaning WorkID reassigned; b's opened.
    assert out["clean"] == [["clean", "reassigned", a], ["clean", "pending", b]]
    # Assigning b again: already theirs, nothing new.
    assert out["r2"]["assigned"] == 0 and out["r2"]["already_assigned"] == 1
    assert out["clean2"] == out["clean"]
