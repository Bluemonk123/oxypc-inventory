"""One WorkID per stage visit — L1/L2, L3/L4, Stress Test, Final QC (2026-10-27).

services/stage_work_orders.py: arrival opens one pending WorkID (Movement
Date = created_at = assigned_at); Pick This claims it (assigned_at = pick
time, created_at kept) or, if someone else holds it, closes theirs as
COMPLETED and opens a new one; Complete / Pass / Fail closes it. Also checks
the routers call the helpers on every path. DB part runs in its own process
and rolls back.
"""
import inspect
import json
import pathlib
import subprocess
import sys

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def test_every_path_uses_the_shared_helpers():
    import routers.cosmetic as cos
    import routers.iqc as iqc
    import routers.repair as rep
    import routers.stress_api as sa
    assert "_pick(db, device, DeviceStage.l1, current_user)" in inspect.getsource(rep.l1_pick)
    assert "_pick(db, device, DeviceStage.l3, current_user)" in inspect.getsource(rep.l3l4_pick)
    assert inspect.getsource(sa).count("_finish_stage(db, device, DeviceStage.qc_check, current_user)") == 2
    assert "_pick(db, device, DeviceStage.qc_check, current_user)" in inspect.getsource(sa.stress_pick)
    assert "_finish_stage(db, device, DeviceStage.final_qc, current_user)" in inspect.getsource(cos.advance_stage)
    assert "_open_for_stage(db, device, DeviceStage.final_qc" in inspect.getsource(cos.advance_stage)
    assert "_pick(db, device, DeviceStage.final_qc, current_user)" in inspect.getsource(cos.fqc_pick)
    assert "open_for_stage(db, device, new_stage" in inspect.getsource(iqc)
    assert "_open_for_stage(db, device, new_stage" in inspect.getsource(rep.move_device)


def test_lifecycle():
    r = subprocess.run([sys.executable, "-c", f"""
import asyncio, json, sys, uuid, types
from datetime import timedelta
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select, text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.work_order import WorkOrder
from services import stage_work_orders as swo
from services.work_order_close import reuse_open_stress_work_order
from utils.timezone import app_now

def U(uid, name):
    return types.SimpleNamespace(id=uid, username=name, full_name=name.title(), role="l1_engineer")

async def main():
    out = {{}}
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        ids = (await db.execute(text("select id from users limit 3"))).scalars().all()
        a, b, c = U(ids[0], "eng_a"), U(ids[1], "eng_b"), U(ids[2], "eng_c")
        t0 = app_now() - timedelta(hours=5)
        d = Device(id=uuid.uuid4(), barcode="ZZSWO-" + uuid.uuid4().hex[:6], lot_id=lot,
                   current_stage=DeviceStage.l1)
        db.add(d); await db.flush()

        # arrive: one pending, unassigned; a second arrival call reuses it
        w1 = await swo.open_for_stage(db, d, DeviceStage.l1, created_by="t", at=t0)
        w1b = await swo.open_for_stage(db, d, DeviceStage.l1, created_by="t")
        out["arrive"] = [w1.status, w1.assigned_user_id, w1.created_at == t0, w1.assigned_at == t0, w1 is w1b]

        # pick: claims the same WorkID; assigned date moves, movement date stays
        p, prev = await swo.pick(db, d, DeviceStage.l1, a)
        out["claim"] = [p is w1, p.assigned_username, p.created_at == t0, p.assigned_at > t0, prev]

        # picking your own tag: no-op
        out["own"] = list(await swo.pick(db, d, DeviceStage.l1, a)) == [None, []]

        # someone else picks: a's closes COMPLETED, new one for b
        p2, prev2 = await swo.pick(db, d, DeviceStage.l1, b)
        out["take"] = [w1.status, w1.completed_at is not None, p2 is not w1, p2.assigned_username,
                       [x[2] for x in prev2]]

        # reassignment (bucket / Change Engineer path) to c: same rule
        p3 = await reuse_open_stress_work_order(db, d.id, DeviceStage.l1, c, created_by="t")
        out["reassign"] = [p2.status, p3.assigned_username, p3.status]

        # complete: closes the same WorkID, keeps the engineer
        f = await swo.finish(db, d, DeviceStage.l1, a)
        out["finish"] = [f is p3, f.status, f.assigned_username]

        # Stress: arrive unassigned, finish credits the submitter (not picked)
        s1 = await swo.open_for_stage(db, d, DeviceStage.qc_check, created_by="t")
        s2 = await swo.finish(db, d, DeviceStage.qc_check, b)
        out["stress"] = [s1.work_id.startswith("STRS-"), s2 is s1, s1.status, s1.assigned_username]

        # Final QC: picked by a, decided by c -> stays a's; none open -> recorded for c
        q1 = await swo.open_for_stage(db, d, DeviceStage.final_qc, created_by="t")
        await swo.pick(db, d, DeviceStage.final_qc, a)
        q2 = await swo.finish(db, d, DeviceStage.final_qc, c)
        q3 = await swo.finish(db, d, DeviceStage.final_qc, c)
        out["fqc"] = [q2 is q1, q1.status, q1.assigned_username, q3 is not q1, q3.status, q3.assigned_username]

        # L3/L4 pick leaves the tag's open L1/L2 WorkID alone
        l1 = await swo.open_for_stage(db, d, DeviceStage.l1, assignee=a, created_by="t")
        await swo.open_for_stage(db, d, DeviceStage.l3, created_by="t")
        l3, _ = await swo.pick(db, d, DeviceStage.l3, b)
        out["l3"] = [l3.work_id.startswith("L3L4-"), l3.assigned_username, l1.status]

        # a cosmetic stage is not handled here
        out["cosmetic"] = await swo.open_for_stage(db, d, DeviceStage.cleaning, created_by="t") is None
        await db.rollback()
    print(json.dumps(out, default=str))

asyncio.run(main())
"""], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["arrive"] == ["pending", None, True, True, True]
    assert o["claim"] == [True, "eng_a", True, True, []]
    assert o["own"] is True
    assert o["take"] == ["completed", True, True, "eng_b", ["Eng_A"]]
    assert o["reassign"] == ["completed", "eng_c", "pending"]
    assert o["finish"] == [True, "completed", "eng_c"]
    assert o["stress"] == [True, True, "completed", "eng_b"]
    assert o["fqc"] == [True, "completed", "eng_a", True, "completed", "eng_c"]
    assert o["l3"] == [True, "eng_b", "pending"]
    assert o["cosmetic"] is True
