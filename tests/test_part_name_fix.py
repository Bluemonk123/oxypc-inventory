"""Part Name = Parts Consumption names (2026-10-31): RAM described in the name
becomes "RAM" (description kept in Model), other case of a name becomes the
exact name, "Battery" is left alone."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_part_name_target_rules():
    from routers.system_health import _part_name_target as t
    assert t("DDR4 8GB") == "RAM" and t("8gb ddr4 ram") == "RAM"
    assert t("keyboard") == "Keyboard" and t("Keyboard") is None
    assert t("Battery") is None and t("RAM Cover") is None and t("HP UK Keyboard") is None


def test_part_name_fix(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    _run(f"""
import asyncio, sys
from decimal import Decimal
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from models.spare_parts import SparePart
async def main():
    async with AsyncSessionLocal() as db:
        db.add(SparePart(name="DDR4 8GB", part_code="PN{sfx}A", category="RAM", qty_in_stock=1, unit_price=Decimal("1")))
        db.add(SparePart(name="Battery", part_code="PN{sfx}B", category="Battery", qty_in_stock=1, unit_price=Decimal("1")))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, pw = make_user("admin")
        _login(app_client, u, pw)
        assert "Part Name — Parts Consumption names" in app_client.get("/admin/system/data-fixes").text
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        r = app_client.post("/admin/system/part-name-fix", data={"csrf_token": csrf}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["ok"], r.text
        out = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for code in ("PN{sfx}A", "PN{sfx}B"):
            n, m = (await db.execute(text(f"select name, model from spare_parts where part_code = '{{code}}'"))).one()
            print(f"{{n}}|{{m}}")
asyncio.run(main())
""").splitlines()
        assert out == ["RAM|DDR4 8GB", "Battery|None"], out
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from spare_parts where part_code like 'PN{sfx}%'"))
        await db.commit()
asyncio.run(main())
""")
