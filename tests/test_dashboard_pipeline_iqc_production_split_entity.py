"""Dashboard page, 2026-09-18:
 - Stage Pipeline gains two tiles — IQC (after GRN) and Production
   (trc_production, slotted after L3/L4 per DeviceStage's own declaration
   order) — bringing the row from 8 to 10 tiles.
 - A "Split Entity" toggle renders a per-entity breakdown under each of the
   10 tiles' totals (hidden by default, revealed by a client-side JS toggle
   — no reload).
 - Entity/Device Type/Location filters, which already narrowed the Stage
   Pipeline tiles, now also narrow Total Devices and the category cards.
 - Returned/Scrapped/Scrap for Sale tags are excluded from Total Devices and
   the category cards' totals (they keep their own explicit callout line
   under the pipeline, which is unaffected).
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


def _seed_device(barcode, stage, entity="ITestEntity", device_type="Laptop"):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select
from database import AsyncSessionLocal
from models.lot import Lot
from models.device import Device, DeviceStage

async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(select(Lot).limit(1))).scalars().first()
        dev = Device(barcode="{barcode}", lot_id=lot.id, brand="ITestBrand", model="ITestModel",
                     current_stage=DeviceStage.{stage}, device_price=1000,
                     entity="{entity}", device_type="{device_type}", sub_category="Laptop")
        db.add(dev)
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

async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Device).where(Device.barcode == "{barcode}"))).scalar_one_or_none()
        if dev:
            await db.delete(dev)
        await db.commit()

asyncio.run(main())
""")


def test_pipeline_has_ten_tiles_with_iqc_and_production_in_stage_order(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/dashboard", follow_redirects=True).text
    # 10 tiles now, in DeviceStage's own declaration order: grn, iqc, l1l2,
    # l3l4, production, qc_check, cosmetic, final_qc, ready_to_sale, sold.
    labels_in_order = ["GRN", "IQC", "L1/L2", "L3/L4", "Production",
                        "Stress/QC", "Cosmetic", "Final QC", "Ready to Sale", "Sold"]
    positions = [html.index(f">{label}<") for label in labels_in_order]
    assert positions == sorted(positions), "pipeline tiles are not in DeviceStage order"


def test_split_entity_toggle_button_present(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/dashboard", follow_redirects=True).text
    assert 'id="splitEntityToggle"' in html
    assert "Split Entity" in html
    assert "split-entity-detail" in html


def test_split_entity_breakdown_reflects_seeded_entities(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode_a = f"ITSPLITA{suffix}"
    barcode_b = f"ITSPLITB{suffix}"
    ent_a = f"ITEntA{suffix}"
    ent_b = f"ITEntB{suffix}"
    _seed_device(barcode_a, "iqc", entity=ent_a)
    _seed_device(barcode_b, "iqc", entity=ent_b)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get("/dashboard", follow_redirects=True).text
        assert f"{ent_a}: <strong>1</strong>" in html
        assert f"{ent_b}: <strong>1</strong>" in html
    finally:
        _cleanup(barcode_a)
        _cleanup(barcode_b)


def test_split_entity_breakdown_uses_fixed_display_order(app_client, make_user):  # noqa: F811
    """2026-09-19: GROUP BY has no defined row order, so without an explicit
    sort the 3 real entities came back in whatever order Postgres felt like,
    differing tile to tile. Now fixed: OxyPC Computers, Renew Circuits,
    Deshwal, in that order, in every tile that has all three."""
    suffix = uuid.uuid4().hex[:6]
    barcode_oxy = f"ITORDOXY{suffix}"
    barcode_rc = f"ITORDRC{suffix}"
    barcode_dw = f"ITORDDW{suffix}"
    # "sold" is a stable, rarely-noisy stage to seed into so all three
    # entities are guaranteed a non-zero count in the SAME tile.
    _seed_device(barcode_oxy, "sold", entity="OxyPC Computers")
    _seed_device(barcode_rc, "sold", entity="Renew Circuits")
    _seed_device(barcode_dw, "sold", entity="Deshwal")
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        # entity= (All): the default view is OxyPC + Renew Circuits only
        # (2026-10-30), which would leave Deshwal out of the breakdown.
        html = app_client.get("/dashboard?entity=", follow_redirects=True).text
        # Scope to the "Sold" tile's own split-entity-detail block.
        sold_idx = html.index(">Sold<")
        block = html[sold_idx:sold_idx + 2000]
        i_oxy = block.index(">OxyPC:")              # short names (2026-10-30)
        i_rc = block.index(">Renew:")
        i_dw = block.index(">Deshwal:")
        assert i_oxy < i_rc < i_dw, "entity breakdown not in OxyPC Computers, Renew Circuits, Deshwal order"
    finally:
        for b in (barcode_oxy, barcode_rc, barcode_dw):
            _cleanup(b)


def test_split_entity_detail_styled_for_single_line_small_font():
    html = (pathlib.Path(ROOT) / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert ".split-entity-detail{font-size:12px" in html.replace(" ", "")
    assert ".split-entity-detaildiv{white-space:nowrap}" in html.replace(" ", "")


def test_iqc_and_production_tiles_respect_entity_filter(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode_iqc = f"ITFILTIQC{suffix}"
    barcode_prod = f"ITFILTPROD{suffix}"
    ent = f"ITFiltEnt{suffix}"
    _seed_device(barcode_iqc, "iqc", entity=ent)
    _seed_device(barcode_prod, "trc_production", entity=ent)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get(f"/dashboard?entity={ent}", follow_redirects=True).text
        # Both new tiles' counts are driven entirely by this one seeded
        # device each under this never-used-elsewhere entity, so the tile
        # value is unambiguous.
        assert f">{ent}: <strong>1</strong><" in html
    finally:
        _cleanup(barcode_iqc)
        _cleanup(barcode_prod)


def test_returned_scrapped_scrap_for_sale_excluded_from_total_devices(app_client, make_user):  # noqa: F811
    suffix = uuid.uuid4().hex[:6]
    barcode_ret = f"ITEXRET{suffix}"
    barcode_scr = f"ITEXSCR{suffix}"
    barcode_sfs = f"ITEXSFS{suffix}"
    barcode_live = f"ITEXLIVE{suffix}"
    ent = f"ITExclEnt{suffix}"
    _seed_device(barcode_ret, "returned", entity=ent)
    _seed_device(barcode_scr, "scrapped", entity=ent)
    _seed_device(barcode_sfs, "scrap_for_sale", entity=ent)
    _seed_device(barcode_live, "iqc", entity=ent)
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        html = app_client.get(f"/dashboard?entity={ent}", follow_redirects=True).text
        # Returned/Scrapped/Scrap for Sale callout line still shows 1 each...
        assert "Returned: <strong>1</strong>" in html
        assert "Scrapped: <strong>1</strong>" in html
        assert "Scrap for Sale: <strong>1</strong>" in html
        # ...but the entity-filtered "Total Inventory" stat is exactly 1
        # (only the live IQC device), proving the 3 dead-end stages were
        # excluded from the total rather than inflating it to 4.
        total_inventory_block = html.split("Total Inventory", 1)[1][:200]
        assert '<div class="fs-3 fw-bold">1</div>' in total_inventory_block
    finally:
        for b in (barcode_ret, barcode_scr, barcode_sfs, barcode_live):
            _cleanup(b)
