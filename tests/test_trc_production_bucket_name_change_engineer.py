"""Production Manager (/trc-production), 2026-09-18:
 - Tag Number Allocation table gains a "Bucket Name" column after Lot.
 - Tag Number Allocation's Bulk Assign is rewired onto the shared Global
   Table module (persistent Set-based selection across pages/search, scan-
   to-select, selected count on the button) — closing the real bug behind
   "only works one at a time": checkbox state for off-page rows was lost on
   every DataTables redraw.
 - New "Change Engineer" button/modal: search a tag from ANY stage, pick a
   User + target Stage (default L1), submit moves the tag and creates a
   WorkOrder — which then shows up on /workid-status like any other one.
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


def _seed_device_in_bucket(barcode, bucket_number, bucket_name, stage="trc_production"):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.bucket import Bucket

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        bucket = Bucket(bucket_number="{bucket_number}", name="{bucket_name}")
        db.add(bucket)
        await db.flush()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.{stage}, bucket_id=bucket.id)
        db.add(dev)
        await db.commit()
        print(dev.id)

asyncio.run(main())
""")


def _cleanup(barcode, bucket_number):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.bucket import Bucket
from models.work_order import WorkOrder
from models.device import StageMovement

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for wo in (await db.execute(select(WorkOrder).where(WorkOrder.device_id == dev.id))).scalars().all():
                await db.delete(wo)
            for mv in (await db.execute(select(StageMovement).where(StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(mv)
            await db.delete(dev)
        bkt = (await db.execute(select(Bucket).where(Bucket.bucket_number == "{bucket_number}"))).scalar_one_or_none()
        if bkt:
            await db.delete(bkt)
        await db.commit()

asyncio.run(main())
""")


def test_bucket_name_column_present_and_populated(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITBKTNAME{suffix}"
    bucket_number = f"ITBKT{suffix}"
    bucket_name = f"ITestBucketName{suffix}"
    _seed_device_in_bucket(barcode, bucket_number, bucket_name)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/trc-production", follow_redirects=True).text
        assert "<th>Bucket Name</th>" in html
        # Column header appears before Brand (right after Lot), matching the
        # requested "after Lot column" placement.
        assert html.index("<th>Bucket Name</th>") < html.index("<th>Brand</th>")
        assert bucket_name in html
    finally:
        _cleanup(barcode, bucket_number)


def test_global_table_wired_onto_tag_allocation_table(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/trc-production", follow_redirects=True).text
    assert "initGlobalTable('#trcTable'" in html
    # Persistent-selection machinery, not the old raw checkbox-only wiring.
    assert "trcSelected" in html
    assert "updateTrcBulkAssignButtonState" in html
    assert "scan: { inputId: 'trcScanInput'" in html
    assert "selectAll: { headerSelector: '#trcSelectAll'" in html


def test_change_engineer_button_and_modal_present(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/trc-production", follow_redirects=True).text
    assert "Move to Stage" in html   # renamed from Change Engineer (2026-11-02)
    assert 'id="changeEngineerModal"' in html
    assert 'id="ceModal_user"' not in html   # no user pick any more
    assert 'id="ceModal_stage"' in html
    # Default target stage is L1 per spec.
    assert '<option value="l1" selected>' in html


def test_change_engineer_users_endpoint_returns_active_users(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    r = app_client.get("/api/change-engineer-users")
    assert r.status_code == 200
    names = [u["name"] for u in r.json()]
    assert any(username in n or n for n in names)  # non-empty, sane shape
    assert len(r.json()) >= 1


def test_change_engineer_moves_stage_creates_workorder_visible_on_workid_status(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITCHENG{suffix}"
    bucket_number = f"ITBKTCE{suffix}"
    _seed_device_in_bucket(barcode, bucket_number, "ITest CE Bucket", stage="qc_check")
    try:
        admin_username, admin_password = make_user("admin")
        eng_username, eng_password = make_user("l1_engineer")
        _login(app_client, admin_username, admin_password)

        eng_id = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.user import User

async def main():
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "{eng_username}"))).scalar_one()
        print(u.id)

asyncio.run(main())
""")

        # Confirm the engineer actually appears in the assignable-users list.
        users = app_client.get("/api/change-engineer-users").json()
        assert any(u["id"] == eng_id for u in users)

        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/devices/change-engineer", data={
            "csrf_token": csrf, "barcode": barcode, "assigned_user_id": eng_id, "target_stage": "l1",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        work_id = body["work_id"]

        # Device actually moved to L1.
        stage = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        print(dev.current_stage.value)

asyncio.run(main())
""")
        assert stage == "l1"

        # New WorkID shows up on /workid-status (assigned to this admin's
        # own visibility — admin sees every WorkOrder).
        wid_html = app_client.get(f"/workid-status?workid={work_id}", follow_redirects=True).text
        assert work_id in wid_html
        assert barcode in wid_html
    finally:
        _cleanup(barcode, bucket_number)


def test_move_to_stage_unassigned_opens_pickable_workid(app_client, make_user):  # noqa: F811
    """Move to Stage (2026-11-02): no user — the tag moves, one pending
    unassigned WorkID opens for the visit (to be picked), and the previous
    open WorkID closes as reassigned."""
    suffix = uuid.uuid4().hex[:6]
    barcode = f"ITMTS{suffix}"
    bucket_number = f"ITBKTMS{suffix}"
    _seed_device_in_bucket(barcode, bucket_number, "ITest MTS Bucket", stage="l1")
    try:
        admin_username, admin_password = make_user("admin")
        eng_username, _p = make_user("l1_engineer")
        _login(app_client, admin_username, admin_password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        eng_id = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.user import User
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(select(User.id).where(User.username == "{eng_username}"))).scalar_one())
asyncio.run(main())
""")
        # first hand it to an engineer at L1/L2 (old path), then Move to L3/L4 unassigned
        r = app_client.post("/devices/change-engineer", data={
            "csrf_token": csrf, "barcode": barcode, "assigned_user_id": eng_id, "target_stage": "l3"})
        assert r.status_code == 200, r.text
        r = app_client.post("/devices/change-engineer", data={
            "csrf_token": csrf, "barcode": barcode, "target_stage": "l1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["work_id"]
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one()
        wos = (await db.execute(select(WorkOrder).where(WorkOrder.device_id == dev.id))).scalars().all()
        new = [w for w in wos if w.work_id == "{body['work_id']}"][0]
        old_open = [w for w in wos if w.work_id != new.work_id and w.status not in ("completed", "reassigned")]
        print(dev.current_stage.value, new.status, new.assigned_user_id is None, len(old_open))
asyncio.run(main())
""")
        assert out == "l1 pending True 0", out
        # moving again to the same stage is refused
        r = app_client.post("/devices/change-engineer", data={
            "csrf_token": csrf, "barcode": barcode, "target_stage": "l1"})
        assert r.status_code == 400
    finally:
        _cleanup(barcode, bucket_number)


def test_summary_tiles_row_never_wraps(app_client, make_user):  # noqa: F811
    """2026-09-19: 7 tiles (Cosmetic added the same day) must stay on one
    row -- same flex-nowrap-and-shrink approach as Dashboard's Stage
    Pipeline, not the old col-6 col-xl-2 grid that wrapped to two rows."""
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/trc-production", follow_redirects=True).text
    assert "prod-tiles-row" in html
    assert "flex-wrap:nowrap" in html.replace(" ", "")


def test_change_engineer_modal_has_asset_history_table(app_client, make_user):  # noqa: F811
    """2026-09-19: picking a tag in the Change Engineer modal now also loads
    its 2 most recent Asset History rows (same /devices/api/asset-history
    endpoint _search_tags_modal.html already uses), not just Current Stage."""
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/trc-production", follow_redirects=True).text
    assert 'id="ceModal_history"' in html
    assert "loadCeHistory" in html
    assert "/devices/api/asset-history?barcode=" in html
    # Capped to 2 rows client-side.
    assert "slice(0, 2)" in html
