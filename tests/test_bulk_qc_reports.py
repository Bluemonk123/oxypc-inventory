"""Bulk QC (2026-10-28): Ready for Sale's ticked tags and a Block's tags give
one zip of their latest Stress / QC report PDFs; tags without a report are
listed in no_qc_report.txt. The report's manual-check Summary reads
"System Diagnose - Pass". WorkID Status: Scrap Type defaults to All, and the
Stage dropdown leaves out stages that never have WorkIDs."""
import io
import pathlib
import subprocess
import sys
import uuid
import zipfile

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_summary_wording():
    sys.path.insert(0, ROOT)
    from routers.stress_api import _result_status_summary
    assert _result_status_summary("pass") == ("PASS", "System Diagnose - Pass")
    assert _result_status_summary("fail") == ("FAIL", "System Diagnose - Fail")
    assert _result_status_summary("na")[0] == "N/A"


def test_bulk_qc_zip_ready_and_block(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    with_rep, no_rep = f"ITQCA{sfx}", f"ITQCB{sfx}"
    rec_id = _run(f"""
import asyncio, sys, uuid
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage
from models.stress import StressTestResult
from models.block_tags import BlockRecord, BlockRecordItem, BlockItemStatus
from models.location import UnitType
from utils.timezone import app_now
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots limit 1"))).scalar()
        uid = (await db.execute(text("select id from users limit 1"))).scalar()
        devs = []
        for bc in ("{with_rep}", "{no_rep}"):
            d = Device(id=uuid.uuid4(), barcode=bc, lot_id=lot, current_stage=DeviceStage.ready_to_sale)
            db.add(d); devs.append(d)
        db.add(StressTestResult(barcode="{with_rep}", brand="Dell", model_name="Latitude", run_at=app_now(),
                                duration="standard", overall_status="PASS",
                                results_json={{"CPU": "pass", "RAM": "pass"}}))
        rec = BlockRecord(id=uuid.uuid4(), block_id="ITQC-{sfx}", block_date=app_now().date(),
                          container=list(UnitType)[0], created_by_user_id=uid)
        db.add(rec); await db.flush()
        for d in devs:
            db.add(BlockRecordItem(id=uuid.uuid4(), block_record_id=rec.id, device_id=d.id,
                                   barcode=d.barcode, status=BlockItemStatus.blocked))
        await db.commit()
        print(rec.id)
asyncio.run(main())
""")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        csrf = app_client.cookies.get("csrf_token") or "dummy"

        def check(r):
            assert r.status_code == 200, r.text[:300]
            assert r.headers["content-type"] == "application/zip"
            assert r.headers["X-QC-Found"] == "1" and r.headers["X-QC-Missing"] == "1"
            z = zipfile.ZipFile(io.BytesIO(r.content))
            names = z.namelist()
            assert f"QC_Report_{with_rep}.pdf" in names and "no_qc_report.txt" in names
            assert z.read(f"QC_Report_{with_rep}.pdf")[:4] == b"%PDF"
            assert no_rep in z.read("no_qc_report.txt").decode()

        check(app_client.post("/sales/qc-reports", data={"csrf_token": csrf, "barcodes": f"{with_rep},{no_rep}"},
                              headers={"X-CSRF-Token": csrf}))
        check(app_client.get(f"/block-tags/{rec_id}/qc-reports"))
        # nothing to give -> a message, not an empty zip
        r = app_client.post("/sales/qc-reports", data={"csrf_token": csrf, "barcodes": no_rep},
                            headers={"X-CSRF-Token": csrf})
        assert r.status_code == 404 and "QC report" in r.json()["error"]

        html = app_client.get("/workid-status").text
        assert '<option value="all" selected' in html
        stage_box = html.split("cosmetic_stage", 1)[1][:20000]
        for gone in ('value="returned"', 'value="on_rent"', 'value="scrap_for_sale"', 'value="scrapped"'):
            assert gone not in stage_box
        assert 'value="l1"' in stage_box and 'value="ready_to_sale"' in stage_box
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from block_record_items where block_record_id = :r"), {{"r": "{rec_id}"}})
        await db.execute(text("delete from block_records where id = :r"), {{"r": "{rec_id}"}})
        await db.execute(text("delete from stress_test_results where barcode in (:a, :b)"), {{"a": "{with_rep}", "b": "{no_rep}"}})
        await db.execute(text("delete from devices where barcode in (:a, :b)"), {{"a": "{with_rep}", "b": "{no_rep}"}})
        await db.commit()
asyncio.run(main())
""")
