"""Part Master Consumed column (crate rows, 2026-10-31).

Each Part Master row is one crate's stock, so Consumed is what was handed
over FROM that row (requests linked to it), not the pooled name total. In
legacy stock mode (Parts Data Fix not applied — the test DB) In Stock still
nets the pooled name+make+model handover total, exactly as before.
"""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)

_SEED_SRC = """
import asyncio, sys
sys.path.insert(0, r"{root}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.device import Device
from models.spare_parts import SparePart
from models.part_request import PartRequest

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).limit(1))).scalars().first()
        p1 = SparePart(part_code="{code1}", name="{name}", category="RAM",
                       source="new", unit_price=100, qty_in_stock=4, min_stock_alert=1)
        p2 = SparePart(part_code="{code2}", name="{name}", category="RAM",
                       source="new", unit_price=100, qty_in_stock=10, min_stock_alert=1)
        db.add(p1)
        db.add(p2)
        await db.flush()
        db.add(PartRequest(part_id=p1.id, part_name="{name}", request_type="new",
                           status="received", qty_handed_over=1, device_id=dev.id))
        db.add(PartRequest(part_id=p1.id, part_name="{name}", request_type="new",
                           status="cancelled", qty_handed_over=0, device_id=dev.id))
        db.add(PartRequest(part_id=p2.id, part_name="{name}", request_type="new",
                           status="received", qty_handed_over=1, device_id=dev.id))
        db.add(PartRequest(part_id=p2.id, part_name="{name}", request_type="faulty",
                           status="handed_over", qty_handed_over=1, device_id=dev.id))
        await db.commit()
        print(p1.id)
        print(p2.id)

asyncio.run(main())
"""

_CLEANUP_SRC = """
import asyncio, sys
sys.path.insert(0, r"{root}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.spare_parts import SparePart
from models.part_request import PartRequest

async def main():
    async with AsyncSessionLocal() as db:
        for code in ["{code1}", "{code2}"]:
            p = (await db.execute(select(SparePart).where(SparePart.part_code == code))).scalar_one_or_none()
            if p:
                p.is_trashed = True
        reqs = (await db.execute(select(PartRequest).where(PartRequest.part_name == "{name}"))).scalars().all()
        for r in reqs:
            await db.delete(r)
        await db.commit()

asyncio.run(main())
"""


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"subprocess failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def test_consumed_column_pools_duplicate_named_parts(app_client, make_user):  # noqa: F811
    name = f"ITESTRAM{uuid.uuid4().hex[:6]}"
    code1 = f"ITC1{uuid.uuid4().hex[:6]}"
    code2 = f"ITC2{uuid.uuid4().hex[:6]}"

    _run(_SEED_SRC.format(root=ROOT, code1=code1, code2=code2, name=name))

    try:
        username, password = make_user("spare_parts_manager")
        _login(app_client, username, password)
        html = app_client.get("/spare-parts", follow_redirects=True).text

        # Consumed = handed over from that crate row: code1 1, code2 1+1 = 2.
        # In Stock (legacy mode) = qty - pooled handovers 3: code1 4-3=1, code2 10-3=7.
        for code, consumed, stock in ((code1, 1, 1), (code2, 2, 7)):
            row = html.split(code, 1)[1].split("</tr>", 1)[0]
            cells = [c.split(">")[-1].strip() for c in row.split("</span>")]
            assert f">{stock}" in row.replace("\n", "").replace(" ", ""), f"{code} In Stock should be {stock}: {row[:600]}"
            assert f">{consumed}<" in row.replace("\n", "").replace(" ", ""), f"{code} Consumed should be {consumed}: {row[:600]}"
    finally:
        _run(_CLEANUP_SRC.format(root=ROOT, code1=code1, code2=code2, name=name))
