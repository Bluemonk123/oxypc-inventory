"""Search Tags — "Search to View Asset History" modal (2026-09-14), shared
across 6 Tag table pages: L1/L2, L3/L4, Stress Test, All Tags (Cosmetic),
Final QC, Production Manager.

templates/_search_tags_modal.html is included once per page and opened via
a "Search Tags" button; it type-ahead searches ANY tag system-wide (the
existing /devices/api/search-tags endpoint) then shows the picked tag's
Current Stage + most recent Asset History (new /devices/api/asset-history
endpoint) — read-only, entirely inside the modal, no device state changes.
"""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)

PAGES = [
    ("/repair/l1", "L1/L2"),
    ("/repair/l3l4", "L3/L4"),
    ("/qc", "Stress Test"),
    ("/cosmetic/all_tags", "All Tags (Cosmetic)"),
    ("/cosmetic/final_qc", "Final QC"),
    ("/trc-production", "Production Manager"),
]


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"subprocess failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def _seed_device_with_history(barcode):
    """A device with two StageMovement rows, so Asset History has something
    to show. Prints nothing needed — barcode is deterministic."""
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage, StageMovement

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.l1)
        db.add(dev)
        await db.flush()
        db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.iqc, to_stage=DeviceStage.stock_in,
                             moved_by="itest", notes="seed 1"))
        db.add(StageMovement(device_id=dev.id, from_stage=DeviceStage.stock_in, to_stage=DeviceStage.l1,
                             moved_by="itest", notes="seed 2"))
        await db.commit()

asyncio.run(main())
""")


def _cleanup(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device, StageMovement

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for m in (await db.execute(select(StageMovement).where(
                    StageMovement.device_id == dev.id))).scalars().all():
                await db.delete(m)
            dev.is_active = False
        await db.commit()

asyncio.run(main())
""")


def test_modal_present_on_all_six_pages_without_the_old_button(app_client, make_user):  # noqa: F811
    # 2026-11-09: the per-page Search Tags button was removed — the top-bar
    # search opens this same modal (base.html includes it once for every page).
    username, password = make_user("admin")
    _login(app_client, username, password)
    for url, label in PAGES:
        html = app_client.get(url, follow_redirects=True).text
        assert 'onclick="openSearchTagsModal()"' not in html, f"{label} ({url}) still has the Search Tags button"
        assert 'id="searchTagsModal"' in html, f"{label} ({url}) missing the modal itself"
        assert 'id="stModal_search"' in html, f"{label} ({url}) missing the modal's search input"


def test_asset_history_endpoint_returns_current_stage_and_recent_movements(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6].upper()
    barcode = f"ITASTHIST{suffix}"
    _seed_device_with_history(barcode)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)

        r = app_client.get(f"/devices/api/asset-history?barcode={barcode}")
        assert r.status_code == 200, r.text[:300]
        payload = r.json()
        assert payload["found"] is True
        assert payload["barcode"] == barcode
        assert payload["current_stage"], "current_stage must be populated"
        assert len(payload["movements"]) == 2
        # Newest first.
        assert payload["movements"][0]["notes"] == "seed 2"
        assert payload["movements"][1]["notes"] == "seed 1"
        for mv in payload["movements"]:
            assert set(mv.keys()) >= {"from_stage", "to_stage", "moved_by", "moved_at", "notes"}
    finally:
        _cleanup(barcode)


def test_asset_history_endpoint_reports_not_found_for_unknown_tag(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    r = app_client.get("/devices/api/asset-history?barcode=ITNOSUCHTAGXYZ")
    assert r.status_code == 200
    assert r.json()["found"] is False
