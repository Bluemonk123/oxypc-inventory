"""Production Manager page — summary card tiles (2026-09-02):

Total Tags at You (stage=trc_production), Total Tags in L1/L2 (stage in
l1/l2), Total Tags in L3/L4 (stage=l3), Total Tags in PNA (stage in l1/l2
AND any active device_pna_parts row), Total Tags in Stress (stage=qc_check),
Total Tags in Final QC (stage in final_qc/final_qc_pass_hold/final_qc_fail_hold).

2026-09-19: added "Total Tags in Cosmetic" (stage in the paint-line stages
+ Putty, models.device.COSMETIC_STAGES -- the same grouping Dashboard's own
Stage Pipeline "Cosmetic" step uses, so the two never disagree on what
counts as Cosmetic).
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


def _seed(barcode, stage, mark_pna=False):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage
from models.pna_part import DevicePNAPart

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="X", model="Y",
                     current_stage=DeviceStage.{stage})
        db.add(dev)
        await db.flush()
        if {mark_pna}:
            db.add(DevicePNAPart(device_id=dev.id, barcode="{barcode}", part_name="Keyboard",
                                 source="l3l4", marked_by="tester", is_active=True))
        await db.commit()
        print(dev.id)

asyncio.run(main())
""")


def _cleanup(barcode):
    _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.pna_part import DevicePNAPart

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            for p in (await db.execute(select(DevicePNAPart).where(
                    DevicePNAPart.device_id == dev.id))).scalars().all():
                await db.delete(p)
            await db.flush()
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def _get_count(html, label):
    """Extract the number shown on a given tile (shared tile macro,
    templates/_tiles.html: the count div sits immediately before the label
    div in source order)."""
    import re
    m = re.search(r'class="oxt-count"[^>]*>([\d,]+)</div><div class="oxt-label"[^>]*>' + re.escape(label) + '<', html)
    assert m, f"count not found for label {label!r}"
    return int(m.group(1).replace(",", ""))


def test_summary_tiles_reflect_stage_counts(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcodes = {
        "you": f"ITTILEYOU{suffix}",
        "l1": f"ITTILEL1{suffix}",
        "l3": f"ITTILEL3{suffix}",
        "pna": f"ITTILEPNA{suffix}",
        "stress": f"ITTILESTR{suffix}",
        "fqc": f"ITTILEFQC{suffix}",
        "cosmetic": f"ITTILECOS{suffix}",
    }
    try:
        _seed(barcodes["you"], "trc_production")
        _seed(barcodes["l1"], "l1")
        _seed(barcodes["l3"], "l3")
        _seed(barcodes["pna"], "l2", mark_pna=True)
        _seed(barcodes["stress"], "qc_check")
        _seed(barcodes["fqc"], "final_qc_pass_hold")
        _seed(barcodes["cosmetic"], "painting")

        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/trc-production", follow_redirects=True).text

        assert "Total Tags at You" in html
        assert "Total Tags in L1/L2" in html
        assert "Total Tags in L3/L4" in html
        assert "Total Tags in PNA" in html
        assert "Total Tags in Stress" in html
        assert "Total Tags in Final QC" in html
        assert "Total Tags in Cosmetic" in html

        assert _get_count(html, "Total Tags at You") >= 1
        assert _get_count(html, "Total Tags in L1/L2") >= 2  # l1 + l2(pna) seed
        assert _get_count(html, "Total Tags in L3/L4") >= 1
        assert _get_count(html, "Total Tags in PNA") >= 1
        assert _get_count(html, "Total Tags in Stress") >= 1
        assert _get_count(html, "Total Tags in Final QC") >= 1
        assert _get_count(html, "Total Tags in Cosmetic") >= 1

        # 2026-09-21: Cosmetic moved to appear before Final QC in the tile row.
        assert html.index("Total Tags in Cosmetic") < html.index("Total Tags in Final QC")
    finally:
        for bc in barcodes.values():
            _cleanup(bc)
