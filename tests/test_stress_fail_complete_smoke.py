"""Smoke test — Stress Test page 'Fail' and 'Complete' assignment endpoints.

Follows the same throwaway-verification pattern used by verify_*.py scripts at
the repo root (AsyncClient + dependency_overrides on get_current_user/get_db),
but lives in tests/ as a pytest-discoverable async test (pytest.ini sets
asyncio_mode = auto, so plain `async def test_*` functions run without any
extra fixture).

Covers:
  POST /stress/{barcode}/fail              -> assigns device to an L1 engineer,
                                               AND self-attributes a completed
                                               "strs" WorkID to whoever submitted
  POST /stress/{barcode}/complete-to-paint  -> sends device to Cleaning, assigned
                                               to a Paint/Cleaning-pool engineer,
                                               AND self-attributes a completed
                                               "strs" WorkID to whoever submitted

Each device now carries TWO WorkOrders after either endpoint: the downstream
assignment (stage "l1" or "recv") and the self-attribution (stage "strs",
same pattern as Final QC's decision WorkID in routers/cosmetic.py) — tests
below filter by stage explicitly rather than relying on an unordered .first().

Creates its own Lot/Device/User fixtures and cleans them up at the end so it
can run repeatedly against a real dev DB without leaving residue.
"""
import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from main import app
from database import get_db, AsyncSessionLocal
from auth.dependencies import get_current_user
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement
from models.lot import Lot
from models.work_order import WorkOrder
from services.stage_work_orders import stage_clause
from models.stress import StressTestResult, STRESS_CHECKLIST_ITEMS


class FakeAdmin:
    id = None
    username = "verify_admin"
    role = UserRole.admin
    status = True
    full_name = "Verify Admin"


_CURRENT_USER = {"user": None}


async def _override_user():
    return _CURRENT_USER["user"]


async def _override_db():
    async with AsyncSessionLocal() as db:
        yield db


@pytest.mark.asyncio
async def test_stress_fail_and_complete_assign():
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    _CURRENT_USER["user"] = FakeAdmin()

    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        assert lot, "Need at least one Lot in the DB to attach a test device to"

        l1_engineer = (await db.execute(
            select(User).where(User.role == UserRole.l1_engineer, User.status == True)
        )).scalars().first()
        paint_engineer = (await db.execute(
            select(User).where(User.role.in_(
                [UserRole.qc_inspector, UserRole.inventory_manager, UserRole.sales_manager]
            ), User.status == True)
        )).scalars().first()

        device_fail = Device(
            barcode=f"VSF-{uuid.uuid4().hex[:8]}", lot_id=lot.id,
            brand="VerifyBrand", model="VerifyModel",
            current_stage=DeviceStage.qc_check,
        )
        device_complete = Device(
            barcode=f"VSC-{uuid.uuid4().hex[:8]}", lot_id=lot.id,
            brand="VerifyBrand", model="VerifyModel",
            current_stage=DeviceStage.qc_check,
            # Pre-set, so /complete-to-paint's clearing behavior is provable —
            # a device coming from an earlier failed run must not still show
            # a stale failure note once it fully passes.
            stress_notes="Failed: Keyboard — stale note from an earlier run",
        )
        db.add(device_fail)
        db.add(device_complete)
        await db.commit()
        fail_barcode = device_fail.barcode
        complete_barcode = device_complete.barcode
        fail_device_id = device_fail.id
        complete_device_id = device_complete.id

    transport = ASGITransport(app=app, raise_app_exceptions=True)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # verify_csrf requires the form's csrf_token to match the
            # csrf_token cookie (double-submit pattern) — set both to the
            # same dummy value so requests reach the real handler instead of
            # being redirected to /auth/login for a "missing cookie".
            c.cookies.set("csrf_token", "smoketest-csrf")
            # ── Fail: assign to L1/L2 engineer (or confirm expected 4xx if no
            # engineer is seeded — either way this proves no 500). ──────────
            if l1_engineer:
                r = await c.post(
                    f"/stress/{fail_barcode}/fail",
                    data={"engineer_user_id": str(l1_engineer.id), "notes": "smoke test",
                          "failed_items": "Keyboard,Touchpad",
                          "csrf_token": "smoketest-csrf"},
                    follow_redirects=False,
                )
                assert r.status_code in (302, 303, 307), f"/fail returned {r.status_code}: {r.text[:300]}"
            else:
                r = await c.post(
                    f"/stress/{fail_barcode}/fail",
                    data={"engineer_user_id": str(uuid.uuid4()), "notes": "smoke test",
                          "csrf_token": "smoketest-csrf"},
                    follow_redirects=False,
                )
                assert r.status_code in (400, 404), f"/fail returned {r.status_code}: {r.text[:300]}"

            # ── Complete: assign to Paint/Cleaning-pool engineer ─────────────
            if paint_engineer:
                r2 = await c.post(
                    f"/stress/{complete_barcode}/complete-to-paint",
                    data={"engineer_user_id": str(paint_engineer.id), "notes": "smoke test",
                          "csrf_token": "smoketest-csrf"},
                    follow_redirects=False,
                )
                assert r2.status_code in (302, 303, 307), (
                    f"/complete-to-paint returned {r2.status_code}: {r2.text[:300]}"
                )
            else:
                r2 = await c.post(
                    f"/stress/{complete_barcode}/complete-to-paint",
                    data={"engineer_user_id": str(uuid.uuid4()), "notes": "smoke test",
                          "csrf_token": "smoketest-csrf"},
                    follow_redirects=False,
                )
                assert r2.status_code in (400, 404), (
                    f"/complete-to-paint returned {r2.status_code}: {r2.text[:300]}"
                )

        # ── Assertions on DB side-effects when an engineer was available ────
        async with AsyncSessionLocal() as db:
            if l1_engineer:
                dev = (await db.execute(select(Device).where(Device.id == fail_device_id))).scalar_one()
                assert dev.current_stage in (DeviceStage.l1, DeviceStage.l2), (
                    f"Device not moved to L1/L2, still at {dev.current_stage}"
                )
                wo = (await db.execute(
                    select(WorkOrder).where(WorkOrder.device_id == fail_device_id,
                                             WorkOrder.stage == "l1")
                )).scalars().first()
                assert wo is not None, "No WorkOrder created for /fail assignment"
                assert wo.assigned_user_id == l1_engineer.id

                strs_wo = (await db.execute(
                    # One Stress WorkID per visit (2026-10-27): the result closes the
                    # tag's Stress WorkID, or records one when none was open.
                    select(WorkOrder).where(WorkOrder.device_id == fail_device_id,
                                             stage_clause(DeviceStage.qc_check),
                                             WorkOrder.status == "completed")
                )).scalars().first()
                assert strs_wo is not None, "No self-attributed WorkID created for the Fail submission"
                assert strs_wo.status == "completed"
                assert strs_wo.completed_at is not None
                assert strs_wo.assigned_username == "verify_admin"
                assert strs_wo.assigned_role == "admin"

                # Checklist-driven Stress Notes — shown on L1/L2 Repair and
                # Device Detail, built from exactly the items marked Fail.
                assert dev.stress_notes == "Failed: Keyboard, Touchpad — smoke test", dev.stress_notes

                sr_row = (await db.execute(
                    select(StressTestResult).where(StressTestResult.barcode == fail_barcode)
                )).scalars().first()
                assert sr_row is not None, "No StressTestResult recorded for the failed checklist"
                assert sr_row.overall_status == "FAIL"
                assert sr_row.results_json["Keyboard"] == "fail"
                assert sr_row.results_json["Touchpad"] == "fail"
                assert sr_row.results_json["CPU"] == "pass"
                assert set(sr_row.results_json) == set(STRESS_CHECKLIST_ITEMS)

            if paint_engineer:
                dev2 = (await db.execute(select(Device).where(Device.id == complete_device_id))).scalar_one()
                # Complete now lands on the Cosmetic Received holding stage,
                # not Cleaning directly — see routers/cosmetic.py
                # COSMETIC_PIPELINE (Cosmetic Received sits before Cleaning).
                assert dev2.current_stage == DeviceStage.cosmetic_received, (
                    f"Device not moved to Cosmetic Received, still at {dev2.current_stage}"
                )
                wo2 = (await db.execute(
                    select(WorkOrder).where(WorkOrder.device_id == complete_device_id,
                                             WorkOrder.stage == "recv")
                )).scalars().first()
                assert wo2 is not None, "No WorkOrder created for /complete-to-paint assignment"
                assert wo2.assigned_user_id == paint_engineer.id

                strs_wo2 = (await db.execute(
                    # One Stress WorkID per visit (2026-10-27): the result closes the
                    # tag's Stress WorkID, or records one when none was open.
                    select(WorkOrder).where(WorkOrder.device_id == complete_device_id,
                                             stage_clause(DeviceStage.qc_check),
                                             WorkOrder.status == "completed")
                )).scalars().first()
                assert strs_wo2 is not None, "No self-attributed WorkID created for the Complete submission"
                assert strs_wo2.status == "completed"
                assert strs_wo2.completed_at is not None
                assert strs_wo2.assigned_username == "verify_admin"
                assert strs_wo2.assigned_role == "admin"

                # Complete clears any stale failure note from an earlier run.
                assert dev2.stress_notes is None, dev2.stress_notes

                sr_row2 = (await db.execute(
                    select(StressTestResult).where(StressTestResult.barcode == complete_barcode)
                )).scalars().first()
                assert sr_row2 is not None, "No StressTestResult recorded for the completed checklist"
                assert sr_row2.overall_status == "PASS"
                # Keys starting with "_" are metadata (e.g. "_visit"), not checklist items.
                assert all(v == "pass" for k, v in sr_row2.results_json.items() if not k.startswith("_"))

    finally:
        # ── Cleanup: remove everything this test created ────────────────────
        async with AsyncSessionLocal() as db:
            for bc in (fail_barcode, complete_barcode):
                srs = (await db.execute(select(StressTestResult).where(StressTestResult.barcode == bc))).scalars().all()
                for s in srs:
                    await db.delete(s)
            for did in (fail_device_id, complete_device_id):
                wos = (await db.execute(select(WorkOrder).where(WorkOrder.device_id == did))).scalars().all()
                for wo in wos:
                    await db.delete(wo)
                movements = (await db.execute(
                    select(StageMovement).where(StageMovement.device_id == did)
                )).scalars().all()
                for m in movements:
                    await db.delete(m)
                dev = (await db.execute(select(Device).where(Device.id == did))).scalar_one_or_none()
                if dev:
                    await db.delete(dev)
            await db.commit()
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
